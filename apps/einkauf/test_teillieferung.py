"""Wareneingang als Teillieferung: fehlende Positionen nicht buchen, Rest bleibt in der Bestellung offen."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.kern.models import Mandant
from apps.lager.services import bestand
from apps.stammdaten.models import Artikel, ArtikelLieferant, Lieferant, Steuersatz

from . import services
from .models import BestellStatus


@override_settings(SECURE_SSL_REDIRECT=False)
class TeillieferungTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.lf = Lieferant.objects.create(nummer="L1", name1="Techdata GmbH")
        st = Steuersatz.objects.create(bezeichnung="19", prozent=Decimal("19"))
        self.maus = Artikel.objects.create(nummer="M1", bezeichnung="Maus", steuersatz=st, vk1=Decimal("15"))
        self.tast = Artikel.objects.create(nummer="T1", bezeichnung="Tastatur", steuersatz=st, vk1=Decimal("25"))
        ArtikelLieferant.objects.create(artikel=self.maus, lieferant=self.lf, bestellnummer="TD-1", ek=Decimal("9"))
        ArtikelLieferant.objects.create(artikel=self.tast, lieferant=self.lf, bestellnummer="TD-2", ek=Decimal("18"))
        self.b = services.bestellung_anlegen(self.m, self.lf)
        services.bestellposition(self.b, self.maus, Decimal("5"))
        services.bestellposition(self.b, self.tast, Decimal("2"))
        services.bestellen(self.b)
        user = get_user_model().objects.create_superuser("t", password="x" * 14)
        self.client.force_login(user)
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()
        self.client.post(reverse("einkauf:wareneingang_neu"), {"bestellung": self.b.pk})
        from .models import Wareneingang
        self.we = Wareneingang.objects.get(bestellung=self.b)

    def zeile(self, artikel):
        return self.we.positionen.get(artikel=artikel)

    def test_position_fehlt_menge_0_bleibt_offen(self):
        url = reverse("einkauf:wareneingang_zeile", args=[self.we.pk, self.zeile(self.tast).pk])
        self.client.post(url, {"menge": "0", "ek": "18"}, follow=True)
        self.assertFalse(self.we.positionen.filter(artikel=self.tast).exists())
        seite = self.client.get(reverse("einkauf:wareneingang", args=[self.we.pk]))
        self.assertContains(seite, "Teillieferung: bleibt in der Bestellung")
        self.assertContains(seite, "doch geliefert")
        rest = services.bleibt_offen(self.we)
        self.assertEqual([(z["bp"].artikel_id, z["rest"]) for z in rest], [(self.tast.pk, Decimal("2"))])
        # weniger Mäuse geliefert
        self.client.post(reverse("einkauf:wareneingang_zeile", args=[self.we.pk, self.zeile(self.maus).pk]), {"menge": "3", "ek": "9"})
        antwort = self.client.post(reverse("einkauf:wareneingang_buchen", args=[self.we.pk]), follow=True)
        self.assertContains(antwort, "Teillieferung: 2 Position(en) bleiben")
        self.b.refresh_from_db()
        self.assertEqual(self.b.status, BestellStatus.TEIL)
        self.assertEqual(bestand(self.m, self.maus), Decimal("3"))
        self.assertEqual(bestand(self.m, self.tast), Decimal("0"))
        offen = {p.artikel_id: p.offen for p in self.b.positionen.all()}
        self.assertEqual(offen, {self.maus.pk: Decimal("2"), self.tast.pk: Decimal("2")})
        # gebuchter WE zeigt den Rest weiter an
        self.assertContains(self.client.get(reverse("einkauf:wareneingang", args=[self.we.pk])), "kommen mit einem späteren Wareneingang")
        # Nachlieferung: zweiter Wareneingang übernimmt nur den Rest, danach vollständig
        self.client.post(reverse("einkauf:wareneingang_neu"), {"bestellung": self.b.pk})
        from .models import Wareneingang
        we2 = Wareneingang.objects.filter(bestellung=self.b).exclude(pk=self.we.pk).get()
        self.assertEqual({p.artikel_id: p.menge for p in we2.positionen.all()}, {self.maus.pk: Decimal("2"), self.tast.pk: Decimal("2")})
        services.wareneingang_buchen(we2)
        self.b.refresh_from_db()
        self.assertEqual(self.b.status, BestellStatus.GELIEFERT)
        self.assertEqual(services.bleibt_offen(we2), [])

    def test_doch_geliefert_nimmt_zeile_wieder_auf(self):
        tast_bp = self.zeile(self.tast).bestellposition
        self.client.post(reverse("einkauf:wareneingang_zeile", args=[self.we.pk, self.zeile(self.tast).pk]), {"aktion": "loeschen"})
        self.client.post(reverse("einkauf:wareneingang_aufnehmen", args=[self.we.pk, tast_bp.pk]))
        self.assertEqual(self.zeile(self.tast).menge, Decimal("2"))
        self.client.post(reverse("einkauf:wareneingang_aufnehmen", args=[self.we.pk, tast_bp.pk]))  # doppelt: keine zweite Zeile
        self.assertEqual(self.we.positionen.filter(artikel=self.tast).count(), 1)
        self.assertEqual(services.bleibt_offen(self.we), [])

    def test_aufnehmen_fremde_bestellposition_und_gebucht(self):
        fremd = services.bestellung_anlegen(self.m, self.lf)
        services.bestellposition(fremd, self.maus, Decimal("1"))
        bp = fremd.positionen.get()
        self.assertEqual(self.client.post(reverse("einkauf:wareneingang_aufnehmen", args=[self.we.pk, bp.pk])).status_code, 404)
        services.wareneingang_buchen(self.we)
        self.we.refresh_from_db()
        with self.assertRaises(ValueError):
            services.wareneingang_aus_bestellung(self.we)

    def test_ohne_bestellung_keine_menge_0(self):
        from .models import Wareneingang
        we = services.wareneingang_anlegen(self.m, self.lf)
        p = services.wareneingang_erfassen(we, self.maus, Decimal("1"))
        self.assertEqual(services.bleibt_offen(we), [])
        self.client.post(reverse("einkauf:wareneingang_zeile", args=[we.pk, p.pk]), {"menge": "0", "ek": "9"})
        self.assertTrue(Wareneingang.objects.get(pk=we.pk).positionen.filter(pk=p.pk).exists())  # Fehler, Zeile bleibt
