from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.kern.models import Mandant

from . import preise
from .models import Artikel, Kategorie, Kunde, Kundenpreis, PreisVorgabe, Steuersatz


class PreiseTest(TestCase):
    def setUp(self):
        st = Steuersatz.objects.create(bezeichnung="19", prozent=Decimal("19"))
        self.ober = Kategorie.objects.create(name="Hardware", nummer="1")
        self.unter = Kategorie.objects.create(name="Mäuse", nummer="101", eltern=self.ober)
        self.a = Artikel.objects.create(nummer="A1", bezeichnung="Maus", steuersatz=st, kategorie=self.unter,
                                        durchschnitts_ek=Decimal("10.0000"))
        self.endkunde = Kunde.objects.create(nummer="1", name1="Privat", preisstufe=1)
        self.haendler = Kunde.objects.create(nummer="2", name1="Händler", preisstufe=3)

    def test_aufschlag_reihenfolge_artikel_kategorie_global(self):
        self.assertIsNone(preise.aufschlag(self.a, 1))
        PreisVorgabe.objects.create(aufschlag_vk1=Decimal("20"), aufschlag_vk3=Decimal("5"))
        self.assertEqual(preise.aufschlag(self.a, 1), Decimal("20"))  # global
        self.ober.aufschlag_vk1 = Decimal("30")
        self.ober.save()
        self.assertEqual(preise.aufschlag(self.a, 1), Decimal("30"))  # Oberkategorie vor global
        self.unter.aufschlag_vk1 = Decimal("40")
        self.unter.save()
        self.assertEqual(preise.aufschlag(self.a, 1), Decimal("40"))  # Unterkategorie vor Oberkategorie
        self.a.aufschlag_vk1 = Decimal("50")
        self.a.save()
        self.assertEqual(preise.aufschlag(self.a, 1), Decimal("50"))  # Artikel vor Kategorie
        self.assertEqual(preise.aufschlag(self.a, 3), Decimal("5"))  # andere Stufe: global

    def test_preis_aus_ek_fest_und_haendlerstufe(self):
        PreisVorgabe.objects.create(aufschlag_vk1=Decimal("25"), aufschlag_vk3=Decimal("10"))
        self.assertEqual(preise.verkaufspreis(self.a, self.endkunde), (Decimal("12.50"), "stufe"))
        self.assertEqual(preise.verkaufspreis(self.a, self.haendler), (Decimal("11.00"), "stufe"))
        self.a.vk1 = Decimal("14.99")  # fest eingetragener VK gewinnt vor dem Aufschlag
        self.a.save()
        self.assertEqual(preise.verkaufspreis(self.a, self.endkunde)[0], Decimal("14.99"))
        self.assertEqual(preise.verkaufspreis(self.a, self.haendler)[0], Decimal("11.00"))
        ohne_ek = Artikel.objects.create(nummer="A2", bezeichnung="x", steuersatz=self.a.steuersatz)
        self.assertEqual(preise.verkaufspreis(ohne_ek, self.endkunde), (None, None))

    def test_haendlerstufe_faellt_auf_vk1_zurueck(self):
        self.a.vk1 = Decimal("20")
        self.a.save()
        self.assertEqual(preise.verkaufspreis(self.a, self.haendler)[0], Decimal("20"))

    def test_sonderpreis_befristet(self):
        heute = date.today()
        k = Kundenpreis.objects.create(kunde=self.endkunde, artikel=self.a, preis=Decimal("7.77"),
                                       gueltig_ab=heute - timedelta(days=1), gueltig_bis=heute + timedelta(days=1))
        self.a.vk1 = Decimal("20")
        self.a.save()
        self.assertEqual(preise.verkaufspreis(self.a, self.endkunde), (Decimal("7.77"), "sonderpreis"))
        self.assertEqual(preise.verkaufspreis(self.a, self.haendler)[0], Decimal("20"))  # nur für diesen Kunden
        k.gueltig_bis = heute - timedelta(days=1)
        k.save()
        self.assertEqual(preise.verkaufspreis(self.a, self.endkunde), (Decimal("20"), "stufe"))  # abgelaufen

    def test_nur_eine_globale_vorgabe(self):
        PreisVorgabe.objects.create(aufschlag_vk1=Decimal("1"))
        with self.assertRaises(ValueError):
            PreisVorgabe.objects.create(aufschlag_vk1=Decimal("2"))


@override_settings(SECURE_SSL_REDIRECT=False)
class PreiseImBelegTest(TestCase):
    def test_beleg_nutzt_sonderpreis_und_aufschlag(self):
        from apps.verkauf.models import Beleg, Belegart
        m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        st = Steuersatz.objects.create(bezeichnung="19", prozent=Decimal("19"))
        kunde = Kunde.objects.create(nummer="1", name1="K", preisstufe=1)
        a = Artikel.objects.create(nummer="A1", bezeichnung="Maus", steuersatz=st, durchschnitts_ek=Decimal("10"))
        PreisVorgabe.objects.create(aufschlag_vk1=Decimal("50"))
        self.client.force_login(get_user_model().objects.create_user("t", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = m.pk
        s.save()
        beleg = Beleg.objects.create(mandant=m, art=Belegart.ANGEBOT, kunde=kunde)
        self.client.post(reverse("verkauf:position_neu", args=[beleg.pk]), {"artikel": "A1", "menge": "1"})
        self.assertEqual(beleg.positionen.get().einzelpreis, Decimal("15.00"))
        Kundenpreis.objects.create(kunde=kunde, artikel=a, preis=Decimal("12"))
        api = self.client.get(reverse("verkauf:api_artikel"), {"q": "Maus", "kunde": kunde.pk}).json()["treffer"][0]
        self.assertEqual((api["preis"], api["sonderpreis"]), ("12.00", True))


class EkErrechnetTest(TestCase):
    def setUp(self):
        from apps.stammdaten.models import ArtikelLieferant, GruppenKomponente, Lieferant
        st = Steuersatz.objects.create(bezeichnung="19", prozent=Decimal("19"))
        self.a = Artikel.objects.create(nummer="A1", bezeichnung="CPU", steuersatz=st)
        self.b = Artikel.objects.create(nummer="A2", bezeichnung="RAM", steuersatz=st, durchschnitts_ek=Decimal("50"))
        l = Lieferant.objects.create(nummer="L1", name1="API")
        ArtikelLieferant.objects.create(artikel=self.a, lieferant=l, bestellnummer="x", ek=Decimal("100"))
        ArtikelLieferant.objects.create(artikel=self.b, lieferant=l, bestellnummer="y", ek=Decimal("40"))
        self.g = Artikel.objects.create(nummer="G1", typ="G", bezeichnung="PC", steuersatz=st)
        GruppenKomponente.objects.create(gruppe=self.g, komponente=self.a, menge=1)
        GruppenKomponente.objects.create(gruppe=self.g, komponente=self.b, menge=2)

    def test_wareneingang_vor_preisliste(self):
        self.assertEqual(self.a.ek_errechnet, Decimal("100"))   # nur Preisliste
        self.assertEqual(self.b.ek_errechnet, Decimal("50"))    # Wareneingang hat Vorrang

    def test_gruppe_summe(self):
        self.assertEqual(self.g.ek_errechnet, Decimal("200"))
