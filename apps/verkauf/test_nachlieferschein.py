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
        # Ohne Antwort wird nicht gedruckt; Abschluss fragt erst nach
        from apps.lager.services import druckhindernis
        self.assertIn("abgebucht", druckhindernis(nl))

    def test_abbuchen_nur_bei_ersatz(self):
        from apps.lager.models import BestandReichtNicht, Bewegungsart, Lagerbewegung
        from apps.lager.services import buche_manuell
        re = self.rechnung(True)
        nein = re.kopieren_als(Belegart.NACHLIEFERSCHEIN)
        nein.nl_lager_abbuchen = False
        nein.abschliessen()
        self.assertFalse(Lagerbewegung.objects.filter(beleg=nein).exists())
        ja = re.kopieren_als(Belegart.NACHLIEFERSCHEIN)
        ja.nl_lager_abbuchen = True
        Beleg.objects.filter(pk=ja.pk).update(nl_lager_abbuchen=True)
        with self.assertRaises(BestandReichtNicht):  # kein Bestand → keine Abbuchung unter 0
            ja.abschliessen()
        buche_manuell(self.m, self.maus, Bewegungsart.WARENEINGANG, Decimal("3"), "Test")
        ja = Beleg.objects.get(pk=ja.pk)  # Zustand nach dem abgebrochenen Abschluss
        ja.abschliessen()
        self.assertEqual(Lagerbewegung.objects.filter(beleg=ja, art=Bewegungsart.VERKAUF).count(), 1)

    def test_nur_aus_abgeschlossener_rechnung(self):
        with self.assertRaises(ValueError):
            self.rechnung(False).kopieren_als(Belegart.NACHLIEFERSCHEIN)
        an = Beleg.objects.create(mandant=self.m, art=Belegart.ANGEBOT, kunde=self.kunde)
        with self.assertRaises(ValueError):
            an.kopieren_als(Belegart.NACHLIEFERSCHEIN)
