"""Kostenstelle am Artikel (Buchhaltung): in der Artikelmaske, beim Kopieren und bei Leistungsartikeln."""
from decimal import Decimal

from django.test import TestCase

from . import kopie
from .admin import LeistungsartikelAdmin
from .forms import ARTIKEL_TABS, ArtikelForm
from .models import Artikel, Steuersatz


class KostenstelleTest(TestCase):
    def setUp(self):
        self.st = Steuersatz.objects.create(bezeichnung="19x", prozent=Decimal("19"))
        self.a = Artikel.objects.create(nummer="K1", bezeichnung="Maus", steuersatz=self.st, kostenstelle="100")

    def test_feld_in_der_maske_unter_stammdaten(self):
        grund = next(f for k, _, f in ARTIKEL_TABS if k == "grund")
        self.assertIn("kostenstelle", grund)
        self.assertIn('name="kostenstelle" value="100"', str(ArtikelForm(instance=self.a)["kostenstelle"]))

    def test_kopie_uebernimmt_kostenstelle(self):
        self.assertEqual(kopie.neuer_artikel(self.a).kostenstelle, "100")

    def test_leistungsartikel_verwaltung(self):
        felder = [f for _, opt in LeistungsartikelAdmin.fieldsets for f in opt["fields"]]
        self.assertIn("kostenstelle", felder)
