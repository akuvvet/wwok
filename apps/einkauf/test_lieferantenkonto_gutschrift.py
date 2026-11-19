"""Lieferanten-Gutschriften erscheinen im Lieferantenkonto (Belegart „Lieferanten-Gutschrift“), offen = Restguthaben > 0."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.kern.models import Mandant
from apps.stammdaten.models import Lieferant

from . import lieferantenkonto, services


@override_settings(SECURE_SSL_REDIRECT=False)
class GutschriftImLieferantenkontoTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.m2 = Mandant.objects.create(kurzname="ZONE", firmenname="ZONE Handel")
        self.lf = Lieferant.objects.create(nummer="L1", name1="Kosatec Computer GmbH", kurzname="KOSATEC")
        self.heute = timezone.localdate()
        user = get_user_model().objects.create_user("t", password="x" * 14, is_superuser=True, is_staff=True)
        self.client.force_login(user)
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    def gutschrift(self, nr, mandant=None):
        return services.gutschrift_erfassen(mandant or self.m, self.lf, nr, self.heute, Decimal("100"), Decimal("19"), Decimal("119"))

    def test_offene_gutschrift_im_konto_und_kopf(self):
        g = self.gutschrift("GS-1")
        self.gutschrift("GS-FREMD", mandant=self.m2)
        seite = self.client.get(reverse("einkauf:lieferant", args=[self.lf.pk]))
        zeilen = [z for z in seite.context["seite"].object_list if z["art"] == "LG"]
        self.assertEqual([z["nummer"] for z in zeilen], ["GS-1"])  # nur eigener Mandant
        self.assertEqual(zeilen[0]["betrag"], Decimal("119"))
        self.assertEqual(seite.context["offen_gutschriften"], Decimal("119"))
        self.assertContains(seite, reverse("einkauf:gutschrift", args=[g.pk]))
        self.assertContains(seite, "Offenes Guthaben")
        self.assertContains(seite, "Lieferanten-Gutschrift")  # im Filter Belegart

    def test_teilweise_ausgezahlt_zeigt_rest_voll_ausgezahlt_nur_bei_alle(self):
        g = self.gutschrift("GS-2")
        services.gutschrift_zahlung_buchen(g, Decimal("19"))
        zeile = lieferantenkonto.belege(self.m, self.lf, art="LG")[0]
        self.assertEqual(zeile["betrag"], Decimal("100"))
        self.assertEqual(zeile["status"], "offen")
        z2 = services.gutschrift_zahlung_buchen(g, Decimal("100"))
        self.assertEqual(lieferantenkonto.belege(self.m, self.lf, art="LG"), [])  # erledigt: nicht mehr offen
        alle = lieferantenkonto.belege(self.m, self.lf, nur_offen=False, art="LG")
        self.assertEqual(alle[0]["status"], "erledigt")
        seite = self.client.get(reverse("einkauf:lieferant", args=[self.lf.pk]))
        self.assertNotContains(seite, "Offenes Guthaben")
        services.gutschrift_zahlung_stornieren(z2, "falsch gebucht")  # nach Storno wieder offen
        self.assertEqual(lieferantenkonto.belege(self.m, self.lf, art="LG")[0]["betrag"], Decimal("100"))

    def test_offene_gutschrift_in_uebersicht_lieferant(self):
        self.gutschrift("GS-3")
        seite = self.client.get(reverse("einkauf:lieferanten"))
        self.assertIn(("LG", "GS-3"), {(z["art"], z["nummer"]) for z in seite.context["offene"]})
