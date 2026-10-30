"""Regressionstests zur Projektprüfung: Betragseingaben, Mahnwesen, Bankimport, Kassenbuch, Lastschrift-Aufhebung."""
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.kern.models import Mandant
from apps.stammdaten.models import Kunde, Zahlungsbedingung
from apps.verkauf.models import Beleg, Belegart, Position

from . import bank, services
from .models import Bankumsatz, Kassenbuch, Lastschrift
from .views import betrag


@override_settings(SECURE_SSL_REDIRECT=False)
class AuditFinanzenTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.kunde = Kunde.objects.create(nummer="1", name1="Kunde GmbH")
        self.user = get_user_model().objects.create_user("t", password="x" * 14, is_staff=True)
        self.client.force_login(self.user)
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    def rechnung(self, zb=None, preis="100"):
        b = Beleg.objects.create(mandant=self.m, art=Belegart.RECHNUNG, kunde=self.kunde, zahlungsbedingung=zb)
        Position.objects.create(beleg=b, bezeichnung="Dienst", einzelpreis=Decimal(preis))
        b.abschliessen()
        b.refresh_from_db()
        return b

    # Betragseingabe
    def test_betrag_lehnt_unsinn_ab(self):
        self.assertEqual(betrag("1.234,50"), Decimal("1234.50"))
        self.assertEqual(betrag("12.5"), Decimal("12.5"))
        for text in ("NaN", "Infinity", "1e50", "1.234", "12,345", "abc"):
            with self.assertRaises(ValueError, msg=text):
                betrag(text)

    def test_bank_csv_betrag_mehrdeutig_wird_abgelehnt(self):
        with self.assertRaises(bank.ImportFehler):
            bank.zahl("1.500")
        self.assertEqual(bank.zahl("1.500,00"), Decimal("1500.00"))
        self.assertEqual(bank.zahl("0.500"), Decimal("0.500"))

    def test_seiten_mit_kaputten_parametern_liefern_kein_500(self):
        self.assertEqual(self.client.get(reverse("finanzen:offene_posten"), {"kunde": "abc"}).status_code, 200)
        self.assertEqual(self.client.get(reverse("werkstatt:annahme"), {"kunde": "abc"}).status_code, 200)
        r = self.client.post(reverse("finanzen:mahnungen_erstellen"), {"auswahl": "kaputt"}, follow=True)
        self.assertContains(r, "Ungültige Auswahl")

    def test_offene_posten_zeigt_bezahlt_mit_cents(self):
        zb = Zahlungsbedingung.objects.create(bezeichnung="sofort", tage_netto=0, tage_mahnung=0)
        b = self.rechnung(zb)
        services.zahlung_buchen(b, Decimal("12.50"), date.today(), "bank", benutzer=self.user)
        self.assertContains(self.client.get(reverse("finanzen:offene_posten")), "12,50")

    # Mahnwesen
    def test_mahnung_nicht_vor_der_faelligkeit(self):
        zb = Zahlungsbedingung.objects.create(bezeichnung="14 Tage", tage_netto=14, tage_mahnung=0)
        b = self.rechnung(zb)
        self.assertEqual(services.mahnvorschlag(self.m, b.belegdatum + timedelta(days=2)), [])
        self.assertEqual(len(services.mahnvorschlag(self.m, b.belegdatum + timedelta(days=15))), 1)

    def test_mahnung_nur_fuer_rechnung_und_in_reihenfolge(self):
        zb = Zahlungsbedingung.objects.create(bezeichnung="sofort", tage_netto=0, tage_mahnung=0)
        b = self.rechnung(zb)
        with self.assertRaises(ValueError):
            services.mahnung_erstellen(b, 3, self.user)  # Stufe 1 und 2 fehlen
        services.mahnung_erstellen(b, 1, self.user)
        auftrag = Beleg.objects.create(mandant=self.m, art=Belegart.AUFTRAG, kunde=self.kunde)
        with self.assertRaises(ValueError):
            services.mahnung_erstellen(auftrag, 1, self.user)

    def test_lastschrift_rechnung_wird_nicht_gemahnt(self):
        zb = Zahlungsbedingung.objects.create(bezeichnung="sofort", tage_netto=0, tage_mahnung=0)
        b = self.rechnung(zb)
        spaeter = b.belegdatum + timedelta(days=30)
        self.assertEqual(len(services.mahnvorschlag(self.m, spaeter)), 1)
        ls = Lastschrift.objects.create(mandant=self.m, beleg=b, betrag=b.summe_brutto, einzugsdatum=spaeter,
                                        mandatsreferenz="M-1", sequenz="FRST", datei="X")
        self.assertEqual(services.mahnvorschlag(self.m, spaeter), [])
        ls.aufgehoben = True
        ls.save()
        self.assertEqual(len(services.mahnvorschlag(self.m, spaeter)), 1)

    def test_lastschrift_aufheben_speichert_benutzer_und_zeit(self):
        b = self.rechnung()
        ls = Lastschrift.objects.create(mandant=self.m, beleg=b, betrag=b.summe_brutto, einzugsdatum=date.today(),
                                        mandatsreferenz="M-1", sequenz="FRST", datei="X")
        self.client.post(reverse("finanzen:lastschrift_aufheben", args=[ls.pk]), {"grund": "Rücklastschrift"})
        ls.refresh_from_db()
        self.assertEqual((ls.aufgehoben, ls.aufgehoben_von), (True, self.user))
        self.assertIsNotNone(ls.aufgehoben_am)

    # Bankimport
    def test_identische_umsaetze_in_einer_datei_bleiben_beide_erhalten(self):
        csv_text = ("Buchungstag;Name Zahlungsbeteiligter;IBAN Zahlungsbeteiligter;Verwendungszweck;Betrag\n"
                    "24.10.2026;Kunde;DE11;Abo Oktober;50,00\n"
                    "24.10.2026;Kunde;DE11;Abo Oktober;50,00\n")
        erg = bank.importiere(self.m, "a.csv", csv_text.encode("cp1252"))
        self.assertEqual((erg["gelesen"], erg["neu"], erg["doppelt"]), (2, 2, 0))
        erg = bank.importiere(self.m, "a.csv", csv_text.encode("cp1252"))  # dieselbe Datei erneut: beides Dubletten
        self.assertEqual((erg["neu"], erg["doppelt"]), (0, 2))
        self.assertEqual(Bankumsatz.objects.count(), 2)

    # Kassenbuch
    def test_kassenbuch_art_und_negativer_stand(self):
        url = reverse("finanzen:kassenbuch")
        self.client.post(url, {"art": "X", "betrag": "10", "text": "Quatsch"})
        self.assertEqual(Kassenbuch.objects.count(), 0)
        self.client.post(url, {"art": Kassenbuch.EINNAHME, "betrag": "20", "text": "Einlage"})
        self.client.post(url, {"art": Kassenbuch.AUSGABE, "betrag": "50", "text": "zu viel"})
        self.assertEqual(Kassenbuch.objects.count(), 1)
        self.client.post(url, {"art": Kassenbuch.AUSGABE, "betrag": "20", "text": "genau der Stand"})
        self.assertEqual(services.kassenstand(self.m), Decimal("0"))
