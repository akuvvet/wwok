"""Entscheidungen Prüfbericht C, Punkt 8: Kassenbuch ohne Minus und ohne rückwirkende oder zukünftige Buchungen."""
from datetime import date, timedelta
from decimal import Decimal

from django.test import TestCase

from apps.kern.models import Mandant
from apps.stammdaten.models import Kunde, Zahlungsbedingung
from apps.verkauf.models import Beleg, Belegart, Position

from . import services
from .models import Kassenbuch, Zahlung


class KassenbuchRegelnTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.heute = date.today()
        kunde = Kunde.objects.create(nummer="1", name1="Kunde GmbH")
        zb = Zahlungsbedingung.objects.create(bezeichnung="10 Tage", tage_netto=10, tage_mahnung=11)
        self.b = Beleg.objects.create(mandant=self.m, art=Belegart.RECHNUNG, kunde=kunde, zahlungsbedingung=zb)
        Position.objects.create(beleg=self.b, bezeichnung="Dienst", einzelpreis=Decimal("100"))
        self.b.abschliessen()
        self.b.refresh_from_db()

    # a) kein Minus
    def test_ausgabe_im_rahmen_des_bestands_geht(self):
        services.kasse_buchen(self.m, "E", Decimal("50"), "Einlage")
        services.kasse_buchen(self.m, "A", Decimal("50"), "Porto")
        self.assertEqual(services.kassenstand(self.m), Decimal("0"))

    def test_ausgabe_ueber_bestand_wird_abgelehnt(self):
        services.kasse_buchen(self.m, "E", Decimal("10"), "Einlage")
        with self.assertRaisesMessage(ValueError, "nicht ins Minus"):
            services.kasse_buchen(self.m, "A", Decimal("10.01"), "Porto")
        self.assertEqual(Kassenbuch.objects.count(), 1)

    def test_barrueckzahlung_ins_minus_wird_abgelehnt_und_nichts_gebucht(self):
        services.zahlung_buchen(self.b, Decimal("119"), self.heute, "ba")
        services.kasse_buchen(self.m, "A", Decimal("100"), "Entnahme")
        with self.assertRaisesMessage(ValueError, "nicht ins Minus"):
            services.zahlung_buchen(self.b, Decimal("-50"), self.heute, "ba")
        self.assertEqual(Zahlung.objects.count(), 1)  # die Zahlung wurde mit zurückgerollt
        self.assertEqual(services.kassenstand(self.m), Decimal("19.00"))

    def test_storno_einer_barzahlung_ins_minus_wird_abgelehnt(self):
        z = services.zahlung_buchen(self.b, Decimal("119"), self.heute, "ba")
        services.kasse_buchen(self.m, "A", Decimal("100"), "Entnahme")
        with self.assertRaisesMessage(ValueError, "nicht ins Minus"):
            services.zahlung_stornieren(z, "Irrtum")
        self.assertEqual(Zahlung.objects.count(), 1)
        self.assertEqual(Kassenbuch.objects.count(), 2)

    def test_storno_einer_barzahlung_mit_deckung_geht(self):
        z = services.zahlung_buchen(self.b, Decimal("119"), self.heute, "ba")
        services.zahlung_stornieren(z, "Irrtum")
        self.assertEqual(services.kassenstand(self.m), Decimal("0.00"))

    def test_bestand_gilt_je_mandant(self):
        anderer = Mandant.objects.create(kurzname="ZONE", firmenname="ZONE Handel")
        services.kasse_buchen(anderer, "E", Decimal("500"), "Einlage")
        with self.assertRaises(ValueError):
            services.kasse_buchen(self.m, "A", Decimal("1"), "Porto")

    # b) kein rückwirkendes oder zukünftiges Datum
    def test_datum_vor_letzter_buchung_wird_abgelehnt(self):
        services.kasse_buchen(self.m, "E", Decimal("50"), "Einlage", self.heute)
        with self.assertRaisesMessage(ValueError, "vor der letzten Buchung"):
            services.kasse_buchen(self.m, "E", Decimal("5"), "Nachtrag", self.heute - timedelta(days=1))

    def test_datum_gleich_letzter_buchung_geht(self):
        services.kasse_buchen(self.m, "E", Decimal("50"), "Einlage", self.heute)
        services.kasse_buchen(self.m, "E", Decimal("5"), "Noch eine", self.heute)
        self.assertEqual(Kassenbuch.objects.count(), 2)

    def test_erste_buchung_darf_in_der_vergangenheit_liegen(self):
        # Die Regel prüft nur gegen die letzte vorhandene Buchung, nicht gegen heute
        services.kasse_buchen(self.m, "E", Decimal("50"), "Anfangsbestand", self.heute - timedelta(days=30))
        services.kasse_buchen(self.m, "E", Decimal("5"), "Später", self.heute - timedelta(days=10))
        self.assertEqual(Kassenbuch.objects.count(), 2)

    def test_zukunft_wird_abgelehnt(self):
        with self.assertRaisesMessage(ValueError, "Zukunft"):
            services.kasse_buchen(self.m, "E", Decimal("5"), "Später", self.heute + timedelta(days=1))

    def test_barzahlung_mit_altem_datum_nach_neuerer_buchung_wird_abgelehnt(self):
        services.kasse_buchen(self.m, "E", Decimal("10"), "Einlage", self.heute)
        with self.assertRaisesMessage(ValueError, "vor der letzten Buchung"):
            services.zahlung_buchen(self.b, Decimal("119"), self.heute - timedelta(days=3), "ba")
        self.assertFalse(Zahlung.objects.exists())

    def test_barzahlung_in_der_zukunft_wird_abgelehnt(self):
        with self.assertRaisesMessage(ValueError, "Zukunft"):
            services.zahlung_buchen(self.b, Decimal("119"), self.heute + timedelta(days=2), "ba")
        self.assertFalse(Zahlung.objects.exists())

    def test_ueberweisung_mit_altem_datum_bleibt_moeglich(self):
        services.kasse_buchen(self.m, "E", Decimal("10"), "Einlage", self.heute)
        services.zahlung_buchen(self.b, Decimal("119"), self.heute - timedelta(days=3), "ue")
        self.assertEqual(Zahlung.objects.count(), 1)
