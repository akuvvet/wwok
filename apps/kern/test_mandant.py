"""Aktiver Mandant: bleibt je Browser über die Anmeldung hinaus erhalten und steht in der Kopfzeile."""
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from .kontext import MANDANT_COOKIE
from .models import Mandant


@override_settings(SECURE_SSL_REDIRECT=False)
class MandantMerkenTest(TestCase):
    def setUp(self):
        self.okay = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.zone = Mandant.objects.create(kurzname="ZONE", firmenname="ZONE Handel")
        self.user = get_user_model().objects.create_user("t", password="x" * 14)
        self.start = reverse("verkauf:start")
        self.client.force_login(self.user)

    def neu_anmelden(self):
        """Neue Sitzung im selben Browser: logout() leert im Testclient auch die Cookies, daher zurücklegen."""
        cookie = self.client.cookies[MANDANT_COOKIE].value
        self.client.logout()
        self.client.cookies[MANDANT_COOKIE] = cookie
        self.client.force_login(self.user)

    def test_ohne_auswahl_klare_aufforderung(self):
        r = self.client.get(reverse("verkauf:belege"), follow=True)
        self.assertContains(r, "Kein Mandant gewählt")
        self.assertContains(r, "Auswählen</button>")

    def test_auswahl_bleibt_nach_neuer_anmeldung(self):
        r = self.client.post(self.start, {"mandant": self.zone.pk})
        self.assertEqual(r.cookies[MANDANT_COOKIE].value, str(self.zone.pk))
        self.neu_anmelden()
        self.assertEqual(self.client.get(reverse("verkauf:belege")).status_code, 200)  # keine Umleitung
        r = self.client.get(self.start)
        self.assertContains(r, 'title="ZONE Handel – hier wechseln">ZONE</a>')
        self.assertContains(r, "Wechseln</button>")

    def test_deaktivierter_mandant_im_cookie_gilt_nicht(self):
        self.client.post(self.start, {"mandant": self.zone.pk})
        self.zone.aktiv = False
        self.zone.save()
        self.neu_anmelden()
        self.assertEqual(self.client.get(reverse("verkauf:belege")).status_code, 200)  # nur OKAY aktiv: automatisch
        self.assertContains(self.client.get(self.start), ">OKAY</a>")

    def test_ungueltiges_cookie_und_anmeldeseite(self):
        self.client.cookies[MANDANT_COOKIE] = "abc"
        self.assertContains(self.client.get(self.start), "Kein Mandant gewählt")
        self.client.logout()
        self.assertNotContains(self.client.get(reverse("login")), "Kein Mandant gewählt")
