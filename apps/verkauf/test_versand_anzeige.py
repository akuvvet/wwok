"""Versandlabel am Beleg sichtbar: Belegkopf, Aktionsleiste, Kundenansicht und Belegliste."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.kern.models import Mandant
from apps.stammdaten.models import Kunde

from .models import Beleg, Belegart, Position, Versand


@override_settings(SECURE_SSL_REDIRECT=False)
class VersandAnzeigeTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.kunde = Kunde.objects.create(nummer="1", name1="Muster GmbH")
        self.b = Beleg.objects.create(mandant=self.m, art=Belegart.LIEFERSCHEIN, kunde=self.kunde, nummer="LS-1")
        self.ohne = Beleg.objects.create(mandant=self.m, art=Belegart.LIEFERSCHEIN, kunde=self.kunde, nummer="LS-2")
        for beleg in (self.b, self.ohne):
            Position.objects.create(beleg=beleg, bezeichnung="Dienst", einzelpreis=Decimal("1"))
        self.user = get_user_model().objects.create_user("t", password="x" * 14, is_staff=True)
        self.client.force_login(self.user)
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    def test_ohne_versand_kein_hinweis(self):
        r = self.client.get(reverse("verkauf:beleg", args=[self.ohne.pk]))
        self.assertNotContains(r, "vorhanden ·")
        self.assertContains(r, 'data-tip="Versand: Paket oder Brief"')

    def test_label_am_beleg_und_in_listen_sichtbar(self):
        v = Versand.objects.create(beleg=self.b, sendungsnummer="00340434161094042557", label=b"%PDF-1.4 x", dhl_erzeugt=True)
        url = reverse("verkauf:beleg", args=[self.b.pk])
        r = self.client.get(url)
        self.assertContains(r, "DHL-Label vorhanden · DHL Paket (ab 1 kg) · 00340434161094042557")
        self.assertContains(r, "noch nicht gedruckt")
        self.assertContains(r, 'data-tip="Versand (Label vorhanden)"')
        self.client.get(reverse("verkauf:versand_aufkleber", args=[self.b.pk, v.pk]))  # Label öffnen/drucken
        v.refresh_from_db()
        self.assertIsNotNone(v.gedruckt_am)
        self.assertContains(self.client.get(url), "gedruckt ")
        for liste in (reverse("verkauf:belege"), reverse("verkauf:kunde", args=[self.kunde.pk])):
            inhalt = self.client.get(liste).content.decode()
            self.assertEqual(inhalt.count('title="Versandlabel vorhanden"'), 1, liste)  # nur LS-1, nicht LS-2
