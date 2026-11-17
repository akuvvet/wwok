"""SMTP-Passwort je Mandant: in der Verwaltung eintragen, verschlüsselt speichern, nie anzeigen; Testmail-Knopf."""
import os
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.verkauf.mail import MailFehler, verbindung
from apps.verkauf.models import MailVersand

from .geheim import entschluesseln, verschluesseln
from .models import AenderungsProtokoll, Mandant

GEHEIM = "Sehr-Geheim-4711"


@override_settings(SECURE_SSL_REDIRECT=False)
class SmtpPasswortTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="ZONE", firmenname="ZONE Handel", smtp_host="smtp.example.test",
                                        smtp_benutzer="post@zone.test", absender_email="post@zone.test")
        self.admin = get_user_model().objects.create_superuser("chef", "chef@zone.test", "x" * 14)
        self.client.force_login(self.admin)
        self.url = reverse("admin:kern_mandant_change", args=[self.m.pk])

    def daten(self, **mehr):
        return {"kurzname": "ZONE", "firmenname": "ZONE Handel", "land": "DE", "smtp_host": "smtp.example.test",
                "smtp_port": 587, "smtp_sicherheit": "starttls", "smtp_benutzer": "post@zone.test",
                "absender_email": "post@zone.test", "konto_erloes_19": "8400", "konto_erloes_7": "8300",
                "konto_erloes_0": "8200", "konto_bank": "1200", "konto_kasse": "1000", "konto_skonto": "8736",
                "aktiv": "on", "flaechenmodus": "standard", "standard_vk": "3", **mehr}

    def test_verschluesselung_hin_und_zurueck(self):
        geheim = verschluesseln(GEHEIM)
        self.assertNotIn(GEHEIM, geheim)
        self.assertEqual(entschluesseln(geheim), GEHEIM)
        self.assertEqual(entschluesseln(""), "")
        self.assertEqual(entschluesseln("kaputt"), "")
        with override_settings(SECRET_KEY="anderer-schluessel"):  # anderer Schlüssel: nicht lesbar statt Absturz
            self.assertEqual(entschluesseln(geheim), "")

    def test_passwort_in_verwaltung_speichern(self):
        r = self.client.post(self.url, self.daten(smtp_passwort_neu=GEHEIM))
        self.assertEqual(r.status_code, 302)
        self.m.refresh_from_db()
        self.assertNotIn(GEHEIM, self.m.smtp_passwort_geheim)  # nie im Klartext in der Datenbank
        self.assertEqual(self.m.smtp_passwort, GEHEIM)
        self.assertEqual(self.m.smtp_passwort_quelle, "verwaltung")
        self.assertEqual(verbindung(self.m).password, GEHEIM)
        seite = self.client.get(self.url)
        self.assertNotContains(seite, GEHEIM)  # wird nie wieder angezeigt
        self.assertContains(seite, "hinterlegt (in der Verwaltung, verschlüsselt)")

    def test_leeres_feld_laesst_passwort_unveraendert_und_entfernen(self):
        self.m.smtp_passwort_setzen(GEHEIM)
        self.m.save()
        self.client.post(self.url, self.daten(smtp_passwort_neu=""))
        self.m.refresh_from_db()
        self.assertEqual(self.m.smtp_passwort, GEHEIM)
        self.client.post(self.url, self.daten(smtp_passwort_loeschen="on"))
        self.m.refresh_from_db()
        self.assertEqual(self.m.smtp_passwort_geheim, "")
        self.assertEqual(self.m.smtp_passwort_quelle, "")
        self.assertContains(self.client.get(self.url), "nicht hinterlegt")

    def test_passwort_steht_nicht_im_aenderungsprotokoll(self):
        self.client.post(self.url, self.daten(smtp_passwort_neu=GEHEIM))
        self.m.refresh_from_db()
        for eintrag in AenderungsProtokoll.objects.filter(tabelle="kern.Mandant"):
            text = f"{eintrag.vorher} {eintrag.nachher}"
            self.assertNotIn(GEHEIM, text)
            self.assertNotIn(self.m.smtp_passwort_geheim, text)
            self.assertNotIn("smtp_passwort", text)

    def test_env_bleibt_rueckfall_verwaltung_hat_vorrang(self):
        with mock.patch.dict(os.environ, {"SMTP_PASSWORD_ZONE": "aus-der-env"}):
            self.assertEqual(self.m.smtp_passwort, "aus-der-env")
            self.assertEqual(self.m.smtp_passwort_quelle, "env")
            self.assertContains(self.client.get(self.url), "hinterlegt (in der .env auf dem Server)")
            self.m.smtp_passwort_setzen(GEHEIM)
            self.assertEqual(self.m.smtp_passwort, GEHEIM)

    def test_ohne_passwort_klare_meldung(self):
        with mock.patch.dict(os.environ), self.assertRaisesMessage(MailFehler, "kein SMTP-Passwort hinterlegt"):
            os.environ.pop("SMTP_PASSWORD_ZONE", None)
            verbindung(self.m)

    def test_testmail_knopf(self):
        self.m.smtp_passwort_setzen(GEHEIM)
        self.m.save()
        self.assertContains(self.client.get(self.url), 'name="_testmail"')
        with mock.patch("apps.verkauf.mail.get_connection",
                        return_value=mail.get_connection("django.core.mail.backends.locmem.EmailBackend")):
            r = self.client.post(self.url, {"_testmail": "1"}, follow=True)
        self.assertContains(r, "Testmail für ZONE an chef@zone.test gesendet.")
        self.assertEqual(mail.outbox[0].to, ["chef@zone.test"])
        self.assertTrue(MailVersand.objects.get(bezug="Testmail").erfolgreich)

    def test_testmail_zeigt_fehler(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("SMTP_PASSWORD_ZONE", None)
            r = self.client.post(self.url, {"_testmail": "1"}, follow=True)
        self.assertContains(r, "kein SMTP-Passwort hinterlegt")

    def test_testmail_nur_mit_aenderungsrecht(self):
        leser = get_user_model().objects.create_user("leser", "l@zone.test", "x" * 14, is_staff=True)
        self.client.force_login(leser)
        self.assertEqual(self.client.post(self.url, {"_testmail": "1"}).status_code, 403)
        self.assertEqual(len(mail.outbox), 0)
