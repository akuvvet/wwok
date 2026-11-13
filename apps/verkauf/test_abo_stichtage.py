"""Wiederkehrende Rechnung/Bestellung mit frei wählbaren Stichtagen (statt fest 15. und Monatsende) und änderbarem Tag im Monat."""
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.kern.kalender import stichtage_parsen, zeitraum
from apps.kern.models import Mandant
from apps.stammdaten.models import Kunde, Steuersatz

from .models import Abo, AboPosition, Beleg, Belegart, Mengenregel, Position


class KalenderTest(TestCase):
    def test_parsen(self):
        self.assertEqual(stichtage_parsen("25, 10"), (10, 25))
        self.assertEqual(stichtage_parsen("1; 15 letzter"), (1, 15, 31))
        for falsch in ("", "32", "Montag"):
            with self.assertRaises(ValueError):
                stichtage_parsen(falsch)

    def test_zeitraum_ueber_den_monatswechsel(self):
        self.assertEqual(zeitraum(date(2026, 12, 10), (10, 25)), (date(2026, 11, 26), date(2026, 12, 10)))
        self.assertEqual(zeitraum(date(2026, 2, 28), (15, 31)), (date(2026, 2, 16), date(2026, 2, 28)))
        self.assertEqual(zeitraum(date(2026, 3, 5), (5,)), (date(2026, 2, 6), date(2026, 3, 5)))  # ein Stichtag = Monatszeitraum


@override_settings(SECURE_SSL_REDIRECT=False)
class AboStichtageTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.kunde = Kunde.objects.create(nummer="9001", name1="Testkunde")
        self.st = Steuersatz.objects.create(bezeichnung="19y", prozent=Decimal("19"))

    def abo(self, **werte):
        return Abo.objects.create(mandant=self.m, kunde=self.kunde, bezeichnung="Touren", monate=Abo.HALBMONATLICH, **werte)

    def test_termin_rueckt_auf_den_naechsten_freien_stichtag(self):
        abo = self.abo(stichtage="25,10", naechster_termin=date(2026, 11, 13))
        self.assertEqual((abo.stichtage, abo.naechster_termin), ("10, 25", date(2026, 11, 25)))
        AboPosition.objects.create(abo=abo, bezeichnung="Tour", regel=Mengenregel.ALLE, einzelpreis=Decimal("10"))
        abo.rechnung_erzeugen(date(2026, 11, 25))
        self.assertEqual(abo.naechster_termin, date(2026, 12, 10))
        b = Beleg.objects.get(abo=abo)
        p = Position.objects.get(beleg=b)
        self.assertEqual((p.menge, p.zusatztext), (Decimal("15"), "Abrechnungszeitraum 11.11.2026 - 25.11.2026"))

    def test_monatsende_position_im_zeitraum_mit_monatsletztem(self):
        abo = self.abo(stichtage="10, 25", naechster_termin=date(2026, 11, 25))
        AboPosition.objects.create(abo=abo, bezeichnung="Prämie", regel=Mengenregel.ALLE, nur_monatsende=True, einzelpreis=Decimal("1"))
        self.assertEqual(abo.zeilen(date(2026, 11, 25)), [])  # 11.–25.11.: kein Monatsletzter
        zeilen = abo.zeilen(date(2026, 12, 10))  # 26.11.–10.12. enthält den 30.11.: ganzer November
        self.assertEqual([(m, t) for _, m, t in zeilen], [(Decimal("30"), "Abrechnungszeitraum 01.11.2026 - 30.11.2026")])

    def test_standard_bleibt_15_und_monatsende(self):
        abo = self.abo(naechster_termin=date(2026, 11, 3))
        self.assertEqual((abo.stichtage, abo.naechster_termin), ("15, letzter", date(2026, 11, 15)))
        self.assertEqual(abo.rhythmus_text, "Stichtage 15, letzter")

    def test_ende_muss_stichtag_sein_und_ungueltige_tage(self):
        abo = Abo(mandant=self.m, kunde=self.kunde, bezeichnung="x", monate=Abo.HALBMONATLICH, stichtage="10, 25",
                  naechster_termin=date(2026, 11, 10), ende=date(2026, 11, 15))
        with self.assertRaisesMessage(ValidationError, "Stichtag"):
            abo.full_clean()
        abo.ende, abo.stichtage = date(2026, 11, 25), "10, 40"
        with self.assertRaisesMessage(ValidationError, "40"):
            abo.full_clean()

    def test_tag_im_monat_aenderbar(self):
        abo = Abo.objects.create(mandant=self.m, kunde=self.kunde, bezeichnung="Wartung", monate=1, naechster_termin=date(2026, 11, 5), starttag=5)
        abo.starttag = 20
        abo.save()
        self.assertEqual(abo.naechster_termin, date(2026, 11, 20))
        abo.starttag = 31
        abo.save()
        abo._termin_weiter(abo.naechster_termin)
        self.assertEqual(abo.naechster_termin, date(2026, 12, 31))

    def test_anlage_aus_rechnung_mit_stichtagen(self):
        u = get_user_model().objects.create_superuser("chef", "c@example.test", "x" * 14)
        self.client.force_login(u)
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()
        b = Beleg.objects.create(mandant=self.m, art=Belegart.RECHNUNG, kunde=self.kunde)
        Position.objects.create(beleg=b, typ="L", bezeichnung="Tour", menge=1, einzelpreis=Decimal("5"))
        url = reverse("verkauf:abo_aus_rechnung", args=[b.pk])
        self.client.post(url, {"monate": "15", "start": "2026-11-01", "stichtage": "10, 40"})
        self.assertFalse(Abo.objects.exists())
        self.client.post(url, {"monate": "15", "start": "2026-11-01", "stichtage": "10, 25"})
        abo = Abo.objects.get()
        self.assertEqual((abo.stichtage, abo.naechster_termin), ("10, 25", date(2026, 11, 10)))
        self.assertContains(self.client.get(reverse("verkauf:abos")), "Stichtage 10, 25")
