from decimal import Decimal

from django.test import TestCase

from apps.kern.models import Mandant
from apps.stammdaten.models import Artikel, Kunde, Steuersatz

from .models import Beleg, Belegart, Position


class NachlieferscheinTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.kunde = Kunde.objects.create(nummer="1", name1="Kunde GmbH")
        self.maus = Artikel.objects.create(nummer="M1", bezeichnung="Maus", steuersatz=Steuersatz.objects.get(nummer=1), vk1=Decimal("20"))

    def rechnung(self, abgeschlossen):
        re = Beleg.objects.create(mandant=self.m, art=Belegart.RECHNUNG, kunde=self.kunde, versandkosten=Decimal("5"))
        Position.objects.create(beleg=re, artikel=self.maus, bezeichnung="Maus", menge=1, einzelpreis=Decimal("20"), steuer_prozent=19)
        if abgeschlossen:
            re.abgeschlossen = True
            re.nummer = "RE2026-00001"
            Beleg.objects.filter(pk=re.pk).update(abgeschlossen=True, nummer=re.nummer)
        return re

    def test_nachlieferschein_aus_rechnung(self):
        re = self.rechnung(True)
        nl = re.kopieren_als(Belegart.NACHLIEFERSCHEIN)
        self.assertTrue(nl.nummer.startswith("NL"))  # eigener Nummernkreis, sofort vergeben
        self.assertEqual(nl.zahlungsbedingung.bezeichnung, "Betrag dankend erhalten")
        self.assertEqual((nl.versandkosten, nl.positionen.count(), nl.vorgaenger), (0, 1, re))
        self.assertIn("RE2026-00001", nl.bemerkung)
        nl.abschliessen()  # bucht keinen Bestand (mit der Rechnung schon abgebucht)
        self.assertTrue(nl.abgeschlossen)
        self.assertFalse(nl.lagerbewegung_set.exists() if hasattr(nl, "lagerbewegung_set") else False)

    def test_nur_aus_abgeschlossener_rechnung(self):
        with self.assertRaises(ValueError):
            self.rechnung(False).kopieren_als(Belegart.NACHLIEFERSCHEIN)
        an = Beleg.objects.create(mandant=self.m, art=Belegart.ANGEBOT, kunde=self.kunde)
        with self.assertRaises(ValueError):
            an.kopieren_als(Belegart.NACHLIEFERSCHEIN)
