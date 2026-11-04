from decimal import Decimal as D

from django.test import SimpleTestCase

from apps.einkauf.preislisten import text, zahl


class PreisZahlTest(SimpleTestCase):
    """EK aus der Preisliste: deutsche und englische Schreibweise unabhängig vom Profil richtig lesen."""

    def test_deutsche_schreibweise_auch_im_punkt_profil(self):
        # früher wurde daraus 1250 bzw. 99 – der Preis landete in der Preisprüfung und wurde nicht übernommen
        for profil in (",", "."):
            self.assertEqual(zahl("12,50", profil), D("12.50"))
            self.assertEqual(zahl("0,99", profil), D("0.99"))
            self.assertEqual(zahl("12,5 €", profil), D("12.5"))
            self.assertEqual(zahl("1.234,50", profil), D("1234.50"))

    def test_englische_schreibweise_auch_im_komma_profil(self):
        for profil in (",", "."):
            self.assertEqual(zahl("12.50", profil), D("12.50"))
            self.assertEqual(zahl("1,234.50", profil), D("1234.50"))
            self.assertEqual(zahl("1,234,567.00", profil), D("1234567.00"))
            self.assertEqual(zahl("1.234.567,00", profil), D("1234567.00"))

    def test_excel_zahlen(self):
        for profil in (",", "."):
            self.assertEqual(zahl(text(12.5), profil), D("12.5"))
            self.assertEqual(zahl(text(1234.56), profil), D("1234.56"))
            self.assertEqual(zahl(text(15.0), profil), D("15"))

    def test_mehrdeutig_folgt_dem_profil(self):
        self.assertEqual(zahl("1,234", "."), D("1234"))  # Tausenderkomma im Punkt-Profil
        self.assertEqual(zahl("1,234", ","), D("1.234"))
        self.assertEqual(zahl("1.234", ","), D("1.234"))  # einzelner Punkt bleibt Dezimalpunkt (wie bisher)

    def test_unlesbar(self):
        with self.assertRaises(ValueError):
            zahl("ab", ",")
