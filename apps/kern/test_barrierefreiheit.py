import re

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from .models import Mandant


@override_settings(SECURE_SSL_REDIRECT=False)
class BarrierefreiheitTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.user = get_user_model().objects.create_superuser("a", "a@a.de", "x" * 14)
        self.client.force_login(self.user)
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    def test_sprunglink_fuehrt_zum_hauptbereich(self):
        html = self.client.get(reverse("dashboard")).content.decode()
        self.assertIn('<a class="sprunglink" href="#inhalt">', html)
        self.assertIn('<main id="inhalt"', html)

    def test_dashboard_aktiver_zeitraum_hat_aria_current(self):
        html = self.client.get(reverse("dashboard")).content.decode()
        self.assertEqual(html.count('aria-current="true"'), 1)

    def test_listenfilter_labels_sind_verknuepft(self):
        for name in ("lager:uebersicht", "einkauf:lieferanten", "einkauf:bestellungen"):
            html = self.client.get(reverse(name)).content.decode()
            main = html[html.index("<main"):]
            for label in re.findall(r"<label\b[^>]*>", main):
                if "for=" in label:
                    ziel = re.search(r'for="([^"]+)"', label).group(1)
                    self.assertIn(f'id="{ziel}"', main, f"{name}: id {ziel} fehlt")
            for label in re.findall(r"<label(?![^>]*for=)[^>]*>(?!\s*<input)[^<]+</label>", main):
                self.fail(f"{name}: Label ohne for: {label}")
