"""Gutschriften über dem Rechnungsbetrag: Warnung mit Pflichtbegründung statt Sperre; Zahlungsweg auf der Zahlungsseite."""
from datetime import date
from decimal import Decimal as D

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.kern.models import Mandant
from apps.stammdaten.models import Kunde, Zahlungsbedingung
from apps.verkauf.models import Beleg, Belegart, GutschriftUeberRechnung, Position

from . import services
from .models import Kassenbuch, Zahlart


@override_settings(SECURE_SSL_REDIRECT=False)
class GutschriftUeberRechnungTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.kunde = Kunde.objects.create(nummer="1", name1="Kunde GmbH")
        self.zb = Zahlungsbedingung.objects.create(bezeichnung="10 Tage", tage_netto=10, tage_mahnung=11)
        self.chef = get_user_model().objects.create_superuser("chef", password="x" * 14)
        self.client.force_login(self.chef)
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()
        self.r = Beleg.objects.create(mandant=self.m, art=Belegart.RECHNUNG, kunde=self.kunde, zahlungsbedingung=self.zb)
        Position.objects.create(beleg=self.r, bezeichnung="Dienst", einzelpreis=D("100"))
        self.r.abschliessen()
        self.r.refresh_from_db()

    def entwurf(self, netto):
        """Gutschrift-Entwurf zur Rechnung mit dem gewünschten Nettobetrag (Rechnung: 100 netto, 119 brutto)."""
        g = self.r.gutschrift_erzeugen()
        p = g.positionen.get()
        p.einzelpreis = D(netto)
        p.save()
        g.berechne()
        return Beleg.objects.get(pk=g.pk)

    def test_ueber_dem_rechnungsbetrag_nur_mit_begruendung(self):
        g = self.entwurf("120")
        with self.assertRaisesRegex(GutschriftUeberRechnung, "23,80 € über dem Rechnungsbetrag von 119,00 €"):
            g.abschliessen()
        g = Beleg.objects.get(pk=g.pk)
        self.assertFalse(g.abgeschlossen)
        self.assertFalse(g.nummer)
        with self.assertRaises(GutschriftUeberRechnung):
            g.abschliessen(ueber_grund="   ")  # Leerzeichen sind keine Begründung
        g = Beleg.objects.get(pk=g.pk)
        g.abschliessen(ueber_grund="Kulanz:  Fahrtkosten erstattet")
        g.refresh_from_db()
        self.assertTrue(g.abgeschlossen)
        self.assertEqual(g.ueber_rechnung_grund, "Kulanz: Fahrtkosten erstattet")

    def test_genau_der_rechnungsbetrag_braucht_keine_begruendung(self):
        g = self.entwurf("100")
        g.abschliessen()
        g.refresh_from_db()
        self.assertTrue(g.abgeschlossen)
        self.assertEqual(g.ueber_rechnung_grund, "")

    def test_teilgutschriften_werden_zusammen_geprueft(self):
        erste = self.entwurf("60")
        erste.abschliessen()  # 71,40 von 119,00: in Ordnung
        zweite = self.entwurf("60")  # zusammen 142,80
        self.assertEqual(zweite.gutschrift_ueber_rechnung()["ueber"], D("23.80"))
        with self.assertRaisesRegex(GutschriftUeberRechnung, "zusammen 142,80 €"):
            zweite.abschliessen()
        zweite = Beleg.objects.get(pk=zweite.pk)
        zweite.abschliessen(ueber_grund="Nachträglicher Preisnachlass")
        self.assertTrue(Beleg.objects.get(pk=zweite.pk).abgeschlossen)
        self.assertEqual(Beleg.objects.get(pk=erste.pk).ueber_rechnung_grund, "")  # die erste lag im Rahmen

    def test_eine_begruendung_ohne_ueberschreitung_wird_nicht_gespeichert(self):
        g = self.entwurf("50")
        g.abschliessen(ueber_grund="nicht nötig")
        self.assertEqual(Beleg.objects.get(pk=g.pk).ueber_rechnung_grund, "")

    def test_maske_fragt_nach_und_speichert_grund_und_benutzer(self):
        g = self.entwurf("120")
        seite = self.client.get(reverse("verkauf:beleg", args=[g.pk]))
        self.assertContains(seite, "mehr als der Rechnungsbetrag")
        ziel = reverse("verkauf:beleg_abschliessen", args=[g.pk])
        antwort = self.client.post(ziel, {"danach": ""})
        self.assertContains(antwort, "Gutschrift über dem Rechnungsbetrag")
        self.assertContains(antwort, 'name="ueber_grund"')
        self.assertFalse(Beleg.objects.get(pk=g.pk).abgeschlossen)
        antwort = self.client.post(ziel, {"danach": "", "ueber_grund": " "})
        self.assertContains(antwort, "Bitte eine Begründung eintragen.")
        self.assertFalse(Beleg.objects.get(pk=g.pk).abgeschlossen)
        antwort = self.client.post(ziel, {"danach": "", "ueber_grund": "Kulanz wegen Lieferverzug"})
        self.assertEqual(antwort.status_code, 302)
        g = Beleg.objects.get(pk=g.pk)
        self.assertTrue(g.abgeschlossen)
        self.assertEqual((g.ueber_rechnung_grund, g.ueber_rechnung_von), ("Kulanz wegen Lieferverzug", self.chef))
        self.assertContains(self.client.get(reverse("verkauf:beleg", args=[g.pk])), "Begründung: Kulanz wegen Lieferverzug")

    def test_zahlungsweg_von_der_rechnung_bis_zur_auszahlung(self):
        heute = date.today()
        services.zahlung_buchen(self.r, D("119.00"), heute, Zahlart.BAR, "Eingang", self.chef)
        g = self.entwurf("100")
        g.abschliessen()
        g.refresh_from_db()
        services.gutschrift_verrechnen(g, self.r, D("119.00"), self.chef)
        services.zahlung_buchen(self.r, D("-119.00"), heute, Zahlart.BAR, "Rückgabe an der Theke", self.chef)
        weg = services.zahlungsweg(self.r)
        self.assertEqual([z["vorgang"] for z in weg["zeilen"]], [
            f"Rechnung {self.r.nummer} ausgestellt", "Zahlungseingang", f"Gutschrift {g.nummer} ausgestellt",
            f"Gutschrift verrechnet: {g.nummer} → Rechnung {self.r.nummer}", "Auszahlung an den Kunden"])
        self.assertEqual([z["stand"] for z in weg["zeilen"]], [D("119.00"), D("0.00"), None, D("-119.00"), D("0.00")])
        self.assertEqual((weg["eingang"], weg["ausgezahlt"], weg["gutschriften"], weg["verrechnet"], weg["frei"], weg["stand"]),
                         (D("119.00"), D("119.00"), D("119.00"), D("119.00"), D("0.00"), D("0.00")))
        self.assertEqual(weg["stand"], services.offen(self.r))
        nummer = Kassenbuch.objects.get(beleg=self.r, art=Kassenbuch.AUSGABE).nummer
        aus = next(z for z in weg["zeilen"] if z["art"] == "aus")
        self.assertEqual(aus["weg"], f"Bar – Kassenbuch Nr. {nummer}")
        self.assertEqual((aus["benutzer"], aus["text"]), (self.chef, "Rückgabe an der Theke"))
        self.assertIsNone(weg["ueber"])
        # Die Gutschrift zeigt denselben Weg
        self.assertEqual(len(services.zahlungsweg(g)["zeilen"]), 5)
        seite = self.client.get(reverse("finanzen:beleg", args=[self.r.pk]))
        self.assertContains(seite, "Zahlungsweg")
        self.assertContains(seite, "Auszahlung an den Kunden")
        self.assertContains(seite, "Zahlung buchen")
        seite = self.client.get(reverse("finanzen:beleg", args=[g.pk]))
        self.assertContains(seite, "Zahlungsweg")
        self.assertContains(seite, "noch frei (Guthaben des Kunden)")
        self.assertNotContains(seite, "Zahlung buchen")
        self.assertContains(self.client.get(reverse("verkauf:beleg", args=[g.pk])), reverse("finanzen:beleg", args=[g.pk]))

    def test_zahlungsweg_storno_und_begruendung(self):
        heute = date.today()
        z = services.zahlung_buchen(self.r, D("50.00"), heute, Zahlart.UEBERWEISUNG, "", self.chef)
        services.zahlung_stornieren(z, "falsche Rechnung", self.chef)
        g = self.entwurf("120")
        g.abschliessen(ueber_grund="Kulanz")
        weg = services.zahlungsweg(self.r)
        self.assertEqual(weg["eingang"], D("0.00"))  # der stornierte Eingang zählt nicht, auch nicht als Auszahlung
        self.assertEqual(weg["ausgezahlt"], D("0.00"))
        self.assertIn("Storno eines Zahlungseingangs", [z["vorgang"] for z in weg["zeilen"]])
        self.assertEqual(weg["ueber"], D("23.80"))
        self.assertEqual(weg["stand"], services.offen(self.r))
        seite = self.client.get(reverse("finanzen:beleg", args=[self.r.pk]))
        self.assertContains(seite, "Über dem Rechnungsbetrag – Begründung: Kulanz")
        self.assertContains(seite, "falsche Rechnung")

    def test_zahlungsweg_nur_fuer_abgeschlossene_rechnung_und_gutschrift(self):
        self.assertIsNone(services.zahlungsweg(self.entwurf("10")))
        ab = Beleg.objects.create(mandant=self.m, art=Belegart.AUFTRAG, kunde=self.kunde, zahlungsbedingung=self.zb)
        self.assertIsNone(services.zahlungsweg(ab))
