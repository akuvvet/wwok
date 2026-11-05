from django.test import TestCase

from .forms import KundeForm
from .models import Kunde, Kundenanrede


class KundeAnredeTest(TestCase):
    def test_anrede_auswahl_und_grunddaten_tab(self):
        form = KundeForm()
        self.assertEqual([w for w, _ in Kundenanrede.choices], ["Firma", "Herr", "Frau"])
        self.assertIn("anrede", form.fields)
        grund = next(f for kennung, _, f in form.TABS if kennung == "grund")
        self.assertIn("anrede", grund)

    def test_speichern(self):
        form = KundeForm({"nummer": "K-ANR-1", "anrede": "Frau", "name1": "Muster", "land": "DE"})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save().anrede, "Frau")

    def test_ungueltige_anrede(self):
        form = KundeForm({"nummer": "K-ANR-2", "anrede": "Dr.", "name1": "Muster", "land": "DE"})
        self.assertFalse(form.is_valid())
        self.assertIn("anrede", form.errors)
