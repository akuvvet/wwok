"""Preis-Radar (Issue 12, Schritt 3): Kategorien, Euro-Wert, Ruhe-Regeln (Szenarien 2, 3, 5), Gesehen, Dashboard und Liste."""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.kern.models import Mandant
from apps.lager.models import Bewegungsart, Lagerbewegung
from apps.verkauf.models import Beleg, Belegart, Position

from . import radar
from .models import Artikel, ArtikelMandant, Kunde, PreisVorgabe, RadarGesehen, Steuersatz

D = Decimal
EINST = {"faktor": D("0.85"), "ek2_abweichung": D("20"), "ek2_tage": 180, "prozent": D("3"), "euro": D("0.50")}
MARGE = D("0.2")


def pruefe(bestand, quelle, ek_kalk, vk, vorschlag=None, vk_fix=True, **kw):
    kw.setdefault("ek_avg", None)
    kw.setdefault("ek_import_wert", None)
    return radar.bewerte(bestand=D(bestand), ek_kalk=ek_kalk, quelle=quelle, vk=vk, vorschlag=vorschlag, vk_fix=vk_fix,
                         ziel_marge=MARGE, einst=EINST, **kw)


def kategorien(ergebnis):
    return [b.kategorie for b in ergebnis[2]]


class BewertungTest(SimpleTestCase):
    def test_szenario1_preisverfall_blau(self):
        basis, quelle, befunde = pruefe(10, "ek_avg", D("10"), D("12.50"), D("12.50"), ek_avg=D("10"), ek_import_wert=D("8"))
        self.assertEqual((basis, quelle), (D("10"), "Ø-EK"))
        self.assertEqual([(b.kategorie, b.diff) for b in befunde], [("verfall", D("2"))])

    def test_szenario2_verlust_rot_mit_euro_wert(self):
        ergebnis = pruefe(16, "ek_avg", D("15.9821"), D("15"), D("19.98"), ek_avg=D("15.9821"), ek_import_wert=D("8.5"))
        self.assertEqual(kategorien(ergebnis), ["verlust", "verfall"])  # Import 8,50 liegt zudem unter Ø-EK × 0,85
        self.assertEqual(ergebnis[2][0].diff, D("0.9821"))
        self.assertEqual((D(16) * ergebnis[2][0].diff).quantize(D("0.01")), D("15.71"))

    def test_szenario2_niedriger_avg_keine_warnung(self):
        self.assertEqual(kategorien(pruefe(6, "ek_import", D("8.5"), D("15"), D("10.63"), ek_avg=D("4.7321"), ek_import_wert=D("8.5"))), [])

    def test_szenario3_nur_ek2(self):
        basis, quelle, befunde = pruefe(0, "ek2", D("8"), None, D("10"), vk_fix=False, ek2=D("8"), ek2_datum=timezone.now())
        self.assertEqual((basis, quelle, befunde), (D("8"), "EK2", []))

    def test_szenario3_vk9_gelb_vk75_rot(self):
        gelb = pruefe(0, "ek2", D("8"), D("9"), D("10"), ek2=D("8"), ek2_datum=timezone.now())
        self.assertEqual([(b.kategorie, b.diff) for b in gelb[2]], [("marge", D("1"))])
        rot = pruefe(0, "ek2", D("8"), D("7.50"), D("10"), ek2=D("8"), ek2_datum=timezone.now())
        self.assertEqual([(b.kategorie, b.diff) for b in rot[2]], [("verlust", D("0.50"))])

    def test_szenario3_import_nach_ek2_abweichung(self):
        ergebnis = pruefe(0, "ek_import", D("10"), None, D("12.50"), vk_fix=False, ek_import_wert=D("10"), ek2=D("8"), ek2_datum=timezone.now())
        self.assertEqual(kategorien(ergebnis), ["ek_abweichung"])

    def test_szenario3_ek2_fix_mit_we_gelb_basis_avg(self):
        basis, quelle, befunde = pruefe(10, "ek2_fix", D("8"), D("10"), D("10"), ek_avg=D("10"), ek2=D("8"), ek2_datum=timezone.now())
        self.assertEqual((basis, quelle), (D("10"), "Ø-EK"))
        self.assertEqual([(b.kategorie, b.diff) for b in befunde], [("marge", D("2.5"))])

    def test_szenario3_ek2_alt_und_kein_ek(self):
        alt = pruefe(0, "ek2", D("8"), None, D("10"), vk_fix=False, ek2=D("8"), ek2_datum=timezone.now() - timedelta(days=200))
        self.assertEqual(kategorien(alt), ["ek2_pruefen"])
        frisch = pruefe(0, "ek2", D("8"), None, D("10"), vk_fix=False, ek2=D("8"), ek2_datum=timezone.now() - timedelta(days=100))
        self.assertEqual(kategorien(frisch), [])
        self.assertEqual(kategorien(pruefe(0, "keine", None, None)), ["kein_ek"])

    def test_preiserhoehung_moeglich_gruen(self):
        ergebnis = pruefe(5, "ek_avg", D("12"), D("15"), D("19"), ek_avg=D("12"))  # vk_fix, Marge 20 % gehalten, Vorschlag höher
        self.assertEqual([(b.kategorie, b.diff) for b in ergebnis[2]], [("erhoehung", D("4"))])
        ohne_fix = pruefe(5, "ek_avg", D("12"), D("15"), D("19"), vk_fix=False, ek_avg=D("12"))
        self.assertEqual(kategorien(ohne_fix), [])  # ohne vk_fix übernimmt das System den Preis selbst

    def test_szenario5_kleine_aenderungen_schweigen(self):
        # 2 % Preisverfall (9,80 statt 10,00): kein Befund
        self.assertEqual(kategorien(pruefe(10, "ek_avg", D("10"), D("12.5"), D("12.5"), ek_avg=D("10"), ek_import_wert=D("9.8"))), [])
        # 0,30 € bei 20 € EK: unter 0,50 €, kein Verlust-Befund
        self.assertEqual(kategorien(pruefe(10, "ek_avg", D("20"), D("19.70"), D("25"), ek_avg=D("20"))), [])


class RadarDatenTest(TestCase):
    def setUp(self):
        self.okay = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.zone = Mandant.objects.create(kurzname="ZONE", firmenname="ZONE Handel")
        PreisVorgabe.objects.all().delete()
        PreisVorgabe.objects.create(aufschlag_vk1=D("25"))  # Marge 20 %
        self.st = Steuersatz.objects.get(nummer=1)

    def artikel(self, nummer="A1", typ="A", **kw):
        return Artikel.objects.create(nummer=nummer, bezeichnung=f"Artikel {nummer}", typ=typ, steuersatz=self.st, **kw)

    def bestand(self, mandant, artikel, menge):
        Lagerbewegung.objects.create(mandant=mandant, artikel=artikel, art=Bewegungsart.WARENEINGANG, menge=D(menge))

    def verlustartikel(self, nummer="A1", **kw):
        a = self.artikel(nummer, vk1=D("15"), **kw)
        self.bestand(self.okay, a, 16)
        ArtikelMandant.objects.create(artikel=a, mandant=self.okay, ek_avg=D("15.9821"), vk_fix1=True)
        return a

    def test_euro_wert_mit_offenen_belegen(self):
        a = self.verlustartikel()
        kunde_beleg = Beleg.objects.create(mandant=self.okay, art=Belegart.AUFTRAG, kunde=Kunde.objects.create(nummer="1", name1="K"))
        Position.objects.create(beleg=kunde_beleg, artikel=a, bezeichnung="x", menge=D("4"), einzelpreis=D("15"))
        e = radar.eintrag(a, self.okay)
        self.assertEqual(e.ampel, "rot")
        self.assertEqual(e.menge, D("20"))
        self.assertEqual(e.euro(e.befunde[0]), D("19.64"))  # 20 × 0,9821

    def test_mandantentrennung(self):
        a = self.verlustartikel()
        self.assertEqual(radar.eintrag(a, self.okay).ampel, "rot")
        self.assertEqual(radar.eintrag(a, self.zone).ampel, "grau")  # ZONE: kein EK
        self.assertEqual(radar.auswertung(self.zone)["rot"], 0)
        self.assertEqual(radar.auswertung(self.okay)["rot"], 1)

    def test_warnungen_aus_nur_ampel(self):
        a = self.verlustartikel()
        ArtikelMandant.objects.filter(artikel=a, mandant=self.okay).update(warnungen_aus=True)
        e = radar.eintrag(a, self.okay)
        self.assertEqual(e.ampel, "rot")
        self.assertEqual(radar.auswertung(self.okay)["rot"], 0)
        self.assertEqual(radar.zeilen(self.okay, "verlust"), [])

    def test_gesehen_blendet_aus_bis_aenderung(self):
        a = self.verlustartikel()
        radar.gesehen_setzen(a, self.okay)
        self.assertEqual(radar.auswertung(self.okay)["rot"], 0)
        ArtikelMandant.objects.filter(artikel=a, mandant=self.okay).update(ek_avg=D("15.9900"))  # unter 3 %: bleibt ausgeblendet
        self.assertEqual(radar.auswertung(self.okay)["rot"], 0)
        ArtikelMandant.objects.filter(artikel=a, mandant=self.okay).update(ek_avg=D("17.00"))  # +6 %, +1,02 €: erscheint wieder
        self.assertEqual(radar.auswertung(self.okay)["rot"], 1)

    def test_typ_p_ohne_bestand_nie(self):
        p = self.artikel("P1", typ="P", durchschnitts_ek=D("10"), ek_herkunft="import", vk1=D("5"))
        self.assertIsNone(radar.eintrag(p, self.okay))
        self.assertEqual(radar.auswertung(self.okay)["rot"], 0)

    def test_typ_l_nur_mit_kosten(self):
        ohne = self.artikel("L1", typ="L")
        self.assertIsNone(radar.eintrag(ohne, self.okay))
        mit = self.artikel("L2", typ="L", kosten=D("20"), vk1=D("15"))
        e = radar.eintrag(mit, self.okay)
        self.assertEqual((e.ampel, e.basis_quelle), ("rot", "Kosten"))
        self.assertEqual(e.menge, D("0"))  # ohne offene Belege nur Ampel, nicht im Dashboard
        self.assertEqual(radar.auswertung(self.okay)["rot"], 0)

    def test_ohne_bestand_nur_ampel(self):
        a = self.artikel(durchschnitts_ek=D("10"), ek_herkunft="import", vk1=D("9"))
        e = radar.eintrag(a, self.okay)
        self.assertEqual(e.ampel, "rot")
        self.assertEqual(radar.auswertung(self.okay)["rot"], 0)

    def test_vk_senkung_pruefen_und_uebernehmen(self):
        a = self.artikel(durchschnitts_ek=D("8"), ek_herkunft="import", vk1=D("15"))
        self.bestand(self.okay, a, 5)
        ArtikelMandant.objects.create(artikel=a, mandant=self.okay, ek_avg=D("4.7321"))
        e = radar.eintrag(a, self.okay)
        self.assertTrue(e.senkung_pruefen)  # Vorschlag 10,00 (ek_kalk 8,00 = Import > Ø-EK) gegen 15,00
        self.assertEqual(radar.auswertung(self.okay)["senkung"], 1)
        self.assertEqual(radar.vk_senkung_uebernehmen(a, self.okay), D("10.00"))
        a.refresh_from_db()
        self.assertEqual(a.vk1, D("10.00"))
        with self.assertRaises(ValueError):
            radar.vk_senkung_uebernehmen(a, self.okay)

    def test_vk_fix_keine_senkungsliste(self):
        a = self.artikel(durchschnitts_ek=D("8"), ek_herkunft="import", vk1=D("15"))
        self.bestand(self.okay, a, 5)
        ArtikelMandant.objects.create(artikel=a, mandant=self.okay, ek_avg=D("4.7321"), vk_fix1=True)
        self.assertFalse(radar.eintrag(a, self.okay).senkung_pruefen)


class RadarSeitenTest(TestCase):
    def setUp(self):
        self.okay = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        PreisVorgabe.objects.all().delete()
        PreisVorgabe.objects.create(aufschlag_vk1=D("25"))
        self.a = Artikel.objects.create(nummer="A1", bezeichnung="Verlustartikel", steuersatz=Steuersatz.objects.get(nummer=1), vk1=D("15"))
        Lagerbewegung.objects.create(mandant=self.okay, artikel=self.a, art=Bewegungsart.WARENEINGANG, menge=D("16"))
        ArtikelMandant.objects.create(artikel=self.a, mandant=self.okay, ek_avg=D("15.9821"), vk_fix1=True)
        self.benutzer = get_user_model().objects.create_user("u", password="x", is_staff=True)
        self.client.force_login(self.benutzer)
        s = self.client.session
        s["mandant_id"] = self.okay.pk
        s.save()

    def test_dashboard_zeigt_kachel(self):
        antwort = self.client.get(reverse("dashboard"))
        self.assertContains(antwort, "Preis-Radar")
        self.assertContains(antwort, "Verlust")
        self.assertContains(antwort, "15,71")

    def test_liste_und_gesehen(self):
        antwort = self.client.get(reverse("stammdaten:radar"), {"kategorie": "verlust"})
        self.assertContains(antwort, "Verlustartikel")
        self.client.post(reverse("stammdaten:radar") + "?kategorie=verlust", {"aktion": "gesehen", "artikel": self.a.pk})
        self.assertTrue(RadarGesehen.objects.filter(artikel=self.a, mandant=self.okay).exists())
        self.assertNotContains(self.client.get(reverse("stammdaten:radar"), {"kategorie": "verlust"}), "Verlustartikel")

    def test_unbekannte_und_nur_liste_kategorie(self):
        self.assertEqual(self.client.get(reverse("stammdaten:radar"), {"kategorie": "quatsch"}).status_code, 404)
        self.assertEqual(self.client.get(reverse("stammdaten:radar"), {"kategorie": "kein_ek"}).status_code, 404)
