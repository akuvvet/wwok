"""Prüfbericht 14.11.2026, Abschnitt C: Proforma-Storno einer vorläufigen (nicht gespeicherten) Proforma ist auch serverseitig gesperrt."""
from decimal import Decimal

from django.test import TestCase

from apps.kern.models import Mandant
from apps.stammdaten.models import Kunde
from apps.verkauf.models import Beleg, Belegart, Position

from . import anzahlung


class ProformaStornoVorlaeufigTest(TestCase):
    def test_vorlaeufige_proforma_laesst_sich_nicht_stornieren(self):
        m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        kunde = Kunde.objects.create(nummer="1", name1="Kunde GmbH")
        pf = Beleg.objects.create(mandant=m, art=Belegart.PROFORMA, kunde=kunde, vorlaeufig=True)
        Position.objects.create(beleg=pf, bezeichnung="Dienst", einzelpreis=Decimal("100"))
        with self.assertRaises(ValueError):
            anzahlung.proforma_stornieren(pf, "Kunde kauft nicht")
        pf.refresh_from_db()
        self.assertFalse(pf.abgeschlossen)
        self.assertFalse(Beleg.objects.filter(art=Belegart.PROFORMA_STORNO).exists())
