"""Die Versionsnummer steht an einer Stelle und erscheint in der Fußzeile – auch auf der Anmeldeseite."""
import re

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.kern.models import Mandant
from config import __version__


@override_settings(SECURE_SSL_REDIRECT=False)
class VersionTest(TestCase):
    def test_version_hat_drei_stellen(self):
        self.assertRegex(__version__, r"^\d+\.\d+\.\d+$")

    def test_anmeldeseite_zeigt_version(self):
        antwort = self.client.get(reverse("login"))
        self.assertContains(antwort, f"wwok Version {__version__}")

    def test_angemeldet_steht_sie_genau_einmal_in_der_fusszeile(self):
        Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.client.force_login(get_user_model().objects.create_superuser("chef", password="x" * 14))
        html = self.client.get(reverse("dashboard"), follow=True).content.decode()
        self.assertEqual(len(re.findall(r'<footer class="fuss">wwok Version ' + re.escape(__version__) + "</footer>", html)), 1)
