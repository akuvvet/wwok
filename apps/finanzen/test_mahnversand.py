"""Mahnwesen: Versandstand je Mahnung, Sammeldruck, Sammelversand mit Übersicht, Filter „noch nicht versendet“."""
from datetime import date, timedelta
from decimal import Decimal
from io import BytesIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from pypdf import PdfReader

from apps.kern.models import Mandant
from apps.stammdaten.models import Kunde, Zahlungsbedingung
from apps.verkauf.models import Beleg, Belegart, MailVersand, Position

from . import mahnversand, services
from .models import Mahnung


def postfach():
    """Ersetzt die SMTP-Verbindung durch das Test-Postfach."""
    return mock.patch("apps.verkauf.mail.get_connection",
                      return_value=mail.get_connection("django.core.mail.backends.locmem.EmailBackend"))


@override_settings(SECURE_SSL_REDIRECT=False)
class MahnversandTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer", smtp_host="smtp.example.test",
                                        absender_email="rechnung@okay.test")
        self.zb = Zahlungsbedingung.objects.create(bezeichnung="10 Tage", tage_netto=10, tage_mahnung=11)
        self.mit = Kunde.objects.create(nummer="1", name1="Mit Mail GmbH", email="a@example.test")
        self.zwei = Kunde.objects.create(nummer="2", name1="Zweite GmbH", email="b@example.test")
        self.ohne = Kunde.objects.create(nummer="3", name1="Ohne Mail GmbH")
        self.benutzer = get_user_model().objects.create_user("t", password="x" * 14)
        self.client.force_login(self.benutzer)
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()
        self.url = reverse("finanzen:mahnungen_sammel")

    def mahnung(self, kunde, mandant=None):
        b = Beleg.objects.create(mandant=mandant or self.m, art=Belegart.RECHNUNG, kunde=kunde, zahlungsbedingung=self.zb)
        Position.objects.create(beleg=b, bezeichnung="Dienst", einzelpreis=Decimal("100"))
        b.abschliessen()
        Beleg.objects.filter(pk=b.pk).update(belegdatum=date.today() - timedelta(days=30))
        b.refresh_from_db()
        return services.mahnung_erstellen(b, 1)

    def test_einzelversand_vermerkt_gesendet(self):
        m = self.mahnung(self.mit)
        self.assertContains(self.client.get(reverse("finanzen:mahnwesen")), "noch nicht gesendet")
        with postfach():
            self.client.post(reverse("finanzen:mahnung_mail", args=[m.pk]), {"an": "x@example.test", "betreff": "B", "text": "T"})
        m.refresh_from_db()
        self.assertIsNotNone(m.gesendet_am)
        self.assertEqual(m.gesendet_an, "x@example.test")
        seite = self.client.get(reverse("finanzen:mahnwesen"))
        self.assertContains(seite, "gesendet am")
        self.assertContains(seite, "x@example.test")
        self.assertNotContains(seite, "noch nicht gesendet")

    def test_fehlgeschlagener_versand_wird_angezeigt_und_nach_erfolg_geloescht(self):
        m = self.mahnung(self.mit)
        self.m.smtp_host = ""  # Versand nicht eingerichtet
        self.m.save()
        self.client.post(reverse("finanzen:mahnung_mail", args=[m.pk]), {"an": "x@example.test", "betreff": "B", "text": "T"})
        m.refresh_from_db()
        self.assertIsNone(m.gesendet_am)
        self.assertTrue(m.versand_fehler)
        self.assertContains(self.client.get(reverse("finanzen:mahnwesen")), "Versand fehlgeschlagen")
        self.m.smtp_host = "smtp.example.test"
        self.m.save()
        m = Mahnung.objects.select_related("beleg__mandant").get(pk=m.pk)
        with postfach():
            mahnversand.mahnung_senden(m)
        m.refresh_from_db()
        self.assertEqual(m.versand_fehler, "")
        self.assertEqual(m.gesendet_an, "a@example.test")

    def test_sammeldruck_ein_pdf_und_druckvermerk(self):
        a, b = self.mahnung(self.mit), self.mahnung(self.ohne)
        r = self.client.post(self.url, {"aktion": "drucken", "mahnung": [a.pk, b.pk]})
        self.assertEqual(r["Content-Type"], "application/pdf")
        self.assertEqual(len(PdfReader(BytesIO(r.content)).pages), 2)  # je Mahnung eine Seite
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertIsNotNone(a.gedruckt_am)
        self.assertIsNotNone(b.gedruckt_am)
        self.assertIsNone(a.gesendet_am)
        self.assertContains(self.client.get(reverse("finanzen:mahnwesen")), "gedruckt am")

    def test_sammelversand_zeigt_erst_die_uebersicht(self):
        a, b = self.mahnung(self.mit), self.mahnung(self.ohne)
        with postfach():
            r = self.client.post(self.url, {"aktion": "senden", "mahnung": [a.pk, b.pk]})
        self.assertContains(r, "a@example.test")
        self.assertContains(r, "keine E-Mail-Adresse")
        self.assertContains(r, "Jetzt senden")
        self.assertEqual(len(mail.outbox), 0)  # ohne Bestätigung geht nichts raus
        self.assertFalse(MailVersand.objects.exists())

    def test_sammelversand_sendet_und_ueberspringt_ohne_adresse(self):
        a, b, c = self.mahnung(self.mit), self.mahnung(self.zwei), self.mahnung(self.ohne)
        with postfach():
            r = self.client.post(self.url, {"aktion": "senden", "bestaetigt": "1", "mahnung": [a.pk, b.pk, c.pk]}, follow=True)
        self.assertEqual(sorted(n.to[0] for n in mail.outbox), ["a@example.test", "b@example.test"])
        self.assertEqual(mail.outbox[0].attachments[0][2], "application/pdf")
        self.assertContains(r, "2 Mahnung(en) per E-Mail gesendet.")
        self.assertContains(r, "Ohne E-Mail-Adresse, nicht gesendet")
        self.assertContains(r, "Ohne Mail GmbH")
        c.refresh_from_db()
        self.assertIsNone(c.gesendet_am)
        self.assertEqual(MailVersand.objects.filter(erfolgreich=True).count(), 2)  # Nachweis im Mailprotokoll

    def test_sammelversand_fehler_je_mahnung_gemeldet(self):
        a = self.mahnung(self.mit)
        self.m.smtp_host = ""
        self.m.save()
        r = self.client.post(self.url, {"aktion": "senden", "bestaetigt": "1", "mahnung": [a.pk]}, follow=True)
        self.assertContains(r, "Mit Mail GmbH")
        self.assertContains(r, "Versand fehlgeschlagen")
        self.assertNotContains(r, "per E-Mail gesendet.")

    def test_filter_nur_noch_nicht_versendete(self):
        gesendet, gedruckt, offen = self.mahnung(self.mit), self.mahnung(self.zwei), self.mahnung(self.ohne)
        with postfach():
            mahnversand.mahnung_senden(Mahnung.objects.get(pk=gesendet.pk))
        mahnversand.sammel_pdf([gedruckt])
        seite = self.client.get(reverse("finanzen:mahnwesen"), {"nur": "offen"})
        self.assertEqual([m.pk for m in seite.context["bisher"]], [offen.pk])
        self.assertEqual(len(self.client.get(reverse("finanzen:mahnwesen")).context["bisher"]), 3)

    def test_fremder_mandant_und_leere_auswahl(self):
        zone = Mandant.objects.create(kurzname="ZONE", firmenname="ZONE Handel", smtp_host="smtp.example.test",
                                      absender_email="z@zone.test")
        fremd = self.mahnung(self.mit, zone)
        with postfach():
            r = self.client.post(self.url, {"aktion": "senden", "bestaetigt": "1", "mahnung": [fremd.pk]}, follow=True)
        self.assertContains(r, "Keine Mahnung ausgewählt.")
        self.assertEqual(len(mail.outbox), 0)
        r = self.client.post(self.url, {"aktion": "drucken", "mahnung": [fremd.pk, "abc"]})
        self.assertEqual(r.status_code, 302)
        fremd.refresh_from_db()
        self.assertIsNone(fremd.gedruckt_am)
        self.assertEqual(self.client.get(self.url).status_code, 405)  # nur per POST

    def test_hoechstens_15_je_sammelversand(self):
        kunden = [Kunde.objects.create(nummer=str(100 + i), name1=f"K{i}", email=f"k{i}@example.test") for i in range(16)]
        ids = [self.mahnung(k).pk for k in kunden]
        with postfach():
            uebersicht = self.client.post(self.url, {"aktion": "senden", "mahnung": ids})
            self.assertEqual(sum(1 for z in uebersicht.context["zeilen"] if z["gewaehlt"]), 15)  # nur 15 vorausgewählt
            self.assertContains(uebersicht, "höchstens 15")
            r = self.client.post(self.url, {"aktion": "senden", "bestaetigt": "1", "mahnung": ids}, follow=True)
            self.assertContains(r, "Höchstens 15 Mahnungen je Versand")
            self.assertEqual(len(mail.outbox), 0)  # zu viele: nichts geht raus
            self.client.post(self.url, {"aktion": "senden", "bestaetigt": "1", "mahnung": ids[:15]})
        self.assertEqual(len(mail.outbox), 15)
