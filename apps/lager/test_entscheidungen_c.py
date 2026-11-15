"""Prüfbericht Abschnitt C, Punkt 10: Ø-EK bei Umbuchung und manueller Buchung."""
from decimal import Decimal

from django.test import TestCase

from apps.kern.models import Mandant
from apps.stammdaten.models import Artikel, ArtikelMandant, Steuersatz

from .models import Bewegungsart, BestandReichtNicht, bestand
from .services import buche_manuell, umbuchen


class OeEkTest(TestCase):
    def setUp(self):
        self.a = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.b = Mandant.objects.create(kurzname="ZONE", firmenname="ZONE Handel")
        st = Steuersatz.objects.create(bezeichnung="19", prozent=Decimal("19"))
        self.art = Artikel.objects.create(nummer="X1", bezeichnung="Ding", steuersatz=st, vk1=Decimal("20"))

    def avg(self, mandant):
        am = ArtikelMandant.objects.filter(artikel=self.art, mandant=mandant).first()
        return am.ek_avg if am else None

    def test_manueller_zugang_mit_preis_schreibt_fort(self):
        buche_manuell(self.a, self.art, Bewegungsart.WARENEINGANG, Decimal("10"), ek=Decimal("10"))
        self.assertEqual(self.avg(self.a), Decimal("10.0000"))
        buche_manuell(self.a, self.art, Bewegungsart.WARENEINGANG, Decimal("10"), ek=Decimal("20"))
        self.assertEqual(self.avg(self.a), Decimal("15.0000"))
        self.assertEqual(bestand(self.a, self.art), Decimal("20"))

    def test_ohne_preis_bleibt_alles_wie_bisher(self):
        buche_manuell(self.a, self.art, Bewegungsart.WARENEINGANG, Decimal("5"))
        self.assertIsNone(self.avg(self.a))

    def test_negativer_preis_und_abgang_mit_preis_abgelehnt(self):
        with self.assertRaises(ValueError):
            buche_manuell(self.a, self.art, Bewegungsart.WARENEINGANG, Decimal("5"), ek=Decimal("-1"))
        buche_manuell(self.a, self.art, Bewegungsart.WARENEINGANG, Decimal("5"))
        with self.assertRaises(ValueError):
            buche_manuell(self.a, self.art, Bewegungsart.EIGENVERBRAUCH, Decimal("1"), ek=Decimal("3"))
        self.assertEqual(bestand(self.a, self.art), Decimal("5"))
        self.assertIsNone(self.avg(self.a))

    def test_umbuchung_uebernimmt_oe_ek_gewogen(self):
        buche_manuell(self.a, self.art, Bewegungsart.WARENEINGANG, Decimal("10"), ek=Decimal("12"))
        buche_manuell(self.b, self.art, Bewegungsart.WARENEINGANG, Decimal("10"), ek=Decimal("20"))
        umbuchen(self.a, self.b, self.art, Decimal("10"))
        self.assertEqual(self.avg(self.b), Decimal("16.0000"))
        self.assertEqual(self.avg(self.a), Decimal("12.0000"))

    def test_umbuchung_in_leeres_ziel_und_ohne_quell_ek(self):
        buche_manuell(self.a, self.art, Bewegungsart.WARENEINGANG, Decimal("4"), ek=Decimal("7"))
        umbuchen(self.a, self.b, self.art, Decimal("2"))
        self.assertEqual(self.avg(self.b), Decimal("7.0000"))
        # Quelle ohne Ø-EK: Ziel bleibt unverändert
        art2 = Artikel.objects.create(nummer="X2", bezeichnung="Zwei", steuersatz=self.art.steuersatz, vk1=Decimal("1"))
        buche_manuell(self.a, art2, Bewegungsart.WARENEINGANG, Decimal("3"))
        umbuchen(self.a, self.b, art2, Decimal("1"))
        self.assertFalse(ArtikelMandant.objects.filter(artikel=art2, mandant=self.b, ek_avg__isnull=False).exists())

    def test_umbuchung_ohne_bestand_aendert_nichts(self):
        buche_manuell(self.a, self.art, Bewegungsart.WARENEINGANG, Decimal("1"), ek=Decimal("7"))
        with self.assertRaises(BestandReichtNicht):
            umbuchen(self.a, self.b, self.art, Decimal("5"))
        self.assertIsNone(self.avg(self.b))
