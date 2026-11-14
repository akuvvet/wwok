"""Prüfbericht C, Lager: Seriennummern wandern bei der Umbuchung zwischen Mandanten mit."""
from decimal import Decimal

from django.test import TestCase

from apps.kern.models import Mandant
from apps.stammdaten.models import Artikel, Steuersatz

from .models import Bewegungsart, Seriennummer, bestand
from .services import buche_manuell, seriennummern_einlagern, umbuchen


class UmbuchungSeriennummernTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.m2 = Mandant.objects.create(kurzname="ZONE", firmenname="ZONE Handel")
        st = Steuersatz.objects.create(bezeichnung="19", prozent=Decimal("19"))
        self.a = Artikel.objects.create(nummer="A1", bezeichnung="Notebook", steuersatz=st, vk1=Decimal("500"))
        buche_manuell(self.m, self.a, Bewegungsart.WARENEINGANG, Decimal("3"))
        seriennummern_einlagern(self.m, self.a, "SN1\nSN2\nSN3")

    def im_lager(self, mandant):
        return sorted(Seriennummer.objects.filter(mandant=mandant, artikel=self.a, status=Seriennummer.IM_LAGER).values_list("nummer", flat=True))

    def test_genannte_nummern_wandern_mit(self):
        umbuchen(self.m, self.m2, self.a, Decimal("2"), seriennummern=["SN1", "SN3"])
        self.assertEqual((bestand(self.m, self.a), bestand(self.m2, self.a)), (Decimal("1"), Decimal("2")))
        self.assertEqual((self.im_lager(self.m), self.im_lager(self.m2)), (["SN2"], ["SN1", "SN3"]))

    def test_ohne_nummern_waeren_sie_verwaist_nichts_wird_gebucht(self):
        with self.assertRaises(ValueError) as fehler:
            umbuchen(self.m, self.m2, self.a, Decimal("2"))  # 1 Stück bleibt, 3 Nummern lägen im Lager
        self.assertIn("Seriennummer", str(fehler.exception))
        self.assertEqual((bestand(self.m, self.a), bestand(self.m2, self.a)), (Decimal("3"), Decimal("0")))
        self.assertEqual(self.im_lager(self.m), ["SN1", "SN2", "SN3"])

    def test_artikel_ohne_nummern_und_rest_passt_geht_wie_bisher(self):
        buche_manuell(self.m, self.a, Bewegungsart.WARENEINGANG, Decimal("5"))  # 8 Stück, nur 3 mit Nummer
        umbuchen(self.m, self.m2, self.a, Decimal("4"))
        self.assertEqual((bestand(self.m, self.a), bestand(self.m2, self.a)), (Decimal("4"), Decimal("4")))
        self.assertEqual(self.im_lager(self.m), ["SN1", "SN2", "SN3"])

    def test_unbekannte_oder_verkaufte_nummer_und_zu_viele_nummern(self):
        with self.assertRaises(ValueError):
            umbuchen(self.m, self.m2, self.a, Decimal("1"), seriennummern=["XX"])
        Seriennummer.objects.filter(nummer="SN1").update(status=Seriennummer.VERKAUFT)
        with self.assertRaises(ValueError):
            umbuchen(self.m, self.m2, self.a, Decimal("1"), seriennummern=["SN1"])
        with self.assertRaises(ValueError):
            umbuchen(self.m, self.m2, self.a, Decimal("1"), seriennummern=["SN2", "SN3"])  # mehr Nummern als Menge
        self.assertEqual((bestand(self.m, self.a), bestand(self.m2, self.a)), (Decimal("3"), Decimal("0")))

    def test_zurueckbuchen_belebt_die_alte_nummer(self):
        umbuchen(self.m, self.m2, self.a, Decimal("3"), seriennummern=["SN1", "SN2", "SN3"])
        umbuchen(self.m2, self.m, self.a, Decimal("1"), seriennummern=["SN2"])
        self.assertEqual((self.im_lager(self.m), self.im_lager(self.m2)), (["SN2"], ["SN1", "SN3"]))
        self.assertEqual(Seriennummer.objects.filter(nummer="SN2").count(), 1)
