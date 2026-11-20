"""Wunsch des Nutzers 20.11.2026: Kontoauszug an Kunde oder Lieferant senden oder drucken (Zeitraum wählbar), Zahlungsavis an
den Lieferanten (PDF oder E-Mail). Empfänger: Hauptkontakt bzw. erster Ansprechpartner mit E-Mail, sonst Firmen-E-Mail."""
import io
from datetime import timedelta
from decimal import Decimal as D
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from pypdf import PdfReader

from apps.finanzen import services as finanzen
from apps.kern.models import Mandant
from apps.stammdaten.models import Ansprechpartner, Anrede, Kunde, Lieferant
from apps.verkauf.models import Beleg, Belegart, MailVersand, Position

from . import services
from .models import Lieferantenrechnung, RechnungStatus


def pdf_text(inhalt) -> str:
    return "\n".join(s.extract_text() or "" for s in PdfReader(io.BytesIO(inhalt)).pages)


@override_settings(SECURE_SSL_REDIRECT=False)
class Basis(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer", smtp_host="smtp.example.test",
                                        absender_email="buchhaltung@okay.test")
        self.heute = timezone.localdate()
        self.client.force_login(get_user_model().objects.create_superuser("chef", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()
        self.herr = Anrede.objects.filter(bezeichnung="Herr").first() or Anrede.objects.create(nummer=92, bezeichnung="Herr")

    def senden(self, url, daten):
        with mock.patch("apps.verkauf.mail.verbindung", return_value=None):
            return self.client.post(url, daten)


class KundenKontoauszugTest(Basis):
    def setUp(self):
        super().setUp()
        self.kunde = Kunde.objects.create(nummer="10072", name1="Kunde GmbH", email="info@kunde.test")

    def rechnung(self, netto, datum):
        b = Beleg.objects.create(mandant=self.m, art=Belegart.RECHNUNG, kunde=self.kunde)
        Position.objects.create(beleg=b, bezeichnung="Dienst", einzelpreis=D(netto))
        b.abschliessen()
        Beleg.objects.filter(pk=b.pk).update(belegdatum=datum)
        return b

    def test_zeitraum_mit_anfangssaldo(self):
        self.rechnung("100", self.heute - timedelta(days=40))   # 119 vor dem Zeitraum
        neu = self.rechnung("10", self.heute - timedelta(days=5))  # 11,90 im Zeitraum
        von = self.heute - timedelta(days=30)
        r = self.client.get(reverse("finanzen:kontoauszug", args=[self.kunde.pk]), {"von": von.isoformat()})
        zeilen = r.context["zeilen"]
        self.assertEqual((zeilen[0]["text"], zeilen[0]["saldo"]), ("Anfangssaldo", D("119.00")))
        self.assertEqual([z["text"] for z in zeilen[1:]], [str(neu)])
        self.assertEqual(r.context["saldo"], D("130.90"))
        self.assertContains(r, f"?von={von.isoformat()}&amp;format=pdf")
        text = pdf_text(self.client.get(reverse("finanzen:kontoauszug", args=[self.kunde.pk]), {"von": von.isoformat(), "format": "pdf"}).content)
        self.assertIn("Anfangssaldo", text)
        self.assertIn(f"Zeitraum: ab {von:%d.%m.%Y}", text)
        bis = self.heute - timedelta(days=35)  # nur die alte Rechnung
        zeilen, saldo = finanzen.zeitraum(finanzen.kontoauszug(self.m, self.kunde)[0], None, bis)
        self.assertEqual((len(zeilen), saldo), (1, D("119.00")))

    def test_mail_an_hauptkontakt_mit_pdf(self):
        self.rechnung("100", self.heute)
        Ansprechpartner.objects.create(kunde=self.kunde, nachname="Albers", email="albers@kunde.test")
        Ansprechpartner.objects.create(kunde=self.kunde, nachname="Zeller", anrede=self.herr, email="zeller@kunde.test", hauptkontakt=True)
        url = reverse("finanzen:kontoauszug_mail", args=[self.kunde.pk])
        r = self.client.get(url)
        self.assertEqual(r.context["an"], "zeller@kunde.test")
        self.assertIn("Sehr geehrter Herr Zeller", r.context["text"])
        self.assertIn("119,00", r.context["text"])
        self.assertContains(self.client.get(reverse("finanzen:kontoauszug", args=[self.kunde.pk])), url)
        r = self.senden(url, {"an": "zeller@kunde.test", "betreff": "Kontoauszug", "text": "Anbei."})
        self.assertRedirects(r, reverse("finanzen:kontoauszug", args=[self.kunde.pk]), fetch_redirect_response=False)
        nachricht = mail.outbox[-1]
        self.assertEqual(nachricht.to, ["zeller@kunde.test"])
        self.assertEqual(nachricht.attachments[0][0], "Kontoauszug-10072.pdf")
        self.assertIn("Kundenkontoauszug", pdf_text(nachricht.attachments[0][1]))
        self.assertTrue(MailVersand.objects.filter(bezug="Kontoauszug 10072", erfolgreich=True).exists())

    def test_ohne_ansprechpartner_firmen_email(self):
        self.assertEqual(self.client.get(reverse("finanzen:kontoauszug_mail", args=[self.kunde.pk])).context["an"], "info@kunde.test")


class LieferantTest(Basis):
    def setUp(self):
        super().setUp()
        self.m2 = Mandant.objects.create(kurzname="ZONE", firmenname="ZONE Handel")
        self.lf = Lieferant.objects.create(nummer="L1", name1="Kosatec Computer GmbH", kurzname="KOS", email="info@kosatec.test",
                                           iban="DE02120300000000202051")
        Ansprechpartner.objects.create(lieferant=self.lf, mandant=self.m2, nachname="Fremd", email="fremd@kosatec.test", hauptkontakt=True)
        Ansprechpartner.objects.create(lieferant=self.lf, mandant=self.m, nachname="Buch", email="buchhaltung@kosatec.test")

    def rechnung(self, nr, brutto, **felder):
        r = services.rechnung_erfassen(self.m, self.lf, nr, self.heute - timedelta(days=3), (D(brutto) / D("1.19")).quantize(D("0.01")), D(brutto))
        if felder:
            Lieferantenrechnung.objects.filter(pk=r.pk).update(**felder)
            r.refresh_from_db()
        return r

    def test_kontoauszug_zeitraum_und_mail(self):
        self.rechnung("R-ALT", "100")
        Lieferantenrechnung.objects.filter(rechnungsnummer="R-ALT").update(datum=self.heute - timedelta(days=60))
        self.rechnung("R-NEU", "50")
        url = reverse("einkauf:lieferant_kontoauszug", args=[self.lf.pk])
        r = self.client.get(url, {"von": (self.heute - timedelta(days=30)).isoformat()})
        self.assertEqual(r.context["zeilen"][0]["saldo"], D("100"))
        self.assertEqual(r.context["saldo"], D("150"))
        self.assertContains(r, "Anfangssaldo")
        self.assertContains(r, reverse("einkauf:zahlungsavis", args=[self.lf.pk]))
        mail_url = reverse("einkauf:lieferant_kontoauszug_mail", args=[self.lf.pk])
        r = self.client.get(mail_url)
        self.assertEqual(r.context["an"], "buchhaltung@kosatec.test")  # nur Ansprechpartner des Mandanten
        self.senden(mail_url, {"an": "buchhaltung@kosatec.test", "betreff": "Kontoauszug", "text": "Anbei."})
        self.assertIn("Lieferantenkontoauszug", pdf_text(mail.outbox[-1].attachments[0][1]))

    def test_zahlungsavis_vorauswahl_skonto_gutschrift(self):
        exportiert = self.rechnung("R-100", "119", status=RechnungStatus.EXPORTIERT, skonto_prozent=D("2"),
                                   skonto_bis=self.heute + timedelta(days=5))
        mit_gs = self.rechnung("R-200", "238")
        g = services.gutschrift_erfassen(self.m, self.lf, "GS-9", self.heute, D("16.81"), D("3.19"), D("20.00"))
        services.gutschrift_verrechnen(g, mit_gs, D("20.00"))  # erst verrechnen, dann zur Zahlung exportieren
        Lieferantenrechnung.objects.filter(pk=mit_gs.pk).update(status=RechnungStatus.EXPORTIERT)
        self.rechnung("R-OFFEN", "50")  # offen: kein Kandidat
        bezahlt = self.rechnung("R-ALT", "60", status=RechnungStatus.BEZAHLT, bezahlt_am=self.heute - timedelta(days=10),
                                bezahlter_betrag=D("60"))
        url = reverse("einkauf:zahlungsavis", args=[self.lf.pk])
        r = self.client.get(url)
        zeilen = {z["rechnung"].rechnungsnummer: z for z in r.context["zeilen"]}
        self.assertEqual(set(zeilen), {"R-100", "R-200", "R-ALT"})
        self.assertEqual({n for n, z in zeilen.items() if z["gewaehlt"]}, {"R-100", "R-200"})  # exportierte vorgewählt
        self.assertEqual((zeilen["R-100"]["skonto"], zeilen["R-100"]["zahlung"]), (D("2.38"), D("116.62")))
        self.assertEqual((zeilen["R-200"]["verrechnet"], zeilen["R-200"]["zahlung"]), (D("20.00"), D("218.00")))
        self.assertEqual(r.context["summe"]["zahlung"], D("334.62"))
        pdf = self.client.get(url, {"r": [exportiert.pk, mit_gs.pk], "datum": self.heute.isoformat(), "aktion": "pdf"})
        self.assertEqual(pdf["Content-Type"], "application/pdf")
        text = pdf_text(pdf.content)
        for erwartet in ("Zahlungsavis", "R-100", "R-200", "GS-9", "334,62", "DE02120300000000202051"):
            self.assertIn(erwartet, text)
        spaet = self.client.get(url, {"r": [exportiert.pk], "datum": (self.heute + timedelta(days=10)).isoformat()})
        self.assertEqual(spaet.context["summe"]["zahlung"], D("119"))  # nach der Skontofrist ohne Skonto
        nur_bezahlt = self.client.get(url, {"r": ["", bezahlt.pk]})
        self.assertEqual(nur_bezahlt.context["summe"]["zahlung"], D("60"))

    def test_zahlungsavis_mail(self):
        r1 = self.rechnung("R-100", "119", status=RechnungStatus.EXPORTIERT)
        url = reverse("einkauf:zahlungsavis", args=[self.lf.pk])
        weiter = self.client.get(url, {"r": [r1.pk], "datum": self.heute.isoformat(), "aktion": "mail"})
        self.assertEqual(weiter.status_code, 302)
        seite = self.client.get(weiter["Location"])
        self.assertEqual(seite.context["an"], "buchhaltung@kosatec.test")
        self.assertIn("119,00 €", seite.context["text"])
        self.senden(weiter["Location"], {"an": "buchhaltung@kosatec.test", "betreff": "Avis", "text": "Anbei."})
        self.assertTrue(mail.outbox[-1].attachments[0][0].startswith("Zahlungsavis-L1-"))
        self.assertIn("R-100", pdf_text(mail.outbox[-1].attachments[0][1]))
        leer = self.client.get(url, {"r": [""], "aktion": "pdf"})  # nichts gewählt: Hinweis statt PDF
        self.assertEqual(leer["Content-Type"].split(";")[0], "text/html")
        self.assertContains(leer, "Bitte mindestens eine Rechnung auswählen.")

    def test_symbol_auf_der_lieferantenseite(self):
        self.assertContains(self.client.get(reverse("einkauf:lieferant", args=[self.lf.pk])), 'aria-label="Zahlungsavis"')
