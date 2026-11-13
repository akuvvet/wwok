"""Layoutfarbe je Mandant (Verwaltung → Mandanten): Akzentfarbe und Kopfstreifen auf eigenen Seiten und in der Verwaltung."""
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse

from .models import Mandant


@override_settings(SECURE_SSL_REDIRECT=False)
class MandantfarbeTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="ZONE", firmenname="ZONE Handel", farbe="#15803d")
        self.client.force_login(get_user_model().objects.create_superuser("chef", "c@example.test", "x" * 14))
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    def test_eigene_seiten_und_verwaltung_nutzen_die_farbe(self):
        for url in (reverse("dashboard"), reverse("admin:index")):
            r = self.client.get(url)
            self.assertContains(r, "--akzent:#15803d")
            self.assertContains(r, "--mandantfarbe:#15803d")

    def test_ohne_farbe_bleibt_standard(self):
        Mandant.objects.filter(pk=self.m.pk).update(farbe="")
        self.assertNotContains(self.client.get(reverse("dashboard")), "--mandantfarbe")

    def test_nur_gueltige_farbcodes(self):
        self.m.farbe = "grün"
        with self.assertRaises(ValidationError):
            self.m.full_clean()

    def test_verwaltung_hat_farbwaehler(self):
        r = self.client.get(reverse("admin:kern_mandant_change", args=[self.m.pk]))
        self.assertContains(r, 'type="color" name="farbe"')

    def test_pfad_beginnt_nach_der_seitenleiste(self):
        r = self.client.get(reverse("dashboard"))
        self.assertContains(r, '<span class="kopf-links">')
