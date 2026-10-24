from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.kern.models import Mandant
from apps.stammdaten.models import Lieferant

from . import services
from .models import RechnungStatus


@override_settings(SECURE_SSL_REDIRECT=False)
class EinkaufStornoLoeschenTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.lf = Lieferant.objects.create(nummer="L1", name1="Techdata GmbH", iban="DE89370400440532013000")
        self.client.force_login(get_user_model().objects.create_user("t", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    def test_lieferantenzahlung_stornieren_mit_grund(self):
        r = services.rechnung_erfassen(self.m, self.lf, "R1", date.today(), Decimal("100"), Decimal("119"))
        self.client.post(reverse("einkauf:rechnung_bezahlt", args=[r.pk]), {"datum": date.today().isoformat(), "betrag": "119"})
        r.refresh_from_db()
        self.assertEqual(r.status, RechnungStatus.BEZAHLT)
        self.client.post(reverse("einkauf:rechnung_zahlung_storno", args=[r.pk]), {"grund": ""})
        r.refresh_from_db()
        self.assertEqual(r.status, RechnungStatus.BEZAHLT)  # ohne Grund nicht möglich
        self.client.post(reverse("einkauf:rechnung_zahlung_storno", args=[r.pk]), {"grund": "Rücklastschrift"})
        r.refresh_from_db()
        self.assertEqual((r.status, r.bezahlt_am), (RechnungStatus.OFFEN, None))
        self.assertIn("Rücklastschrift", r.historie)
        self.assertContains(self.client.get(reverse("einkauf:rechnung", args=[r.pk])), "Rücklastschrift")

    def test_bestellung_loeschen(self):
        b = services.bestellung_anlegen(self.m, self.lf)
        self.client.post(reverse("einkauf:bestellung_loeschen", args=[b.pk]))
        self.assertFalse(services.Bestellung.objects.filter(pk=b.pk).exists())
        b2 = services.bestellung_anlegen(self.m, self.lf)
        services.wareneingang_anlegen(self.m, self.lf, b2)
        self.client.post(reverse("einkauf:bestellung_loeschen", args=[b2.pk]))
        self.assertTrue(services.Bestellung.objects.filter(pk=b2.pk).exists())  # mit Wareneingang nicht löschbar
