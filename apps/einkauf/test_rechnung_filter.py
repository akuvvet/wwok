"""Lieferantenrechnungen: Filter Nicht bezahlt (offen + exportiert), SEPA erstellt/nicht gebucht, Bezahlt, Alle."""
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.kern.models import Mandant
from apps.stammdaten.models import Lieferant

from .models import Lieferantenrechnung, RechnungStatus


@override_settings(SECURE_SSL_REDIRECT=False)
class RechnungFilterTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        lf = Lieferant.objects.create(nummer="L1", name1="API GmbH", kurzname="API")
        for nummer, status in (("R-OFFEN", RechnungStatus.OFFEN), ("R-SEPA", RechnungStatus.EXPORTIERT), ("R-BEZAHLT", RechnungStatus.BEZAHLT)):
            Lieferantenrechnung.objects.create(mandant=self.m, lieferant=lf, rechnungsnummer=nummer, datum=date(2026, 11, 1), faellig=date(2026, 11, 15),
                                               netto=Decimal("10"), brutto=Decimal("11.90"), status=status)
        self.client.force_login(get_user_model().objects.create_superuser("t", "t@example.test", "x" * 14))
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    def nummern(self, status):
        r = self.client.get(reverse("einkauf:rechnungen"), {"status": status})
        return sorted(z[0].rechnungsnummer for z in r.context["zeilen"])

    def test_filter(self):
        self.assertEqual(self.nummern("offen"), ["R-OFFEN", "R-SEPA"])  # nicht bezahlt schließt exportierte ein
        self.assertEqual(self.nummern("exportiert"), ["R-SEPA"])
        self.assertEqual(self.nummern("bezahlt"), ["R-BEZAHLT"])
        self.assertEqual(self.nummern("alle"), ["R-BEZAHLT", "R-OFFEN", "R-SEPA"])

    def test_knopf_vorhanden(self):
        r = self.client.get(reverse("einkauf:rechnungen"))
        self.assertContains(r, 'href="?status=exportiert"')
        self.assertContains(r, "SEPA erstellt, nicht gebucht")
