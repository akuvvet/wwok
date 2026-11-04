from datetime import date
from decimal import Decimal

from django.test import TestCase

from apps.kern.models import Mandant
from apps.lager.models import Bewegungsart
from apps.lager.services import buche_manuell
from apps.stammdaten.models import Artikel, Kunde, Steuersatz

from .models import Beleg, Belegart, Position, rechnung_aus_lieferscheinen


class LieferdatumRechnungTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.kunde = Kunde.objects.create(nummer="1", name1="Kunde GmbH")
        st = Steuersatz.objects.create(bezeichnung="19", prozent=Decimal("19"))
        self.a = Artikel.objects.create(nummer="A1", bezeichnung="Maus", steuersatz=st, vk1=Decimal("10"))
        buche_manuell(self.m, self.a, Bewegungsart.WARENEINGANG, Decimal("20"))

    def lieferschein(self, datum):
        ls = Beleg.objects.create(mandant=self.m, art=Belegart.LIEFERSCHEIN, kunde=self.kunde, belegdatum=datum)
        Position.objects.create(beleg=ls, artikel=self.a, bezeichnung="Maus", menge=Decimal("1"), einzelpreis=Decimal("10"))
        ls.abschliessen()
        return ls

    def test_rechnung_ohne_lieferschein_lieferdatum_gleich_rechnungsdatum(self):
        re = Beleg.objects.create(mandant=self.m, art=Belegart.RECHNUNG, kunde=self.kunde, belegdatum=date(2026, 5, 4))
        Position.objects.create(beleg=re, artikel=self.a, bezeichnung="Maus", menge=Decimal("1"), einzelpreis=Decimal("10"))
        self.assertEqual(re.lieferdatum, date(2026, 5, 4))

    def test_ein_lieferschein_uebernimmt_dessen_datum(self):
        ls = self.lieferschein(date(2026, 3, 1))
        re = rechnung_aus_lieferscheinen([ls])
        self.assertEqual(re.lieferdatum, date(2026, 3, 1))

    def test_sammelrechnung_kein_kopfdatum_positionen_tragen_lieferschein(self):
        l1, l2 = self.lieferschein(date(2026, 3, 1)), self.lieferschein(date(2026, 3, 9))
        re = rechnung_aus_lieferscheinen([l1, l2])
        self.assertIsNone(re.lieferdatum)
        self.assertEqual({p.lieferschein.belegdatum for p in re.positionen.all()}, {date(2026, 3, 1), date(2026, 3, 9)})

    def test_andere_belegarten_ohne_lieferdatum(self):
        ls = self.lieferschein(date(2026, 3, 1))
        self.assertIsNone(ls.lieferdatum)
