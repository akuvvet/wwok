from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse


class VerwaltungKopfTest(TestCase):
    """Verwaltung: Kopf und Brotkrümel stehen fest, „Speichern“ rechts in den Brotkrümeln."""

    def setUp(self):
        self.admin = get_user_model().objects.create_superuser("admin", "a@a.de", "x" * 14)
        self.client.force_login(self.admin)

    def test_brotleiste_fest_mit_speichern_skript(self):
        html = self.client.get(reverse("admin:auth_user_change", args=[self.admin.pk])).content.decode()
        self.assertRegex(html, r'<div id="wwok-brotleiste">\s*<nav')
        self.assertIn("#container { height:100vh;", html)
        self.assertIn("knopf.textContent = 'Speichern'", html)
        self.assertIn('name="_save"', html)  # Ziel des Knopfs bleibt Djangos eigener Speichern-Knopf
        self.assertIn('wwok Warenwirtschaft', html)  # Kopf aus admin.site.site_header bleibt

    def test_anmeldung_ohne_brotleiste(self):
        self.client.logout()
        r = self.client.get(reverse("admin:login"))
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, 'id="wwok-brotleiste"')

    def test_startseite_hat_feste_titelleiste_mit_menusuche(self):
        html = self.client.get(reverse("admin:index")).content.decode()
        self.assertRegex(html, r'<div id="wwok-titelleiste"><h1>Verwaltung</h1><input[^>]*id="wwok-menusuche"')
        self.assertNotIn("#wwok-titelleiste { position:sticky;", html)  # Titelleiste scrollt nicht
        self.assertIn("#content > #content-main { flex:1 1 auto; min-height:0; overflow:auto; }", html)  # nur Inhalt scrollt
        self.assertIn("html, body { height:100%; overflow:hidden; }", html)  # keine Seiten-Scrollleiste
        self.assertIn("#container > .main { flex:1 1 0; min-height:0; overflow:hidden; }", html)  # überschreibt Djangos flex-shrink:0
        self.assertRegex(html, r"#wwok-menusuche \{[^}]*margin-left:auto;")  # Suchfeld rechts in der Leiste

    def test_andere_seiten_ohne_menusuche(self):
        html = self.client.get(reverse("admin:auth_user_change", args=[self.admin.pk])).content.decode()
        self.assertNotIn('id="wwok-menusuche"', html)
