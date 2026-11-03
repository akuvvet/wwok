from django.contrib import admin
from django.test import SimpleTestCase

from apps.stammdaten.models import Artikel, Leistungsartikel


class AdminAktivSpalteTest(SimpleTestCase):
    """Die Artikelliste zeigt „Aktiv“ (grün/rot) statt „Nicht aktiv“."""

    def test_aktiv_ist_umkehrung_von_nicht_aktiv(self):
        for modell in (Artikel, Leistungsartikel):
            ma = admin.site._registry[modell]
            self.assertIn("aktiv", ma.list_display)
            self.assertNotIn("nicht_aktiv", ma.list_display)
            self.assertTrue(ma.aktiv(modell(nicht_aktiv=False)))
            self.assertFalse(ma.aktiv(modell(nicht_aktiv=True)))
            self.assertTrue(ma.aktiv.boolean)
