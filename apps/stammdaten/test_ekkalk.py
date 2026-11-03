"""VK-Kalkulation mit mehreren EK-Quellen (Issue 12, Schritt 1): Szenarien 1–3 und Mandanten."""
from datetime import timedelta
from decimal import Decimal

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from apps.kern.models import Mandant

from .ekkalk import (kalkulation, kalkulations_ek, neuer_durchschnitts_ek, runden, vk_uebernahme, vk_vorschlag,
                     vk_vorschlag_aus, wareneingang_ek)
from .models import Artikel, ArtikelMandant, PreisVorgabe, Steuersatz

D = Decimal
MARGE = D("0.2")


class RechenregelTest(SimpleTestCase):
    def test_durchschnitt_gleitend(self):
        self.assertEqual(neuer_durchschnitts_ek(D(0), None, D(10), D("10")), D("10.0000"))
        self.assertEqual(neuer_durchschnitts_ek(D(4), D("10"), D(10), D("9")), D("9.2857"))

    def test_bestand_null_oder_negativ_nimmt_we_preis(self):
        self.assertEqual(neuer_durchschnitts_ek(D(0), D("7"), D(5), D("9")), D("9.0000"))
        self.assertEqual(neuer_durchschnitts_ek(D(-2), D("7"), D(5), D("9")), D("9.0000"))

    def test_szenario1_grundablauf(self):
        k = kalkulations_ek(D(10), ek_avg=D("10"))
        self.assertEqual((k.ek_kalk, k.quelle, vk_vorschlag_aus(k.ek_kalk, MARGE)), (D("10"), "ek_avg", D("12.50")))
        k = kalkulations_ek(D(10), ek_avg=D("10"), ek_import=D("8"))  # Import günstiger: max gilt
        self.assertEqual((k.ek_kalk, vk_vorschlag_aus(k.ek_kalk, MARGE)), (D("10"), D("12.50")))
        avg = neuer_durchschnitts_ek(D(4), D("10"), D(10), D("9"))
        k = kalkulations_ek(D(14), ek_avg=avg, ek_import=D("8.5"))
        self.assertEqual((k.ek_kalk, vk_vorschlag_aus(k.ek_kalk, MARGE)), (D("9.2857"), D("11.61")))
        k = kalkulations_ek(D(0), ek_avg=avg, ek_import=D("8.5"))  # Bestand 0: ek_avg zählt nicht mehr
        self.assertEqual((k.ek_kalk, k.quelle, vk_vorschlag_aus(k.ek_kalk, MARGE)), (D("8.5"), "ek_import", D("10.63")))

    def test_szenario2_extreme_wareneingaenge(self):
        avg = neuer_durchschnitts_ek(D(6), D("9.2857"), D(10), D("2"))
        self.assertEqual(avg, D("4.7321"))
        k = kalkulations_ek(D(16), ek_avg=avg, ek_import=D("8.5"))
        self.assertEqual((k.ek_kalk, vk_vorschlag_aus(k.ek_kalk, MARGE)), (D("8.5"), D("10.63")))
        avg = neuer_durchschnitts_ek(D(6), D("9.2857"), D(10), D("20"))
        self.assertEqual(avg, D("15.9821"))
        k = kalkulations_ek(D(16), ek_avg=avg, ek_import=D("8.5"))
        self.assertEqual(vk_vorschlag_aus(k.ek_kalk, MARGE), D("19.98"))
        k = kalkulations_ek(D(16), ek_avg=D("4.7321"))  # ohne Import
        self.assertEqual((k.ek_kalk.quantize(D("0.01")), vk_vorschlag_aus(k.ek_kalk, MARGE)), (D("4.73"), D("5.92")))

    def test_szenario3_ek2(self):
        k = kalkulations_ek(D(0), ek2=D("8"))
        self.assertEqual((k.ek_kalk, k.quelle, vk_vorschlag_aus(k.ek_kalk, MARGE)), (D("8"), "ek2", D("10.00")))
        k = kalkulations_ek(D(0), ek2=D("8"), ek_import=D("10"))
        self.assertEqual((k.ek_kalk, k.quelle), (D("10"), "ek_import"))
        k = kalkulations_ek(D(10), ek_avg=D("10"), ek2=D("8"), ek2_fix=True)
        self.assertEqual((k.ek_kalk, k.quelle, vk_vorschlag_aus(k.ek_kalk, MARGE)), (D("8"), "ek2_fix", D("10.00")))
        k = kalkulations_ek(D(0), ek_we=D("9"), ek2=D("8"))
        self.assertEqual((k.ek_kalk, k.quelle), (D("9"), "ek_we"))

    def test_kein_ek(self):
        k = kalkulations_ek(D(5))
        self.assertEqual((k.ek_kalk, k.quelle, vk_vorschlag_aus(k.ek_kalk, MARGE)), (None, "keine", None))

    def test_bestand_ohne_ek_avg_faellt_auf_import_dann_ek2(self):
        self.assertEqual(kalkulations_ek(D(3), ek_import=D("7")).quelle, "ek_import")
        self.assertEqual(kalkulations_ek(D(3), ek2=D("7")).quelle, "ek2")

    def test_rundung(self):
        self.assertEqual(runden(D("11.6125"), False), D("11.61"))
        self.assertEqual(runden(D("11.6150"), False), D("11.62"))
        self.assertEqual(runden(D("11.61"), True), D("11.99"))
        self.assertEqual(runden(D("12.00"), True), D("12.99"))
        self.assertEqual(runden(D("11.99"), True), D("11.99"))

    def test_vk_uebernahme(self):
        self.assertEqual(vk_uebernahme(D("12.50"), D("11.61")), "senkung_auto")  # 7,1 %
        self.assertEqual(vk_uebernahme(D("15"), D("5.92")), "senkung_pruefen")
        self.assertEqual(vk_uebernahme(D("15"), D("19.98")), "erhoehung")
        self.assertEqual(vk_uebernahme(D("15"), D("19.98"), vk_fix=True), "fix")
        self.assertEqual(vk_uebernahme(None, D("10")), "neu")
        self.assertEqual(vk_uebernahme(D("10"), D("10")), "gleich")
        self.assertEqual(vk_uebernahme(D("10"), D("9")), "senkung_auto")  # genau 10 %


class MandantTest(TestCase):
    def setUp(self):
        self.okay = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.zone = Mandant.objects.create(kurzname="ZONE", firmenname="ZONE Handel")
        self.a = Artikel.objects.create(nummer="A1", bezeichnung="Test", steuersatz=Steuersatz.objects.get(nummer=1),
                                        durchschnitts_ek=D("8.5"), ek_herkunft="import")
        PreisVorgabe.objects.all().delete()
        PreisVorgabe.objects.create(aufschlag_vk1=D("25"))  # Aufschlag 25 % = Marge 20 %

    def test_ek_avg_nur_im_mandanten_mit_wareneingang(self):
        wareneingang_ek(self.okay, self.a, D(0), D(10), D("10"))
        self.assertEqual(ArtikelMandant.objects.get(artikel=self.a, mandant=self.okay).ek_avg, D("10.0000"))
        self.assertFalse(ArtikelMandant.objects.filter(artikel=self.a, mandant=self.zone, ek_avg__isnull=False).exists())
        self.assertEqual(kalkulation(self.a, self.okay, D(10)).ek_kalk, D("10.0000"))
        zone = kalkulation(self.a, self.zone, D(0))
        self.assertEqual((zone.ek_kalk, zone.quelle), (D("8.5"), "ek_import"))

    def test_vk_vorschlag_aus_aufschlag_als_marge(self):
        self.assertEqual(vk_vorschlag(self.a, self.zone, 1, D(0)), D("10.63"))

    def test_zweiter_wareneingang_mittelt(self):
        wareneingang_ek(self.okay, self.a, D(0), D(10), D("10"))
        am = wareneingang_ek(self.okay, self.a, D(4), D(10), D("9"))
        self.assertEqual((am.ek_avg, am.ek_we), (D("9.2857"), D("9.0000")))

    def test_ek2_datum_wird_bei_aenderung_gesetzt(self):
        am = ArtikelMandant.objects.create(artikel=self.a, mandant=self.okay)
        self.assertIsNone(am.ek2_datum)
        am.ek2 = D("8")
        am.save()
        erstes = am.ek2_datum
        self.assertLess(timezone.now() - erstes, timedelta(seconds=30))
        am.ek2_fix = True  # ek2 unverändert: Datum bleibt
        am.save()
        self.assertEqual(am.ek2_datum, erstes)
        am.ek2 = D("9")
        am.save()
        self.assertGreaterEqual(am.ek2_datum, erstes)

    def test_gruppe_und_leistung_bleiben_unberuehrt(self):
        l = Artikel.objects.create(nummer="L1", typ="L", bezeichnung="Dienst", steuersatz=Steuersatz.objects.get(nummer=1), kosten=D("5"))
        self.assertEqual(kalkulation(l, self.okay).ek_kalk, D("5"))
        self.assertIsNone(wareneingang_ek(self.okay, l, D(0), D(1), D("5")))
