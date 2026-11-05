"""Preislistenartikel-Liste: Sortierung nach lieferant_id/bezeichnung und Lieferantenfilter ohne distinct-Join (Issue 46)."""
from decimal import Decimal as D

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.kern.models import Mandant
from apps.stammdaten.models import Lieferant, Preislistenartikel


@override_settings(SECURE_SSL_REDIRECT=False)
class PreislistenartikelListeTest(TestCase):
    def setUp(self):
        m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.zeta = Lieferant.objects.create(nummer="70100", name1="Zeta GmbH", kurzname="Zeta")
        self.api = Lieferant.objects.create(nummer="70200", name1="API GmbH", kurzname="API")
        self.leer = Lieferant.objects.create(nummer="70300", name1="Leer GmbH", kurzname="Leer")
        for lf, nr, bez in [(self.api, "2", "Kabel"), (self.zeta, "1", "Zange"), (self.api, "1", "Adapter"), (self.zeta, "2", "Anker")]:
            Preislistenartikel.objects.create(lieferant=lf, bestellnummer=nr, bezeichnung=bez, ek=D("1"))
        self.client.force_login(get_user_model().objects.create_superuser("chef", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = m.pk
        s.save()
        self.url = reverse("einkauf:preislistenartikel")

    def test_sortierung_lieferant_dann_bezeichnung(self):
        r = self.client.get(self.url)
        erwartet = list(Preislistenartikel.objects.order_by("lieferant_id", "bezeichnung").values_list("pk", flat=True))
        self.assertEqual([e.pk for e in r.context["seite"]], erwartet)
        self.assertEqual(len(erwartet), 4)

    def test_lieferantenfilter_nur_mit_artikeln_nach_kurzname(self):
        r = self.client.get(self.url)
        self.assertEqual([lf.kurz for lf in r.context["lieferanten"]], ["API", "Zeta"])

    def test_filter_nach_lieferant(self):
        r = self.client.get(self.url, {"lieferant": self.api.pk})
        self.assertEqual(sorted(e.bezeichnung for e in r.context["seite"]), ["Adapter", "Kabel"])
