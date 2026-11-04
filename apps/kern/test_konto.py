from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import NoReverseMatch, reverse


@override_settings(SECURE_SSL_REDIRECT=False)
class KontoTest(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("t", "t@example.test", "x" * 14)

    def test_kein_passwort_reset_per_mail(self):
        for name in ("password_reset", "password_reset_done", "password_reset_confirm", "password_reset_complete"):
            with self.assertRaises(NoReverseMatch):
                reverse(name, kwargs={"uidb64": "x", "token": "y"} if name == "password_reset_confirm" else None)
        for adresse in ("/konto/password_reset/", "/konto/password_reset/done/", "/konto/reset/done/", "/konto/reset/x/y/"):
            self.assertEqual(self.client.get(adresse).status_code, 404, adresse)
        self.assertEqual(self.client.post("/konto/password_reset/", {"email": "t@example.test"}).status_code, 404)
        self.assertEqual(mail.outbox, [])

    def test_admin_anmeldung_ohne_link_passwort_vergessen(self):
        r = self.client.get(reverse("admin:login"))
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, "password_reset")

    def test_anmelden_abmelden_und_passwort_aendern_bleiben(self):
        self.assertEqual(self.client.get(reverse("login")).status_code, 200)
        self.assertRedirects(self.client.get(reverse("password_change")),
                             f"{reverse('login')}?next={reverse('password_change')}", fetch_redirect_response=False)
        self.client.force_login(self.user)
        r = self.client.post(reverse("password_change"), {"old_password": "x" * 14, "new_password1": "Neu-Passwort-2026!",
                                                          "new_password2": "Neu-Passwort-2026!"})
        self.assertRedirects(r, reverse("password_change_done"))
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("Neu-Passwort-2026!"))
        self.assertEqual(self.client.post(reverse("logout")).status_code, 302)
