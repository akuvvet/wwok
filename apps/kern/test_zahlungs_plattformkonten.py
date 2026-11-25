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
        self.assertContains(seite, "Zahlungskonten (PayPal/Kreditkarte)")
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
        self.assertEqual(amazon.fehlende_angaben(), ["Händler-ID", "LWA Client-ID", "LWA Client Secret", "Refresh-Token"])
        self.assertEqual(amazon.marktplatz_wert, "A1PA6795UKMFR9")
        kaufland = Plattformkonto(mandant=self.m, plattform="kaufland", bezeichnung="Kaufland", client_id="KEY")
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
                                          paypal_client_id="CID")
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
