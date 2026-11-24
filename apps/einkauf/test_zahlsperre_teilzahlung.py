"""Zahlsperre und Teilzahlung für Lieferantenrechnungen (Entscheidung des Nutzers 24.11.2026, README 18)."""
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from decimal import Decimal as D

from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.kern.models import Mandant
from apps.stammdaten.models import Lieferant

from . import lieferantenkonto, services
from .models import Lieferantenrechnung, Lieferantenteilzahlung, RechnungStatus
from .views import rechnung_summen

HEUTE = date.today()


def meldungen(antwort):
    return " | ".join(str(m) for m in get_messages(antwort.wsgi_request))


def betraege(xml: bytes) -> list:
    return [e.text for e in ET.fromstring(xml).iter() if e.tag.endswith("InstdAmt")]


@override_settings(SECURE_SSL_REDIRECT=False)
class ZahlsperreTeilzahlungTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer", iban="DE02120300000000202051")
        self.lf = Lieferant.objects.create(nummer="L1", name1="Kosatec GmbH", kurzname="KOS", iban="DE89370400440532013000")
        self.chef = get_user_model().objects.create_superuser("chef", password="x" * 14)
        self.normal = get_user_model().objects.create_user("mitarbeiter", password="x" * 14)
        self.anmelden()
        self.r = self.rechnung("R1", D("119.00"))

    def anmelden(self, benutzer=None):
        self.client.force_login(benutzer or self.chef)
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    def rechnung(self, nummer, brutto, faellig=None, **zusatz):
        netto = (brutto / D("1.19")).quantize(D("0.01"))
        return Lieferantenrechnung.objects.create(mandant=self.m, lieferant=self.lf, rechnungsnummer=nummer, datum=HEUTE,
                                                  faellig=faellig or HEUTE, netto=netto, steuer=brutto - netto, brutto=brutto, **zusatz)

    def sepa(self, *rechnungen, **betraege_):
        daten = {"auswahl": [r.pk for r in rechnungen], "ausfuehrung": HEUTE.isoformat()}
        daten.update({f"betrag_{r.pk}": b for r, b in betraege_.get("teil", {}).items()})
        return self.client.post(reverse("einkauf:sepa"), daten)

    # ---- Zahlsperre ---------------------------------------------------------------------------
    def test_zahlsperre_setzen_braucht_grund_und_steht_in_historie(self):
        with self.assertRaisesMessage(ValueError, "Grund"):
            services.zahlsperre_setzen(self.r, "  ")
        services.zahlsperre_setzen(self.r, "Ware reklamiert", self.chef)
        self.r.refresh_from_db()
        self.assertTrue(self.r.zahlsperre)
        self.assertIn("Zahlsperre gesetzt – Ware reklamiert", self.r.historie)
        services.zahlsperre_aufheben(self.r, self.chef)
        self.r.refresh_from_db()
        self.assertEqual((self.r.zahlsperre, self.r.zahlsperre_grund), (False, ""))
        self.assertIn("Zahlsperre aufgehoben (war: Ware reklamiert)", self.r.historie)
        with self.assertRaisesMessage(ValueError, "keine Zahlsperre"):
            services.zahlsperre_aufheben(self.r)

    def test_zahlsperre_nicht_nach_export_oder_zahlung(self):
        Lieferantenrechnung.objects.filter(pk=self.r.pk).update(status=RechnungStatus.EXPORTIERT)
        with self.assertRaisesMessage(ValueError, "exportiert"):
            services.zahlsperre_setzen(self.r, "x")
        Lieferantenrechnung.objects.filter(pk=self.r.pk).update(status=RechnungStatus.BEZAHLT)
        with self.assertRaisesMessage(ValueError, "bezahlt"):
            services.zahlsperre_setzen(self.r, "x")

    def test_sepa_bricht_bei_zahlsperre_ab(self):
        frei = self.rechnung("R2", D("50.00"))
        services.zahlsperre_setzen(self.r, "Reklamation")
        antwort = self.sepa(self.r, frei)
        self.assertIn("Zahlsperre bei R1", meldungen(antwort))
        self.assertEqual(set(Lieferantenrechnung.objects.values_list("status", flat=True)), {RechnungStatus.OFFEN})
        antwort = self.sepa(frei)
        self.assertEqual(betraege(antwort.content), ["50.00"])

    def test_zahlsperre_in_summen_und_liste(self):
        self.rechnung("R2", D("50.00"))
        services.zahlsperre_setzen(self.r, "Reklamation")
        s = rechnung_summen(Lieferantenrechnung.objects.filter(mandant=self.m), HEUTE, D("0"))
        self.assertEqual((s["gesperrt"], s["gesperrt_anzahl"], s["gesamt"], s["faellig"]), (D("119.00"), 1, D("50.00"), D("50.00")))
        antwort = self.client.get(reverse("finanzen:zahlungen_lieferanten"))
        self.assertContains(antwort, "Zahlsperre: Reklamation")
        self.assertNotContains(antwort, f'name="auswahl" value="{self.r.pk}"')

    def test_zahlsperre_ueber_die_seite_und_bezahlt_hebt_sie_auf(self):
        url = reverse("einkauf:rechnung_zahlsperre", args=[self.r.pk])
        antwort = self.client.post(url, {"grund": ""}, follow=True)
        self.assertContains(antwort, "Grund")
        self.client.post(url, {"grund": "Lieferung fehlt"})
        self.assertContains(self.client.get(reverse("einkauf:rechnung", args=[self.r.pk])), "Zahlsperre:</strong> Lieferung fehlt")
        antwort = self.client.post(reverse("einkauf:rechnung_bezahlt", args=[self.r.pk]), {"datum": HEUTE.isoformat(), "betrag": "119,00"})
        self.assertIn("Zahlsperre", meldungen(antwort))
        self.r.refresh_from_db()
        self.assertEqual((self.r.status, self.r.zahlsperre), (RechnungStatus.BEZAHLT, False))

    # ---- Teilzahlung --------------------------------------------------------------------------
    def test_teilzahlung_senkt_zahlbetrag_rest_bleibt_offen(self):
        services.teilzahlung_buchen(self.r, D("40.00"), HEUTE, self.chef)
        self.r.refresh_from_db()
        self.assertEqual((self.r.status, self.r.teilgezahlt, self.r.zahlbetrag(), self.r.offener_betrag),
                         (RechnungStatus.OFFEN, D("40.00"), D("79.00"), D("79.00")))
        self.assertIn("Teilzahlung 40.00 €", self.r.historie)
        services.teilzahlung_buchen(self.r, D("9.00"), HEUTE)
        self.r.refresh_from_db()
        self.assertEqual(self.r.zahlbetrag(), D("70.00"))

    def test_teilzahlung_grenzen(self):
        for betrag, text in ((D("0"), "größer als 0"), (D("-5"), "größer als 0"), (D("119.00"), "kleiner als der offene Betrag"),
                             (D("200"), "kleiner als der offene Betrag")):
            with self.subTest(betrag), self.assertRaisesMessage(ValueError, text):
                services.teilzahlung_buchen(self.r, betrag, HEUTE)
        with self.assertRaisesMessage(ValueError, "Zukunft"):
            services.teilzahlung_buchen(self.r, D("1"), HEUTE + timedelta(days=1))
        Lieferantenrechnung.objects.filter(pk=self.r.pk).update(status=RechnungStatus.BEZAHLT)
        with self.assertRaisesMessage(ValueError, "schon als bezahlt"):
            services.teilzahlung_buchen(self.r, D("1"), HEUTE)
        self.assertFalse(Lieferantenteilzahlung.objects.exists())

    def test_teilzahlung_mit_skonto_und_gutschrift(self):
        r = self.rechnung("R-SK", D("100.00"), skonto_prozent=D("2"), skonto_bis=HEUTE + timedelta(days=5))
        g = services.gutschrift_erfassen(self.m, self.lf, "G1", HEUTE, D("8.40"), D("1.60"), D("10.00"), "", None, self.chef)
        services.gutschrift_verrechnen(g, r, D("10.00"), HEUTE)
        services.teilzahlung_buchen(r, D("30.00"), HEUTE)
        r.refresh_from_db()
        self.assertEqual(r.zahlbetrag(), D("58.00"))  # 98 nach Skonto − 10 Gutschrift − 30 Teilzahlung
        self.assertEqual(r.offener_betrag, D("60.00"))

    def test_storno_nur_admin_mit_grund_und_nicht_nach_vollzahlung(self):
        t = services.teilzahlung_buchen(self.r, D("40.00"), HEUTE)
        url = reverse("einkauf:rechnung_teilzahlung_storno", args=[self.r.pk, t.pk])
        self.anmelden(self.normal)
        self.client.post(url, {"grund": "falsch"})
        self.assertFalse(Lieferantenteilzahlung.objects.get(pk=t.pk).storniert)
        self.anmelden()
        self.assertIn("Stornogrund", meldungen(self.client.post(url, {"grund": " "})))
        self.client.post(url, {"grund": "doppelt gebucht"})
        t.refresh_from_db()
        self.r.refresh_from_db()
        self.assertTrue(t.storniert)
        self.assertEqual(self.r.zahlbetrag(), D("119.00"))
        self.assertIn("storniert – doppelt gebucht", self.r.historie)
        t2 = services.teilzahlung_buchen(self.r, D("19.00"), HEUTE)
        self.client.post(reverse("einkauf:rechnung_bezahlt", args=[self.r.pk]), {"datum": HEUTE.isoformat()})
        self.r.refresh_from_db()
        self.assertEqual((self.r.status, self.r.bezahlter_betrag), (RechnungStatus.BEZAHLT, D("100.00")))  # Rest vorgeschlagen
        with self.assertRaisesMessage(ValueError, "ganz bezahlt"):
            services.teilzahlung_stornieren(t2, "x")

    def test_loeschen_und_korrigieren_geschuetzt(self):
        services.teilzahlung_buchen(self.r, D("40.00"), HEUTE)
        self.r.refresh_from_db()
        with self.assertRaisesMessage(ValueError, "Teilzahlungen"):
            services.rechnung_loeschen(self.r, "Test")
        with self.assertRaisesMessage(ValueError, "Teilzahlung"):
            self.r.betraege_korrigieren(D("25.21"), D("4.79"), D("30.00"))

    def test_teilzahlung_ueber_die_seite(self):
        antwort = self.client.post(reverse("einkauf:rechnung_teilzahlung", args=[self.r.pk]),
                                   {"datum": HEUTE.isoformat(), "betrag": "19,50"}, follow=True)
        self.assertContains(antwort, "Teilzahlung 19,50 € gebucht, offen bleiben 99,50 €")
        self.assertContains(antwort, "nach Teilzahlungen von 19,50 €")
        self.assertContains(antwort, "<h2>Teilzahlungen</h2>")

    # ---- SEPA mit Teilbetrag ------------------------------------------------------------------
    def test_sepa_mit_teilbetrag_und_buchen(self):
        voll = self.rechnung("R2", D("50.00"))
        antwort = self.sepa(self.r, voll, teil={self.r: "30,00"})
        self.assertEqual(betraege(antwort.content), ["30.00", "50.00"])
        self.assertIn("Teilzahlung exportiert: R1 30,00 €", meldungen(antwort))
        self.r.refresh_from_db()
        self.assertEqual((self.r.status, self.r.sepa_betrag), (RechnungStatus.EXPORTIERT, D("30.00")))
        seite = self.client.get(reverse("einkauf:rechnung", args=[self.r.pk]))
        self.assertContains(seite, "Teilzahlung über 30,00 €")
        self.assertContains(seite, 'name="betrag" size="9" inputmode="decimal" required value="30,00"')
        avis = services.zahlungsavis([self.r], HEUTE)["zeilen"][0]
        self.assertEqual((avis["zahlung"], avis["skonto"]), (D("30.00"), D("0")))
        self.assertIn("offen bleiben 89,00 €", avis["hinweis"])
        services.teilzahlung_buchen(self.r, D("30.00"), HEUTE)
        self.r.refresh_from_db()
        self.assertEqual((self.r.status, self.r.sepa_betrag, self.r.zahlbetrag()), (RechnungStatus.OFFEN, None, D("89.00")))
        self.assertEqual(betraege(self.sepa(self.r).content), ["89.00"])  # nächster Export: der Rest

    def test_sepa_teilbetrag_grenzen_exportiert_nichts(self):
        for text in ("0", "-1", "119,01"):
            with self.subTest(text):
                antwort = self.sepa(self.r, teil={self.r: text})
                self.assertIn("Nichts exportiert", meldungen(antwort))
                self.r.refresh_from_db()
                self.assertEqual(self.r.status, RechnungStatus.OFFEN)
        antwort = self.sepa(self.r, teil={self.r: "119,00"})  # voller Betrag = keine Teilzahlung
        self.r.refresh_from_db()
        self.assertEqual((self.r.status, self.r.sepa_betrag), (RechnungStatus.EXPORTIERT, None))

    def test_lieferantenkonto_zeigt_teilzahlung(self):
        services.teilzahlung_buchen(self.r, D("40.00"), HEUTE)
        texte = str(lieferantenkonto.kontoauszug(self.m, self.lf))
        self.assertIn("Teilzahlung an Lieferanten zu Rechnung R1", texte)
        self.client.post(reverse("einkauf:rechnung_bezahlt", args=[self.r.pk]), {"datum": HEUTE.isoformat()})
        texte = str(lieferantenkonto.kontoauszug(self.m, self.lf))
        self.assertNotIn("Skonto/Differenz", texte)  # 40 + 79 = 119: keine Differenz
