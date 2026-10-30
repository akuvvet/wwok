"""Letzter bezahlter Preis des Kunden: nur aus abgeschlossenen Rechnungen, nach Rabatten, ohne Stornos."""
from datetime import date
from decimal import Decimal as D

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.kern.models import Mandant
from apps.lager.models import Bewegungsart
from apps.lager.services import buche_manuell
from apps.stammdaten.models import Artikel, Kunde, Lieferant, Preislistenartikel, Steuersatz

from .models import Beleg, Belegart, Position, letzte_preise


@override_settings(SECURE_SSL_REDIRECT=False)
class LetzterPreisTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.kunde = Kunde.objects.create(nummer="1", name1="Muster GmbH")
        self.anderer = Kunde.objects.create(nummer="2", name1="Andere GmbH")
        self.st = Steuersatz.objects.create(bezeichnung="19", prozent=D("19"))
        self.a = Artikel.objects.create(nummer="A1", bezeichnung="Maus", steuersatz=self.st, vk1=D("20"))
        buche_manuell(self.m, self.a, Bewegungsart.WARENEINGANG, D("50"))
        self.user = get_user_model().objects.create_user("t", password="x" * 14, is_staff=True)
        self.client.force_login(self.user)
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    def rechnung(self, preis, datum=None, kunde=None, rabatt="0", belegrabatt="0", abschliessen=True):
        b = Beleg.objects.create(mandant=self.m, art=Belegart.RECHNUNG, kunde=kunde or self.kunde, rabatt_prozent=D(belegrabatt))
        Position.objects.create(beleg=b, artikel=self.a, bezeichnung="Maus", einzelpreis=D(preis), rabatt_prozent=D(rabatt))
        if abschliessen:
            b.abschliessen()
        if datum:
            Beleg.objects.filter(pk=b.pk).update(belegdatum=datum)
        return b

    def letzter(self, **kw):
        return letzte_preise(self.m, self.kunde, [self.a.pk], **kw).get(("a", self.a.pk))

    def test_nur_abgeschlossene_rechnung_des_kunden_und_die_juengste(self):
        self.assertIsNone(self.letzter())
        self.rechnung("10", date(2026, 1, 1))
        b = self.rechnung("12", date(2026, 3, 1))
        self.rechnung("9", date(2026, 2, 1))
        self.rechnung("1", abschliessen=False)  # Entwurf zählt nicht
        self.rechnung("2", kunde=self.anderer)  # anderer Kunde auch nicht
        angebot = Beleg.objects.create(mandant=self.m, art=Belegart.ANGEBOT, kunde=self.kunde)
        Position.objects.create(beleg=angebot, artikel=self.a, bezeichnung="Maus", einzelpreis=D("3"))
        z = self.letzter()
        self.assertEqual((z["preis"], z["datum"], z["beleg_id"]), (D("12.00"), date(2026, 3, 1), b.pk))

    def test_rabatte_sind_eingerechnet(self):
        self.rechnung("100", rabatt="10", belegrabatt="5")
        self.assertEqual(self.letzter()["preis"], D("85.50"))

    def test_voll_gutgeschriebene_rechnung_zaehlt_nicht(self):
        self.rechnung("10", date(2026, 1, 1))
        b = self.rechnung("12", date(2026, 3, 1))
        gs = Beleg.objects.create(mandant=self.m, art=Belegart.GUTSCHRIFT, kunde=self.kunde, vorgaenger=b)
        Position.objects.create(beleg=gs, bezeichnung="Storno", einzelpreis=D("12"))
        Beleg.objects.filter(pk=gs.pk).update(abgeschlossen=True, summe_brutto=b.summe_brutto)
        self.assertEqual(self.letzter()["preis"], D("10.00"))

    def gutschrift(self, rechnung, menge="1", brutto="1", artikel=True):
        gs = Beleg.objects.create(mandant=self.m, art=Belegart.GUTSCHRIFT, kunde=self.kunde, vorgaenger=rechnung)
        Position.objects.create(beleg=gs, artikel=self.a if artikel else None, bezeichnung="Storno", einzelpreis=D("1"), menge=D(menge))
        Beleg.objects.filter(pk=gs.pk).update(abgeschlossen=True, summe_brutto=D(brutto))

    def test_mehrere_teilgutschriften_ergeben_voll_storniert(self):
        self.rechnung("10", date(2026, 1, 1))
        b = self.rechnung("12", date(2026, 3, 1))
        halb = b.summe_brutto / 2
        self.gutschrift(b, menge="0.4", brutto=str(halb), artikel=False)
        self.gutschrift(b, menge="0.4", brutto=str(halb), artikel=False)
        self.assertEqual(self.letzter()["preis"], D("10.00"))

    def test_zurueckgegebene_menge_zaehlt_je_artikel_auch_ohne_vollen_betrag(self):
        self.rechnung("10", date(2026, 1, 1))
        b = self.rechnung("12", date(2026, 3, 1))
        self.gutschrift(b, menge="1", brutto="1")  # ganze Position zurück, Betrag kleiner (z. B. Versand bleibt)
        self.assertEqual(self.letzter()["preis"], D("10.00"))

    def test_teilrueckgabe_laesst_den_preis_gelten(self):
        b = self.rechnung("12", date(2026, 3, 1))
        Position.objects.filter(beleg=b).update(menge=D("3"))
        self.gutschrift(b, menge="1", brutto="1")
        self.assertEqual(self.letzter()["preis"], D("12.00"))

    def test_preislistenartikel(self):
        lf = Lieferant.objects.create(nummer="L1", name1="API", kurzname="API")
        e = Preislistenartikel.objects.create(lieferant=lf, bestellnummer="X", bezeichnung="Kabel", ek=D("1"))
        b = Beleg.objects.create(mandant=self.m, art=Belegart.RECHNUNG, kunde=self.kunde)
        Position.objects.create(beleg=b, preisliste_artikel=e, typ="P", bezeichnung="Kabel", einzelpreis=D("7"))
        Beleg.objects.filter(pk=b.pk).update(abgeschlossen=True)
        self.assertEqual(letzte_preise(self.m, self.kunde, preisliste_ids=[e.pk])[("p", e.pk)]["preis"], D("7.00"))

    def test_anzeige_in_der_kundenbestellung_und_der_suche(self):
        b = self.rechnung("100", date(2026, 3, 1), rabatt="10")
        auftrag = Beleg.objects.create(mandant=self.m, art=Belegart.AUFTRAG, kunde=self.kunde)
        Position.objects.create(beleg=auftrag, artikel=self.a, bezeichnung="Maus", einzelpreis=D("20"))
        r = self.client.get(reverse("verkauf:beleg", args=[auftrag.pk]))
        self.assertContains(r, "Zuletzt bezahlt:")
        self.assertContains(r, "90,00 €")
        self.assertContains(r, f">{b.nummer}</a>")
        d = self.client.get(reverse("verkauf:api_artikel"), {"q": "maus", "kunde": self.kunde.pk}).json()
        self.assertEqual(d["treffer"][0]["letzter"], {"preis": "90,00", "datum": "01.03.2026", "nummer": b.nummer})
        d = self.client.get(reverse("verkauf:api_artikel"), {"q": "maus", "kunde": self.anderer.pk}).json()
        self.assertIsNone(d["treffer"][0]["letzter"])

    def test_eigene_rechnung_und_gutschrift_zeigen_nichts_von_sich_selbst(self):
        b = self.rechnung("10")
        self.assertNotContains(self.client.get(reverse("verkauf:beleg", args=[b.pk])), "Zuletzt bezahlt:")
