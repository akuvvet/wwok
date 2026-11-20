"""Kunden-Skonto (Prüfbericht A7): Zahlung in der Skontofrist abzüglich Skonto gleicht die Rechnung aus."""
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.kern.models import Mandant
from apps.lager.models import Bewegungsart
from apps.lager.services import buche_manuell
from apps.stammdaten.models import Artikel, Kunde, Steuersatz, Zahlungsbedingung
from apps.verkauf.models import Beleg, Belegart, Position

from . import bank, datev, services
from .models import Bankumsatz, Zahlart


@override_settings(SECURE_SSL_REDIRECT=False)
class SkontoTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.kunde = Kunde.objects.create(nummer="10001", name1="Kunde GmbH")
        self.st = Steuersatz.objects.create(bezeichnung="19", prozent=Decimal("19"))
        self.a = Artikel.objects.create(nummer="A1", bezeichnung="Maus", steuersatz=self.st, vk1=Decimal("10"))
        buche_manuell(self.m, self.a, Bewegungsart.WARENEINGANG, Decimal("100"))
        self.zb = Zahlungsbedingung.objects.create(bezeichnung="10 Tage 2 %, 30 Tage netto", tage_netto=30, skonto_tage=10,
                                                   skonto_prozent=Decimal("2"))
        self.client.force_login(get_user_model().objects.create_superuser("chef", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    def rechnung(self, zb="ja"):
        b = Beleg.objects.create(mandant=self.m, art=Belegart.RECHNUNG, kunde=self.kunde, belegdatum=date(2026, 11, 1),
                                 zahlungsbedingung=self.zb if zb == "ja" else None)
        Position.objects.create(beleg=b, artikel=self.a, bezeichnung="Maus", menge=Decimal("10"), einzelpreis=Decimal("10"))
        b.abschliessen()
        Beleg.objects.filter(pk=b.pk).update(belegdatum=date(2026, 11, 1))  # der Abschluss setzt das Tagesdatum: festes Datum für die Frist
        b.refresh_from_db()
        self.assertEqual(b.summe_brutto, Decimal("119.00"))
        return b

    def skonti(self, b):
        return list(b.zahlungen.filter(zahlart=Zahlart.SKONTO).values_list("betrag", flat=True))

    def test_zahlung_in_der_frist_mit_abzug_gleicht_aus(self):
        b = self.rechnung()
        self.assertEqual(services.skonto(b), (Decimal("2.38"), date(2026, 11, 11)))
        z = services.zahlung_buchen(b, Decimal("116.62"), date(2026, 11, 11), Zahlart.UEBERWEISUNG)
        self.assertEqual(z.skonto.betrag, Decimal("2.38"))
        self.assertEqual((self.skonti(b), services.offen(b)), ([Decimal("2.38")], Decimal("0.00")))
        self.assertEqual(services.offene_rechnungen(self.m), [])  # nicht mehr offen, also auch kein Mahnvorschlag
        self.assertIn("Skonto 2 %", b.zahlungen.get(zahlart=Zahlart.SKONTO).bemerkung)

    def test_kleinerer_abzug_wird_gewaehrt_groesserer_nicht(self):
        b = self.rechnung()
        services.zahlung_buchen(b, Decimal("118.00"), date(2026, 11, 5), Zahlart.UEBERWEISUNG)
        self.assertEqual((self.skonti(b), services.offen(b)), ([Decimal("1.00")], Decimal("0.00")))
        c = self.rechnung()
        services.zahlung_buchen(c, Decimal("116.00"), date(2026, 11, 5), Zahlart.UEBERWEISUNG)  # 3,00 € abgezogen
        self.assertEqual((self.skonti(c), services.offen(c)), ([], Decimal("3.00")))

    def test_nach_der_frist_und_ohne_skontobedingung_bleibt_der_rest_offen(self):
        b = self.rechnung()
        z = services.zahlung_buchen(b, Decimal("116.62"), date(2026, 11, 12), Zahlart.UEBERWEISUNG)
        self.assertIsNone(z.skonto)
        self.assertEqual(services.offen(b), Decimal("2.38"))
        services.zahlung_buchen(b, Decimal("2.38"), date(2026, 11, 20), Zahlart.SKONTO, "Kulanz")  # von Hand nach der Frist
        self.assertEqual(services.offen(b), Decimal("0.00"))
        ohne = self.rechnung(zb="nein")
        services.zahlung_buchen(ohne, Decimal("116.62"), date(2026, 11, 2), Zahlart.UEBERWEISUNG)
        self.assertEqual((self.skonti(ohne), services.offen(ohne)), ([], Decimal("2.38")))

    def test_teilzahlungen_skonto_erst_mit_der_letzten(self):
        b = self.rechnung()
        services.zahlung_buchen(b, Decimal("50.00"), date(2026, 11, 3), Zahlart.UEBERWEISUNG)
        self.assertEqual(self.skonti(b), [])
        services.zahlung_buchen(b, Decimal("66.62"), date(2026, 11, 9), Zahlart.BAR)
        self.assertEqual((self.skonti(b), services.offen(b)), ([Decimal("2.38")], Decimal("0.00")))
        from .models import Kassenbuch
        self.assertEqual(Kassenbuch.objects.count(), 1)  # Skonto ist kein Geld: nicht im Kassenbuch

    def test_storno_der_zahlung_nimmt_das_skonto_zurueck(self):
        b = self.rechnung()
        z = services.zahlung_buchen(b, Decimal("116.62"), date(2026, 11, 5), Zahlart.UEBERWEISUNG)
        services.zahlung_stornieren(z, "falsche Rechnung")
        self.assertEqual(services.offen(b), Decimal("119.00"))
        self.assertFalse(services.aktive_skonti(b).exists())
        services.zahlung_buchen(b, Decimal("116.62"), date(2026, 11, 6), Zahlart.UEBERWEISUNG)  # erneut: Skonto wieder gewährt
        self.assertEqual(services.offen(b), Decimal("0.00"))

    def test_bankabgleich_gewaehrt_skonto(self):
        b = self.rechnung()
        u = Bankumsatz.objects.create(mandant=self.m, buchungsdatum=date(2026, 11, 10), betrag=Decimal("116.62"),
                                      name="Kunde GmbH", zweck=f"{b.nummer} abzgl. 2 % Skonto", pruefsumme="s1")
        self.assertTrue(bank.zuordnen_automatisch(u))
        u.refresh_from_db()
        self.assertEqual((u.status, services.offen(b), self.skonti(b)), (Bankumsatz.ZUGEORDNET, Decimal("0.00"), [Decimal("2.38")]))
        self.assertEqual(u.zahlungen.count(), 1)  # nur der Geldeingang hängt am Umsatz

    def test_maske_und_datev(self):
        b = self.rechnung()
        seite = self.client.get(reverse("finanzen:beleg", args=[b.pk]))
        self.assertContains(seite, "Skonto bis 11.11.2026")
        self.assertContains(seite, "Skontofrist laut Zahlungsbedingung: bis 11.11.2026")  # die Maske schlägt den Rest im Feld Skonto vor
        antwort = self.client.post(reverse("finanzen:zahlung", args=[b.pk]),
                                   {"betrag": "116,62", "datum": "2026-11-08", "zahlart": "ue", "offen_bisher": "119,00"}, follow=True)
        self.assertContains(antwort, "2,38 € Skonto gewährt")
        self.assertEqual(services.offen(b), Decimal("0.00"))
        text = datev.export(self.m, date(2026, 11, 1), date(2026, 11, 30)).decode("cp1252")
        zeile = next(z for z in text.splitlines() if "Skonto Kunde GmbH" in z)
        self.assertTrue(zeile.startswith("2,38;"))
        self.assertIn(";8736;10001;", zeile)  # Skontokonto an Debitor, nicht Bank
        self.assertIn(";1200;10001;", next(z for z in text.splitlines() if "Zahlung Kunde GmbH" in z))


@override_settings(SECURE_SSL_REDIRECT=False)
class SkontoFeldTest(SkontoTest):
    """Wunsch des Nutzers 19.11.2026: Feld „Skonto“ in „Zahlung buchen“. Rechnung 100 €, Zahlung 80 € → 20 € stehen als Skonto;
    so gelassen ist die Rechnung bezahlt, auf 0 gesetzt bleiben 20 € offen."""

    def buchen(self, b, betrag, skonto, **extra):
        daten = {"betrag": betrag, "skonto": skonto, "datum": "2026-11-20", "zahlart": Zahlart.UEBERWEISUNG, **extra}
        return self.client.post(reverse("finanzen:zahlung", args=[b.pk]), daten, follow=True)

    def test_maske_hat_feld_skonto_mit_vorschlag(self):
        b = self.rechnung()
        r = self.client.get(reverse("finanzen:beleg", args=[b.pk]))
        self.assertContains(r, 'name="skonto" value="0,00"')
        self.assertContains(r, 'name="offen_bisher" value="119,00" data-wert="offen"')  # Vorschlag über die gemeinsame Live-Rechnung
        self.assertContains(r, 'data-ergibt="wenn(betrag > 0, max(runde(offen - betrag), 0), 0)"')
        self.assertContains(r, "Skonto (gewährter Rest)")

    def test_rest_als_skonto_gleicht_aus(self):
        b = self.rechnung()
        r = self.buchen(b, "80,00", "39,00")
        self.assertContains(r, "39,00 € Skonto gewährt")
        self.assertContains(r, "ausgeglichen")
        self.assertEqual((self.skonti(b), services.offen(b)), ([Decimal("39.00")], Decimal("0.00")))

    def test_skonto_null_laesst_den_rest_offen(self):
        b = self.rechnung()
        r = self.buchen(b, "80,00", "0")
        self.assertContains(r, "Noch offen: 39,00 €")
        self.assertEqual((self.skonti(b), services.offen(b)), ([], Decimal("39.00")))

    def test_skonto_null_auch_in_der_skontofrist(self):
        b = self.rechnung()  # 2 % bis 11.11.: die Maske entscheidet, kein automatisches Skonto
        self.client.post(reverse("finanzen:zahlung", args=[b.pk]),
                         {"betrag": "116,62", "skonto": "0", "datum": "2026-11-05", "zahlart": Zahlart.UEBERWEISUNG})
        self.assertEqual((self.skonti(b), services.offen(b)), ([], Decimal("2.38")))

    def test_teil_skonto_und_zu_viel(self):
        b = self.rechnung()
        self.buchen(b, "80,00", "9,00")
        self.assertEqual((self.skonti(b), services.offen(b)), ([Decimal("9.00")], Decimal("30.00")))
        c = self.rechnung()
        r = self.buchen(c, "80,00", "40,00")
        self.assertContains(r, "mehr als offen")
        self.assertFalse(c.zahlungen.exists())  # nichts gebucht, auch nicht die Zahlung
        r = self.buchen(c, "80,00", "-1")
        self.assertContains(r, "nicht negativ")

    def test_skonto_nur_mit_zahlungseingang(self):
        b = self.rechnung()
        self.buchen(b, "119,00", "0")
        r = self.buchen(b, "-10,00", "5,00")
        self.assertContains(r, "nur zusammen mit einem Zahlungseingang")

    def test_storno_der_zahlung_nimmt_das_skonto_mit(self):
        b = self.rechnung()
        self.buchen(b, "80,00", "39,00")
        zahlung = b.zahlungen.exclude(zahlart=Zahlart.SKONTO).get()
        services.zahlung_stornieren(zahlung, "Fehlbuchung")
        self.assertEqual(services.offen(b), Decimal("119.00"))

    def test_ohne_feld_bleibt_alles_wie_bisher(self):
        b = self.rechnung()  # z. B. Bankabgleich oder alte Maske: automatisches Skonto in der Frist
        self.client.post(reverse("finanzen:zahlung", args=[b.pk]),
                         {"betrag": "116,62", "datum": "2026-11-05", "zahlart": Zahlart.UEBERWEISUNG})
        self.assertEqual(self.skonti(b), [Decimal("2.38")])
