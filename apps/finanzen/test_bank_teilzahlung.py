"""Bankimport: Teilzahlungen und Überweisungen für mehrere Rechnungen werden gebucht und mindern den offenen Betrag beim Kunden."""
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.kern.models import Mandant
from apps.stammdaten.models import Kunde, Zahlungsbedingung
from apps.verkauf.models import Beleg, Belegart, Position

from . import bank, services
from .models import Bankumsatz, Zahlung

KOPF = "Buchungstag;Valuta;Name Zahlungsbeteiligter;IBAN Zahlungsbeteiligter;Verwendungszweck;Betrag\n"


@override_settings(SECURE_SSL_REDIRECT=False)
class BankTeilzahlungTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.kunde = Kunde.objects.create(nummer="1", name1="Kunde GmbH")
        self.zb = Zahlungsbedingung.objects.create(bezeichnung="10 Tage x", tage_netto=10)
        self.a, self.b = self.rechnung("100"), self.rechnung("200")  # 119,00 und 238,00 brutto
        self.client.force_login(get_user_model().objects.create_superuser("t", "t@example.test", "x" * 14))
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    def rechnung(self, netto):
        b = Beleg.objects.create(mandant=self.m, art=Belegart.RECHNUNG, kunde=self.kunde, zahlungsbedingung=self.zb)
        Position.objects.create(beleg=b, bezeichnung="Dienst", einzelpreis=Decimal(netto))
        b.abschliessen()
        b.refresh_from_db()
        return b

    def importieren(self, zweck, betrag):
        return bank.importiere(self.m, "a.csv", (KOPF + f"05.11.2026;05.11.2026;Kunde GmbH;DE02;{zweck};{betrag}\n").encode("cp1252"))

    def test_teilzahlung_mindert_offen_beim_kunden(self):
        self.importieren(f"Teilzahlung {self.b.nummer}", "100,00")
        self.assertEqual(services.offen(self.b), Decimal("138.00"))
        self.assertEqual(services.kunde_offen(self.m, self.kunde), Decimal("257.00"))
        r = self.client.get(reverse("verkauf:kunde", args=[self.kunde.pk]))
        self.assertEqual(r.context["offen"], Decimal("257.00"))

    def test_mehrere_rechnungen_werden_der_reihe_nach_bedient(self):
        self.importieren(f"{self.a.nummer} und {self.b.nummer}", "200,00")
        self.assertEqual((services.offen(self.a), services.offen(self.b)), (Decimal("0.00"), Decimal("157.00")))
        u = Bankumsatz.objects.get()
        self.assertEqual(u.status, Bankumsatz.ZUGEORDNET)
        self.assertEqual(sorted(z.betrag for z in u.zahlungen.all()), [Decimal("81.00"), Decimal("119.00")])
        self.assertContains(self.client.get(reverse("finanzen:bank") + "?status=zugeordnet"), f"{self.b.nummer} (81,00 €)")

    def test_ueberzahlung_bleibt_vorschlag(self):
        self.importieren(f"{self.a.nummer}", "150,00")
        u = Bankumsatz.objects.get()
        self.assertEqual((u.status, u.vorschlag), (Bankumsatz.OFFEN, self.a))
        self.assertEqual(services.offen(self.a), Decimal("119.00"))

    def test_von_hand_mehrere_nummern(self):
        self.importieren("ohne Bezug", "300,00")
        u = Bankumsatz.objects.get()
        self.client.post(reverse("finanzen:bank_zuordnen", args=[u.pk]), {"rechnung": f"{self.a.nummer}, {self.b.nummer}"})
        self.assertEqual((services.offen(self.a), services.offen(self.b)), (Decimal("0.00"), Decimal("57.00")))

    def test_von_hand_ueberzahlung_bleibt_auf_der_letzten(self):
        self.importieren("ohne Bezug", "400,00")
        u = Bankumsatz.objects.get()
        bank.zuordnen(u, [self.a, self.b])
        self.assertEqual((services.offen(self.a), services.offen(self.b)), (Decimal("0.00"), Decimal("-43.00")))

    def test_nachbuchen_alter_vorschlaege(self):
        # Stand vor der Änderung: Teilbetrag nur als Vorschlag gespeichert
        u = Bankumsatz.objects.create(mandant=self.m, buchungsdatum=date(2026, 11, 5), betrag=Decimal("50"), zweck=f"Rate {self.b.nummer}",
                                      pruefsumme="alt", vorschlag=self.b)
        self.client.post(reverse("finanzen:bank_nachbuchen"))
        u.refresh_from_db()
        self.assertEqual(u.status, Bankumsatz.ZUGEORDNET)
        self.assertEqual(services.offen(self.b), Decimal("188.00"))

    def test_storno_gibt_aufgeteilten_umsatz_erst_ganz_frei(self):
        self.importieren(f"{self.a.nummer} {self.b.nummer}", "200,00")
        u = Bankumsatz.objects.get()
        erste, zweite = u.zahlungen.order_by("pk")
        services.zahlung_stornieren(erste, "falsch zugeordnet")
        u.refresh_from_db()
        self.assertEqual((u.status, u.zahlung), (Bankumsatz.ZUGEORDNET, zweite))
        services.zahlung_stornieren(zweite, "falsch zugeordnet")
        u.refresh_from_db()
        self.assertEqual((u.status, u.zahlung), (Bankumsatz.OFFEN, None))
        self.assertEqual(Zahlung.objects.filter(aus_bankumsatz=u, storno_zu__isnull=True).count(), 2)  # nichts gelöscht
