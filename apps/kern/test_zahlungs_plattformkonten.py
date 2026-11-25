"""Zahlungskonten (PayPal, Kreditkarte) und Plattformkonten (eBay, Amazon, Kaufland) je Mandant (Wunsch des Nutzers 25.11.2026)."""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import AenderungsProtokoll, Mandant, Plattformkonto, Zahlungskonto



@override_settings(SECURE_SSL_REDIRECT=False)
class ZahlungsUndPlattformkontenTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.client.force_login(get_user_model().objects.create_superuser("chef", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()
        self.url = reverse("admin:kern_mandant_change", args=[self.m.pk])

    def formular(self, **zusatz):
        """POST-Daten der Mandantenmaske samt leerer Inline-Verwaltung."""
        daten = {"kurzname": "OKAY", "firmenname": "OKAY Computer", "land": "DE", "aktiv": "on", "farbe": "#1d4ed8",
                 "flaechenmodus": self.m.flaechenmodus, "standard_vk": self.m.standard_vk, "smtp_port": "587",
                 "smtp_sicherheit": self.m.smtp_sicherheit, "konto_erloes_19": "8400", "konto_erloes_7": "8300",
                 "konto_erloes_0": "8120", "konto_bank": "1200", "konto_kasse": "1000", "konto_skonto": "8736"}
        for praefix in ("zahlungskonten", "plattformkonten"):
            daten.update({f"{praefix}-TOTAL_FORMS": "0", f"{praefix}-INITIAL_FORMS": "0",
                          f"{praefix}-MIN_NUM_FORMS": "0", f"{praefix}-MAX_NUM_FORMS": "1000"})
        daten.update(zusatz)
        return daten

    def test_maske_zeigt_beide_bereiche_und_shopware(self):
        seite = self.client.get(self.url)
        self.assertContains(seite, "Konten (Bank, PayPal, Karte …)")
        self.assertContains(seite, "Plattformkonten (eBay/Amazon/Kaufland)")
        self.assertContains(seite, "+ Shopware-Shop anlegen")

    def test_zwei_ebay_konten_mit_verschluesseltem_secret(self):
        daten = self.formular(**{
            "plattformkonten-TOTAL_FORMS": "2",
            "plattformkonten-0-plattform": "ebay", "plattformkonten-0-bezeichnung": "eBay Account 1", "plattformkonten-0-aktiv": "on",
            "plattformkonten-0-umgebung": "live", "plattformkonten-0-client_id": "APP-1", "plattformkonten-0-client_secret_neu": "geheim-1",
            "plattformkonten-0-preisstufe": "1", "plattformkonten-0-preisaufschlag": "0", "plattformkonten-0-bestand_puffer": "0",
            "plattformkonten-1-plattform": "ebay", "plattformkonten-1-bezeichnung": "eBay Account 2", "plattformkonten-1-aktiv": "on",
            "plattformkonten-1-umgebung": "live", "plattformkonten-1-preisstufe": "2", "plattformkonten-1-preisaufschlag": "8.5",
            "plattformkonten-1-bestand_puffer": "1"})
        antwort = self.client.post(self.url, daten)
        self.assertEqual(antwort.status_code, 302, antwort.content[:3000])
        erstes = Plattformkonto.objects.get(bezeichnung="eBay Account 1")
        self.assertEqual(Plattformkonto.objects.filter(mandant=self.m, plattform="ebay").count(), 2)
        self.assertEqual(erstes.geheimnis("client_secret"), "geheim-1")
        self.assertNotIn("geheim-1", erstes.client_secret_geheim)
        seite = self.client.get(self.url).content.decode()
        self.assertNotIn("geheim-1", seite)
        self.assertIn("fehlt: RuName, Refresh-Token", seite)
        self.assertFalse(AenderungsProtokoll.objects.filter(nachher__icontains="geheim-1").exists())

    def test_fehlende_angaben_je_plattform(self):
        amazon = Plattformkonto(mandant=self.m, plattform="amazon", bezeichnung="Amazon")
        self.assertEqual(amazon.fehlende_angaben(), ["Händler-ID", "LWA Client-ID", "LWA Client Secret", "Refresh-Token", "DATEV-Konto"])
        self.assertEqual(amazon.marktplatz_wert, "A1PA6795UKMFR9")
        kaufland = Plattformkonto(mandant=self.m, plattform="kaufland", bezeichnung="Kaufland", client_id="KEY", datev_konto="1250")
        kaufland.geheimnis_setzen("client_secret", "SECRET")
        self.assertEqual((kaufland.fehlende_angaben(), kaufland.marktplatz_wert), ([], "de"))

    def test_kreditkarte_nur_letzte_vier_ziffern(self):
        karte = Zahlungskonto(mandant=self.m, art="karte", bezeichnung="Firmen-Visa", karte_anbieter="visa",
                              karte_letzte4="4242", karte_ablauf="08/28")
        karte.full_clean()
        self.assertEqual(str(karte), "Firmen-Visa ••••4242")
        for feld, wert in (("karte_letzte4", "42424"), ("karte_ablauf", "13/28"), ("bemerkung", "Nr. 4111 1111 1111 1111"),
                           ("karte_inhaber", "4111111111111111")):
            with self.subTest(feld):
                falsch = Zahlungskonto(**{**{f.name: getattr(karte, f.name) for f in Zahlungskonto._meta.concrete_fields
                                             if f.name not in ("id",)}, feld: wert})
                with self.assertRaises(ValidationError):
                    falsch.full_clean()

    def test_paypal_secret_verschluesselt_und_entfernbar(self):
        pp = Zahlungskonto.objects.create(mandant=self.m, art="paypal", bezeichnung="PayPal OKAY", paypal_email="pp@okay.test",
                                          paypal_client_id="CID", datev_konto="1240")
        self.assertEqual(pp.fehlende_angaben(), ["Secret"])
        pp.secret_setzen("pp-geheim")
        pp.save()
        pp.refresh_from_db()
        self.assertEqual((pp.secret, pp.fehlende_angaben()), ("pp-geheim", []))
        self.assertNotIn("pp-geheim", pp.paypal_secret_geheim)

    def test_kartenfelder_nur_bei_kreditkarte(self):
        daten = self.formular(**{
            "zahlungskonten-TOTAL_FORMS": "1", "zahlungskonten-0-art": "paypal", "zahlungskonten-0-bezeichnung": "PayPal",
            "zahlungskonten-0-aktiv": "on", "zahlungskonten-0-paypal_umgebung": "live", "zahlungskonten-0-karte_letzte4": "1234"})
        self.assertContains(self.client.post(self.url, daten), "Nur bei Art „Kreditkarte“.")

    def test_token_ablauf_wird_angezeigt(self):
        Plattformkonto.objects.create(mandant=self.m, plattform="ebay", bezeichnung="eBay 1",
                                      refresh_token_bis=timezone.localdate() + timedelta(days=10))
        self.assertContains(self.client.get(self.url), "Refresh-Token läuft in 10 Tagen ab")

    # ---- 0.34.0: Konten erweiterbar (Bank), DATEV-Konten laut Steuerberater, Kostenstellen -------------------
    def test_mehrere_bankkonten_mit_datev_konto(self):
        for name, iban, konto in (("Commerzbank", "DE89 3704 0044 0532 0130 00", "1200"), ("Postbank", "", "1100")):
            k = Zahlungskonto(mandant=self.m, art="bank", bezeichnung=name, bank_name=name, iban=iban,
                              kontonummer="" if iban else "0253 2544 61", datev_konto=konto)
            k.full_clean()
            k.save()
        self.assertEqual(Zahlungskonto.objects.get(bezeichnung="Commerzbank").iban, "DE89370400440532013000")
        self.assertEqual([k.fehlende_angaben() for k in Zahlungskonto.objects.filter(art="bank")], [[], []])
        self.assertEqual(Zahlungskonto(mandant=self.m, art="bank", bezeichnung="X").fehlende_angaben(),
                         ["IBAN oder Kontonummer", "DATEV-Konto"])
        with self.assertRaises(ValidationError):
            Zahlungskonto(mandant=self.m, art="bank", bezeichnung="Falsch", iban="1234", datev_konto="1210").full_clean()

    def test_kartenpruefung_laesst_iban_und_kontonummer_zu(self):
        from .models import keine_kartennummer
        for erlaubt in ("DE89 3704 0044 0532 0130 00", "Konto 0363 8509 02", "Referenz 123456789012345"):
            keine_kartennummer(erlaubt)
        for verboten in ("4111 1111 1111 1111", "5500-0000-0000-0004", "378282246310005"):
            with self.subTest(verboten), self.assertRaises(ValidationError):
                keine_kartennummer(verboten)

    def test_kostenstellen_zuordnung_und_pruefung(self):
        self.m.kostenstellen_konten = "Apetito = 8401\n\nWerkstatt=8402"
        self.assertEqual(self.m.kostenstellen_zuordnung(), {"apetito": "8401", "werkstatt": "8402"})
        from .testhilfe import MANDANT_INLINES_LEER
        daten = {**self.formular(), **MANDANT_INLINES_LEER, "kostenstellen_konten": "Apetito 8401"}
        self.assertContains(self.client.post(self.url, daten), "Zeile 1: bitte „Kostenstelle = Kontonummer“")
