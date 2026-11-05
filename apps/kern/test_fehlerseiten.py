import logging

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.test import Client, TestCase, override_settings
from django.urls import path


def _403(request):
    raise PermissionDenied


def _500(request):
    raise RuntimeError("x")


urlpatterns = [path("t403/", _403), path("t500/", _500)]


@override_settings(DEBUG=False, SECURE_SSL_REDIRECT=False)
class FehlerseitenTest(TestCase):
    def test_404(self):
        r = self.client.get("/gibt-es-nicht/")
        self.assertContains(r, "Seite nicht gefunden", status_code=404)

    @override_settings(ROOT_URLCONF="apps.kern.test_fehlerseiten")
    def test_403(self):
        r = self.client.get("/t403/")
        self.assertContains(r, "Zugriff verweigert", status_code=403)

    @override_settings(ROOT_URLCONF="apps.kern.test_fehlerseiten")
    def test_500(self):
        c = Client(raise_request_exception=False)
        with self.assertLogs("django.request", "ERROR"):
            r = c.get("/t500/")
        self.assertContains(r, "Serverfehler", status_code=500)


@override_settings(SECURE_SSL_REDIRECT=False)
class FehlanmeldungTest(TestCase):
    def test_log_ohne_passwort(self):
        get_user_model().objects.create_user("t", "t@example.test", "x" * 14)
        with self.assertLogs("wwok.anmeldung", logging.WARNING) as cm:
            self.client.post("/konto/login/", {"username": "t", "password": "geheim-passwort"})
        text = "\n".join(cm.output)
        self.assertIn("'t'", text)
        self.assertIn("127.0.0.1", text)
        self.assertNotIn("geheim-passwort", text)
