"""Prüfbericht E4 (Entscheidung 15.11.2026): Für andere Kundenbestellungen reservierte Ware ist nicht lieferbar – wie „verfügbar 0“."""
from decimal import Decimal

from django.test import TestCase

from apps.kern.models import Mandant
from apps.stammdaten.models import Artikel, Kunde, Steuersatz
from apps.verkauf.models import Beleg, Belegart, Position

from .models import BestandReichtNicht, Bewegungsart, bestand, reserviert
from .services import buche_manuell, druckhindernis, fehlbestand


class ReserviertTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.m2 = Mandant.objects.create(kurzname="ZONE", firmenname="ZONE Handel")
        self.k1 = Kunde.objects.create(nummer="1", name1="Erster")
        self.k2 = Kunde.objects.create(nummer="2", name1="Zweiter")
        st = Steuersatz.objects.create(bezeichnung="19", prozent=Decimal("19"))
        self.a = Artikel.objects.create(nummer="A1", bezeichnung="Maus", steuersatz=st, vk1=Decimal("10"))
        buche_manuell(self.m, self.a, Bewegungsart.WARENEINGANG, Decimal("10"))

    def beleg(self, art, menge, kunde=None, mandant=None):
        b = Beleg.objects.create(mandant=mandant or self.m, art=art, kunde=kunde or self.k1)
        Position.objects.create(beleg=b, artikel=self.a, bezeichnung="Maus", menge=Decimal(menge), einzelpreis=Decimal("10"))
        return b

    def test_fremde_reservierung_sperrt_lieferschein_und_rechnung(self):
        self.beleg(Belegart.AUFTRAG, "8")  # Kunde 1 hat 8 von 10 bestellt
        ls = self.beleg(Belegart.LIEFERSCHEIN, "3", kunde=self.k2)
        self.assertIn("davon 8 für andere Bestellungen reserviert", fehlbestand(ls)[0])
        self.assertIn("Lagerbestand reicht nicht", druckhindernis(ls))
        with self.assertRaises(BestandReichtNicht):
            ls.abschliessen()
        self.assertEqual(bestand(self.m, self.a), Decimal("10"))
        re = self.beleg(Belegart.RECHNUNG, "3", kunde=self.k2)  # Rechnung ohne Lieferschein bucht selbst ab: gleiche Regel
        with self.assertRaises(BestandReichtNicht):
            re.abschliessen()

    def test_freie_menge_bleibt_lieferbar(self):
        self.beleg(Belegart.AUFTRAG, "8")
        ls = self.beleg(Belegart.LIEFERSCHEIN, "2", kunde=self.k2)  # genau die freie Menge
        self.assertEqual(fehlbestand(ls), [])
        ls.abschliessen()
        self.assertEqual((bestand(self.m, self.a), reserviert(self.m, self.a)), (Decimal("8"), Decimal("8")))

    def test_eigene_bestellung_blockiert_ihre_lieferung_nicht(self):
        ab = self.beleg(Belegart.AUFTRAG, "10")
        ls = ab.folgebeleg_erzeugen()  # Lieferschein aus der Bestellung
        self.assertEqual(fehlbestand(ls), [])
        ls.abschliessen()
        self.assertEqual(bestand(self.m, self.a), Decimal("0"))

    def test_rest_der_eigenen_bestellung_zaehlt_nicht_als_fremd(self):
        ab = self.beleg(Belegart.AUFTRAG, "8")
        ls = Beleg.objects.create(mandant=self.m, art=Belegart.LIEFERSCHEIN, kunde=self.k1, vorgaenger=ab)  # Teillieferung, Rest bleibt reserviert
        Position.objects.create(beleg=ls, artikel=self.a, bezeichnung="Maus", menge=Decimal("5"), einzelpreis=Decimal("10"))
        self.assertEqual(fehlbestand(ls), [])
        ls.abschliessen()
        self.assertEqual(bestand(self.m, self.a), Decimal("5"))

    def test_reservierung_im_anderen_mandanten_zaehlt_nicht(self):
        self.beleg(Belegart.AUFTRAG, "10", mandant=self.m2)
        ls = self.beleg(Belegart.LIEFERSCHEIN, "10", kunde=self.k2)
        ls.abschliessen()
        self.assertEqual(bestand(self.m, self.a), Decimal("0"))

    def test_nach_wareneingang_wieder_lieferbar(self):
        self.beleg(Belegart.AUFTRAG, "8")
        ls = self.beleg(Belegart.LIEFERSCHEIN, "3", kunde=self.k2)
        buche_manuell(self.m, self.a, Bewegungsart.WARENEINGANG, Decimal("1"))
        ls.abschliessen()
        self.assertEqual(bestand(self.m, self.a), Decimal("8"))
