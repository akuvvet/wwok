from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.kern.models import Mandant
from apps.stammdaten.models import Kunde, Zahlungsbedingung
from apps.verkauf.models import Beleg, Belegart, Position

from . import bank, services
from .models import Bankumsatz, Kassenbuch, Zahlung


@override_settings(SECURE_SSL_REDIRECT=False)
class StornoTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.kunde = Kunde.objects.create(nummer="1", name1="Kunde GmbH")
        zb = Zahlungsbedingung.objects.create(bezeichnung="10 Tage", tage_netto=10, tage_mahnung=11)
        self.b = Beleg.objects.create(mandant=self.m, art=Belegart.RECHNUNG, kunde=self.kunde, zahlungsbedingung=zb)
        Position.objects.create(beleg=self.b, bezeichnung="Dienst", einzelpreis=Decimal("100"))
        self.b.abschliessen()
        self.b.refresh_from_db()

    def test_zahlung_storno_mit_grund_und_gegenbuchung(self):
        z = services.zahlung_buchen(self.b, Decimal("119"), date.today(), "ue")
        with self.assertRaises(ValueError):
            services.zahlung_stornieren(z, "  ")  # Grund ist Pflicht
        gegen = services.zahlung_stornieren(z, "Falsche Rechnung zugeordnet")
        self.assertEqual((gegen.betrag, gegen.storno_zu, gegen.storno_grund), (Decimal("-119.00"), z, "Falsche Rechnung zugeordnet"))
        self.assertEqual(services.offen(self.b), Decimal("119.00"))  # wieder offen
        with self.assertRaises(ValueError):
            services.zahlung_stornieren(z, "nochmal")  # nicht doppelt
        with self.assertRaises(ValueError):
            services.zahlung_stornieren(gegen, "Storno zurück")  # Storno nicht erneut stornierbar
        self.assertEqual(Zahlung.objects.count(), 2)  # nichts gelöscht

    def test_bar_storno_laeuft_ins_kassenbuch_und_bank_wird_frei(self):
        z = services.zahlung_buchen(self.b, Decimal("119"), date.today(), "ba")
        services.zahlung_stornieren(z, "Kunde hat zurückgefordert")
        self.assertEqual(services.kassenstand(self.m), Decimal("0.00"))
        self.assertEqual([k.art for k in Kassenbuch.objects.order_by("nummer")], ["E", "A"])
        self.assertIn("Storno", Kassenbuch.objects.get(art="A").text)
        csv_text = f"Buchungstag;Betrag;Name;Verwendungszweck\n24.10.2026;119,00;Kunde;{self.b.nummer}\n"
        bank.importiere(self.m, "a.csv", csv_text.encode("cp1252"))
        umsatz = Bankumsatz.objects.get()
        self.assertEqual(umsatz.status, "zugeordnet")
        services.zahlung_stornieren(umsatz.zahlung, "Umsatz falsch zugeordnet")
        umsatz.refresh_from_db()
        self.assertEqual((umsatz.status, umsatz.zahlung), ("offen", None))  # kann neu zugeordnet werden

    def test_maske_storno(self):
        self.client.force_login(get_user_model().objects.create_user("t", password="x" * 14, is_staff=True))
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()
        z = services.zahlung_buchen(self.b, Decimal("50"), date.today(), "ue")
        self.assertContains(self.client.get(reverse("finanzen:beleg", args=[self.b.pk])), "Stornogrund")
        self.client.post(reverse("finanzen:zahlung_storno", args=[z.pk]), {"grund": ""})
        self.assertEqual(Zahlung.objects.count(), 1)
        self.client.post(reverse("finanzen:zahlung_storno", args=[z.pk]), {"grund": "Irrtum"})
        self.assertEqual(Zahlung.objects.count(), 2)
        self.assertContains(self.client.get(reverse("finanzen:beleg", args=[self.b.pk])), "Irrtum")

    def test_mitarbeiter_darf_nicht_stornieren(self):
        self.client.force_login(get_user_model().objects.create_user("m", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()
        z = services.zahlung_buchen(self.b, Decimal("50"), date.today(), "ue")
        antwort = self.client.post(reverse("finanzen:zahlung_storno", args=[z.pk]), {"grund": "Irrtum"})
        self.assertEqual(antwort.status_code, 302)
        self.assertEqual(Zahlung.objects.count(), 1)  # nichts storniert
