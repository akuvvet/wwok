"""Positionsimport im Beleg und halbmonatliche Abrechnung am Beispiel der Apetito-Dateien (Oktober 2026)."""
from datetime import date
from decimal import Decimal
from io import BytesIO

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.forms import inlineformset_factory
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from openpyxl import Workbook

from apps.kern import kalender
from apps.kern.models import Mandant
from apps.stammdaten.leistung_import import ImportFehler
from apps.stammdaten.models import Artikel, Kunde, Lieferant, Preislistenartikel, Steuersatz

from .admin import AboPositionFormset
from .models import Abo, AboPosition, Beleg, Belegart, BelegGesperrt, Mengenregel, Position, faellige_abos, regel_vorschlag
from .position_import import menge_lesen, positionen_importieren

KOPF = ["pos", "nummer", "benennung", "k", "netto", "menge", "gesamtnetto", "Zusatz_pos", "Kennzeichen"]
HALB = "Abrechnungszeitraum 16.10.2026-31.10.2026"
MONAT = "Abrechnungszeitraum 01.10.2026-31.10.2026"
DATEI_30 = [
    [1, 40009, "IV Qualiprämie", "", 40, 11, 440, MONAT, "L"],
    [2, 40134, "Fahrzeugwerbung", "", 350, 11, 3850, MONAT, "L"],
    [3, 40003, "Tour D009 IV", "", 105.51, 11, 1160.61, HALB, "L"],
    [4, 40003, "Tour D009 IV", "", 124, 5, 620, HALB, "L"],
    [21, 40045, "Tour D270 IV", "", 145, 11, 1595, HALB, "L"],
    [21, 40149, "Tour D270 IV", "", 144, 5, 720, HALB, "L"],
    [22, 40148, "Shuttle TSP Köln", "", 68, 16, 1088, MONAT, "L"],
]


def excel(zeilen, kopf=KOPF):
    wb = Workbook()
    wb.active.append(kopf)
    for z in zeilen:
        wb.active.append(z)
    puffer = BytesIO()
    wb.save(puffer)
    return puffer.getvalue()


class KalenderTest(SimpleTestCase):
    def test_feiertage_nrw_2026(self):
        self.assertEqual(sorted(kalender.feiertage(2026)), [
            date(2026, 1, 1), date(2026, 4, 3), date(2026, 4, 6), date(2026, 5, 1), date(2026, 5, 14), date(2026, 5, 25),
            date(2026, 6, 4), date(2026, 10, 3), date(2026, 11, 1), date(2026, 12, 25), date(2026, 12, 26)])

    def test_ostern(self):
        self.assertEqual([kalender.ostersonntag(j) for j in (2024, 2025, 2026, 2027, 2038)],
                         [date(2024, 3, 31), date(2025, 4, 20), date(2026, 4, 5), date(2027, 3, 28), date(2038, 4, 25)])

    def test_tage_wie_in_den_dateien(self):
        self.assertEqual(kalender.tage_zaehlen(date(2026, 10, 1), date(2026, 10, 15)), (11, 4))
        self.assertEqual(kalender.tage_zaehlen(date(2026, 10, 16), date(2026, 10, 31)), (11, 5))

    def test_feiertag_am_wochenende_zaehlt_einmal_und_feiertag_unter_der_woche_ist_frei(self):
        self.assertEqual(kalender.tage_zaehlen(date(2026, 10, 3), date(2026, 10, 3)), (0, 1))
        self.assertEqual(kalender.tage_zaehlen(date(2026, 12, 25), date(2026, 12, 25)), (0, 1))
        self.assertEqual(kalender.tage_zaehlen(date(2026, 12, 24), date(2026, 12, 24)), (1, 0))

    def test_stichtage_auch_im_februar(self):
        self.assertEqual(kalender.stichtag(date(2027, 2, 16)), date(2027, 2, 28))
        self.assertEqual(kalender.stichtag(date(2028, 2, 20)), date(2028, 2, 29))
        self.assertEqual(kalender.stichtag(date(2026, 11, 15)), date(2026, 11, 15))
        self.assertEqual(kalender.naechster_stichtag(date(2027, 2, 15)), date(2027, 2, 28))
        self.assertEqual(kalender.naechster_stichtag(date(2027, 2, 28)), date(2027, 3, 15))
        self.assertEqual(kalender.naechster_stichtag(date(2026, 12, 31)), date(2027, 1, 15))
        self.assertEqual(kalender.halbmonat(date(2027, 2, 28)), (date(2027, 2, 16), date(2027, 2, 28)))
        self.assertEqual(kalender.tage_zaehlen(*kalender.halbmonat(date(2027, 2, 28))), (9, 4))

    def test_regel_vorschlag(self):
        self.assertEqual(regel_vorschlag(Decimal("11.000"), HALB), (Mengenregel.WOCHENTAGE, False))
        self.assertEqual(regel_vorschlag(Decimal("5"), HALB), (Mengenregel.FREI, False))
        self.assertEqual(regel_vorschlag(Decimal("15"), "Abrechnungszeitraum 01.10.2026 - 15.10.2026"), (Mengenregel.ALLE, False))
        self.assertEqual(regel_vorschlag(Decimal("11"), MONAT), (Mengenregel.FEST, True))
        self.assertEqual(regel_vorschlag(Decimal("16"), MONAT), (Mengenregel.ALLE, False))
        self.assertEqual(regel_vorschlag(Decimal("22"), MONAT), (Mengenregel.WOCHENTAGE, True))
        self.assertEqual(regel_vorschlag(Decimal("31"), MONAT), (Mengenregel.ALLE, True))
        self.assertEqual(regel_vorschlag(Decimal("3"), HALB), (Mengenregel.FEST, False))
        self.assertEqual(regel_vorschlag(Decimal("11"), ""), (Mengenregel.FEST, False))
        self.assertEqual(regel_vorschlag(Decimal("11"), "Abrechnungszeitraum 31.02.2026 - 15.03.2026"), (Mengenregel.FEST, False))
        self.assertEqual(regel_vorschlag(Decimal("3"), "Abrechnungszeitraum 05.10.2026 - 07.10.2026"), (Mengenregel.FEST, False))

    def test_menge_lesen(self):
        self.assertEqual(menge_lesen(11.0), Decimal("11"))
        self.assertEqual(menge_lesen("1,5"), Decimal("1.5"))
        self.assertEqual(menge_lesen(10.999999999999998), Decimal("11"))
        for falsch in ("1.000", "0", 0, -1, "x", 1.2345, "1,2345", 1e8):
            with self.assertRaises(ValueError, msg=falsch):
                menge_lesen(falsch)


class Grundlage(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.kunde = Kunde.objects.create(nummer="9001", name1="Testkunde")
        self.satz = Steuersatz.objects.get(prozent=19)
        for nummer in (40003, 40009, 40045, 40134, 40148, 40149):
            Artikel.objects.create(nummer=str(nummer), bezeichnung=f"Stamm {nummer}", typ="L", steuersatz=self.satz)
        self.u = get_user_model().objects.create_superuser("chef", "c@example.org", "geheim")
        self.client.force_login(self.u)
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    def beleg(self, art=Belegart.RECHNUNG):
        return Beleg.objects.create(mandant=self.m, art=art, kunde=self.kunde)


class PositionsimportTest(Grundlage):
    def test_datei_wird_uebernommen(self):
        b = self.beleg()
        anzahl, netto = positionen_importieren(b, "a.xlsx", excel(DATEI_30))
        self.assertEqual((anzahl, netto), (7, Decimal("9473.61")))
        b.refresh_from_db()
        self.assertEqual(b.summe_netto, Decimal("9473.61"))
        pos = list(b.positionen.all())
        self.assertEqual([p.pos for p in pos], [1, 2, 3, 4, 5, 6, 7])  # doppelte 21 aus der Datei zählt nicht
        self.assertEqual({p.typ for p in pos}, {"L"})
        self.assertEqual((pos[2].artikel.nummer, pos[2].einzelpreis, pos[3].einzelpreis), ("40003", Decimal("105.51"), Decimal("124.00")))
        self.assertEqual((pos[2].bezeichnung, pos[2].zusatztext), ("Tour D009 IV", HALB))
        self.assertEqual(pos[6].bezeichnung, "Shuttle TSP Köln")

    def test_csv_und_fehlende_spalten(self):
        Artikel.objects.filter(nummer="40003").update(vk1=Decimal("99.00"))
        b = self.beleg(Belegart.AUFTRAG)
        inhalt = "Nummer;Menge;Netto\n40003;2;1.234,50\n40003;1,5;\n".encode("cp1252")
        self.assertEqual(positionen_importieren(b, "a.csv", inhalt)[0], 2)
        a, c = b.positionen.all()
        self.assertEqual((a.bezeichnung, a.einzelpreis, c.einzelpreis, c.menge), ("Stamm 40003", Decimal("1234.50"), Decimal("99.00"), Decimal("1.5")))

    def test_fehler_alles_oder_nichts(self):
        Artikel.objects.create(nummer="50001", bezeichnung="Alt", typ="L", steuersatz=self.satz, nicht_aktiv=True)
        Artikel.objects.create(nummer="50002", bezeichnung="Ware", typ="A", steuersatz=self.satz)
        Artikel.objects.create(nummer="50003", bezeichnung="Gruppe", typ="G", steuersatz=self.satz)
        b = self.beleg()
        zeilen = [
            [1, 40003, "gut", "", 10, 2, 20, "", "L"],
            [2, 49999, "unbekannt", "", 10, 2, 20, "", "L"],
            [3, 50001, "inaktiv", "", 10, 2, 20, "", "L"],
            [4, 50002, "falsches Kennzeichen", "", 10, 2, 20, "", "L"],
            [5, 50003, "Gruppe", "", 10, 2, 20, "", ""],
            [6, 40003, "Summe falsch", "", 10, 2, 21, "", "L"],
            [7, 40003, "Menge 0", "", 10, 0, 0, "", "L"],
            [8, 40003, "negativ", "", -10, 2, -20, "", "L"],
            [9, "", "keine Nummer", "", 10, 2, 20, "", ""],
            [10, 40003, "ohne Preis", "", "", 2, "", "", ""],
            [11, 40003, "x" * 256, "", 10, 2, 20, "", ""],
            [12, 40003, "Fehlerzelle", "", "#REF!", 2, 20, "", ""],
            [13, 40003, "Datum", "", date(2026, 1, 1), 2, 20, "", ""],
        ]
        with self.assertRaises(ImportFehler) as f:
            positionen_importieren(b, "a.xlsx", excel(zeilen))
        self.assertEqual([t.split(":")[0] for t in f.exception.fehler], [f"Zeile {n}" for n in range(3, 15)])
        self.assertFalse(b.positionen.exists())

    def test_kopfzeile_und_leere_datei(self):
        b = self.beleg()
        with self.assertRaisesMessage(ImportFehler, "In der Kopfzeile fehlt: Menge"):
            positionen_importieren(b, "a.csv", b"nummer;netto\n40003;1\n")
        with self.assertRaisesMessage(ImportFehler, "keine Positionen"):
            positionen_importieren(b, "a.csv", b"nummer;menge\n")
        with self.assertRaisesMessage(ImportFehler, "leer"):
            positionen_importieren(b, "a.csv", b"")
        with self.assertRaisesMessage(ImportFehler, "lässt sich nicht lesen"):
            positionen_importieren(b, "a.xlsx", b"kein excel")
        with self.assertRaisesMessage(ImportFehler, "mehr als 1000"):
            positionen_importieren(b, "a.csv", b"nummer;menge\n" + b"40003;1\n" * 1001)

    def test_zeile_breiter_als_kopf(self):
        with self.assertRaises(ImportFehler) as f:
            positionen_importieren(self.beleg(), "a.csv", b"nummer;menge\n40003;1;x\n")
        self.assertIn("Mehr Spalten", f.exception.fehler[0])

    def test_belegart_und_sperre(self):
        with self.assertRaisesMessage(ImportFehler, "keine Positionen importieren"):
            positionen_importieren(self.beleg(Belegart.GUTSCHRIFT), "a.csv", b"nummer;menge\n40003;1\n")
        b = self.beleg()
        Position.objects.create(beleg=b, typ="L", bezeichnung="x", menge=1, einzelpreis=Decimal("1"))
        b.abschliessen()
        with self.assertRaises(BelegGesperrt):
            positionen_importieren(b, "a.csv", b"nummer;menge;netto\n40003;1;5\n")

    def test_inzwischen_abgeschlossene_rechnung_bleibt_unveraendert(self):
        b = self.beleg()
        Position.objects.create(beleg=b, typ="L", bezeichnung="x", menge=1, einzelpreis=Decimal("1"))
        veraltet = Beleg.objects.get(pk=b.pk)
        b.abschliessen()
        with self.assertRaises(BelegGesperrt):
            positionen_importieren(veraltet, "a.csv", b"nummer;menge;netto\n40003;1;5\n")
        self.assertEqual(b.positionen.count(), 1)

    def test_mehrdeutige_oder_unbekannte_preisspalten(self):
        Artikel.objects.filter(nummer="40003").update(vk1=Decimal("99.00"))
        b = self.beleg()
        with self.assertRaisesMessage(ImportFehler, "mehrdeutig"):
            positionen_importieren(b, "a.csv", b"nummer;Einzelpreis;menge;Netto\n40003;10,00;3;30,00\n")
        with self.assertRaisesMessage(ImportFehler, "mehrdeutig"):
            positionen_importieren(b, "a.csv", b"nummer;menge;netto;netto\n40003;3;30,00;10,00\n")
        with self.assertRaisesMessage(ImportFehler, "Spalte nicht erkannt: Netto EUR"):
            positionen_importieren(b, "a.csv", b"nummer;menge;Netto EUR\n40003;3;10,00\n")
        with self.assertRaisesMessage(ImportFehler, "Spalte nicht erkannt: Nachlass %"):
            positionen_importieren(b, "a.csv", b"nummer;menge;netto;Nachlass %\n40003;3;10,00;5\n")
        with self.assertRaisesMessage(ImportFehler, "Spalte nicht erkannt: Listenpreis"):
            positionen_importieren(b, "a.csv", b"nummer;Listenpreis;menge\n40003;10,00;1\n")
        with self.assertRaisesMessage(ImportFehler, "Spalte nicht erkannt: Nachlass Rabatt"):
            positionen_importieren(b, "a.csv", b"nummer;netto;menge;Nachlass Rabatt\n40003;10,00;1;5\n")
        self.assertEqual(positionen_importieren(b, "a.csv", b"nummer;VK-Preis;menge;Rabatt in %\n40003;10,00;3;50\n")[1], Decimal("15.00"))
        self.assertEqual(positionen_importieren(b, "a.csv", b"nummer;netto;menge;gesamtnetto\n40003;10,00;3;30,00\n")[1], Decimal("30.00"))

    def test_leere_zeilen_zaehlen_nicht_fuer_die_grenze(self):
        inhalt = b"nummer;menge;netto\n40003;1;5\n" + b";;\n" * 1500
        self.assertEqual(positionen_importieren(self.beleg(), "a.csv", inhalt)[0], 1)

    def test_ansicht_gilt_erst_nach_speichern(self):
        b = self.beleg()
        Position.objects.create(beleg=b, typ="L", bezeichnung="vorher", menge=1, einzelpreis=Decimal("1"))
        b.berechne()
        url = reverse("verkauf:positionen_import", args=[b.pk])
        r = self.client.post(url, {"datei": SimpleUploadedFile("a.xlsx", excel(DATEI_30))}, follow=True)
        self.assertContains(r, "7 Position(en) importiert")
        self.assertContains(r, "Nicht gespeicherte Änderungen")
        self.assertEqual(b.positionen.count(), 8)
        self.client.post(reverse("verkauf:beleg_verwerfen", args=[b.pk]))
        self.assertEqual(list(b.positionen.values_list("bezeichnung", flat=True)), ["vorher"])

    def test_ansicht_fehler_laesst_beleg_ohne_offenen_stand(self):
        b = self.beleg()
        url = reverse("verkauf:positionen_import", args=[b.pk])
        r = self.client.post(url, {"datei": SimpleUploadedFile("a.csv", b"nummer;menge\n49999;1\n")}, follow=True)
        self.assertContains(r, "Nummer 49999 ist nicht angelegt")
        self.assertFalse(b.hat_offenen_stand)
        r = self.client.post(url, {}, follow=True)
        self.assertContains(r, "bis 5 MB")

    def test_fremder_mandant(self):
        anderer = Mandant.objects.create(kurzname="ZONE", firmenname="ZONE Handel")
        b = Beleg.objects.create(mandant=anderer, art=Belegart.RECHNUNG, kunde=self.kunde)
        r = self.client.post(reverse("verkauf:positionen_import", args=[b.pk]), {"datei": SimpleUploadedFile("a.csv", b"nummer;menge\n40003;1\n")})
        self.assertEqual(r.status_code, 404)


class HalbmonatTest(Grundlage):
    def abo(self, termin=date(2026, 10, 15), **felder):
        abo = Abo.objects.create(mandant=self.m, kunde=self.kunde, bezeichnung="Apetito", monate=Abo.HALBMONATLICH, naechster_termin=termin, **felder)
        art = {a.nummer: a for a in Artikel.objects.all()}
        for nummer, text, regel, menge, preis, ende in (
                ("40009", "IV Qualiprämie", Mengenregel.FEST, 11, "40", True),
                ("40134", "Fahrzeugwerbung", Mengenregel.FEST, 11, "350", True),
                ("40003", "Tour D009 IV", Mengenregel.WOCHENTAGE, 1, "105.51", False),
                ("40003", "Tour D009 IV", Mengenregel.FREI, 1, "124", False),
                ("40148", "Shuttle TSP Köln", Mengenregel.ALLE, 1, "68", False)):
            AboPosition.objects.create(abo=abo, artikel=art[nummer], bezeichnung=text, regel=regel, menge=menge, einzelpreis=Decimal(preis), nur_monatsende=ende)
        return abo

    def test_rechnung_zum_15(self):
        abo = self.abo()
        b = abo.rechnung_erzeugen(date(2026, 10, 15))
        zeilen = [(p.artikel.nummer, p.menge, p.einzelpreis, p.typ) for p in b.positionen.all()]
        self.assertEqual(zeilen, [("40003", 11, Decimal("105.51"), "L"), ("40003", 4, Decimal("124.00"), "L"), ("40148", 15, Decimal("68.00"), "L")])
        self.assertEqual({p.zusatztext for p in b.positionen.all()}, {"Abrechnungszeitraum 01.10.2026 - 15.10.2026"})
        self.assertEqual(b.summe_netto, Decimal("2676.61"))
        self.assertEqual(b.belegdatum, date(2026, 10, 15))
        self.assertEqual(abo.naechster_termin, date(2026, 10, 31))

    def test_rechnung_zum_monatsende(self):
        abo = self.abo(date(2026, 10, 31))
        b = abo.rechnung_erzeugen(date(2026, 10, 31))
        zeilen = [(p.bezeichnung, p.menge, p.zusatztext) for p in b.positionen.all()]
        zeitraum = "Abrechnungszeitraum 16.10.2026 - 31.10.2026"
        self.assertEqual(zeilen, [("IV Qualiprämie", 11, "Abrechnungszeitraum 01.10.2026 - 31.10.2026"),
                                  ("Fahrzeugwerbung", 11, "Abrechnungszeitraum 01.10.2026 - 31.10.2026"),
                                  ("Tour D009 IV", 11, zeitraum), ("Tour D009 IV", 5, zeitraum), ("Shuttle TSP Köln", 16, zeitraum)])
        self.assertEqual(abo.naechster_termin, date(2026, 11, 15))

    def test_rechnung_laesst_sich_abschliessen(self):
        b = self.abo().rechnung_erzeugen(date(2026, 10, 15))
        b.abschliessen()
        self.assertTrue(b.nummer)

    def test_termine_ueber_den_jahreswechsel_und_februar(self):
        abo = self.abo(date(2027, 1, 31))
        termine = []
        for _ in range(4):
            abo.rechnung_erzeugen(abo.naechster_termin)
            termine.append(abo.naechster_termin)
        self.assertEqual(termine, [date(2027, 2, 15), date(2027, 2, 28), date(2027, 3, 15), date(2027, 3, 31)])

    def test_termin_rueckt_auf_den_stichtag(self):
        self.assertEqual(self.abo(date(2026, 11, 3)).naechster_termin, date(2026, 11, 15))
        self.assertEqual(self.abo(date(2026, 11, 20)).naechster_termin, date(2026, 11, 30))

    def test_belegdatum_nie_vor_dem_stichtag_und_nachholen(self):
        abo = self.abo()
        self.assertEqual(abo.rechnung_erzeugen(date(2026, 10, 3)).belegdatum, date(2026, 10, 15))
        b = abo.rechnung_erzeugen(date(2026, 11, 4))  # versäumter Termin: Zeitraum des Termins, Datum des Laufs
        self.assertEqual(b.belegdatum, date(2026, 11, 4))
        self.assertEqual(b.positionen.first().zusatztext, "Abrechnungszeitraum 01.10.2026 - 31.10.2026")

    def test_taeglicher_lauf(self):
        abo = self.abo(date(2026, 1, 15))
        self.assertEqual(list(faellige_abos(date(2026, 1, 14))), [])
        self.assertEqual(list(faellige_abos(date(2026, 1, 15))), [abo])
        call_command("wiederkehrende_rechnungen", verbosity=0)
        abo.refresh_from_db()
        self.assertEqual(abo.naechster_termin, date(2026, 1, 31))

    def test_netto_der_liste_gilt_fuer_die_naechste_rechnung(self):
        self.assertEqual(self.abo().netto, Decimal("2676.61"))
        self.assertEqual(self.abo(date(2026, 10, 31)).netto, Decimal("7158.61"))

    def test_ende_beendet_die_vorlage(self):
        abo = self.abo(date(2026, 10, 15), ende=date(2026, 10, 15))
        abo.rechnung_erzeugen(date(2026, 10, 15))
        self.assertFalse(abo.aktiv)

    def test_ende_muss_ein_stichtag_sein(self):
        abo = self.abo(ende=date(2026, 11, 20))
        with self.assertRaises(ValidationError):
            abo.clean()
        abo.ende = date(2026, 11, 30)
        abo.clean()

    def test_nur_monatsende_haengt_nicht_am_15(self):
        abo = Abo.objects.create(mandant=self.m, kunde=self.kunde, bezeichnung="Werbung", monate=Abo.HALBMONATLICH, naechster_termin=date(2026, 11, 15))
        AboPosition.objects.create(abo=abo, bezeichnung="Werbung", menge=11, einzelpreis=Decimal("350"), nur_monatsende=True)
        self.assertIsNone(abo.rechnung_erzeugen(date(2026, 11, 15)))
        self.assertEqual(abo.naechster_termin, date(2026, 11, 30))
        self.assertEqual(abo.rechnung_erzeugen(date(2026, 11, 30)).summe_netto, Decimal("3850.00"))
        self.assertEqual(abo.naechster_termin, date(2026, 12, 15))
        r = self.client.post(reverse("verkauf:abo_aktion", args=[abo.pk]), {"aktion": "jetzt"}, follow=True)
        self.assertContains(r, "nächste Rechnung am 31.12.2026")
        Abo.objects.filter(pk=abo.pk).update(naechster_termin=date(2026, 1, 15))
        call_command("wiederkehrende_rechnungen", verbosity=0)
        abo.refresh_from_db()
        self.assertEqual(abo.naechster_termin, date(2026, 1, 31))

    def test_abo_ohne_positionen_bleibt_ein_fehler(self):
        abo = Abo.objects.create(mandant=self.m, kunde=self.kunde, bezeichnung="Leer", monate=Abo.HALBMONATLICH, naechster_termin=date(2026, 11, 15))
        with self.assertRaisesMessage(ValueError, "keine Positionen"):
            abo.rechnung_erzeugen(date(2026, 11, 15))

    def test_preisliste_und_lagergruppe_werden_keine_vorlage(self):
        b = self.beleg()
        Position.objects.create(beleg=b, typ="L", bezeichnung="x", menge=1, einzelpreis=Decimal("1"), preisliste_artikel=self.preisliste())
        self.client.post(reverse("verkauf:abo_aus_rechnung", args=[b.pk]), {"monate": "1", "start": "2026-11-20"})
        self.assertFalse(Abo.objects.exists())
        b = self.beleg()
        ware = Artikel.objects.create(nummer="50002", bezeichnung="Ware", typ="A", steuersatz=self.satz)
        kopf = Position.objects.create(beleg=b, typ="G", bezeichnung="Paket", menge=1, einzelpreis=Decimal("1"))
        Position.objects.create(beleg=b, gruppe=kopf, typ="A", artikel=ware, bezeichnung="Ware", menge=1, einzelpreis=Decimal("1"))
        self.client.post(reverse("verkauf:abo_aus_rechnung", args=[b.pk]), {"monate": "1", "start": "2026-11-20"})
        self.assertFalse(Abo.objects.exists())

    def preisliste(self):
        lieferant = Lieferant.objects.create(nummer="L1", name1="Lieferant", kurzname="LF")
        return Preislistenartikel.objects.create(lieferant=lieferant, bestellnummer="X1", bezeichnung="Laptop", ek=Decimal("10"))

    def test_vorlage_aus_importierter_rechnung(self):
        b = self.beleg()
        positionen_importieren(b, "a.xlsx", excel(DATEI_30))
        url = reverse("verkauf:abo_aus_rechnung", args=[b.pk])
        b.arbeitsstand_sichern(self.u)
        r = self.client.post(url, {"monate": "15", "start": "2026-11-01", "bezeichnung": "Apetito"}, follow=True)
        self.assertContains(r, "nicht gespeicherte Änderungen")
        self.assertFalse(Abo.objects.exists())
        b.arbeitsstand_beenden()
        r = self.client.post(url, {"monate": "15", "start": "2026-11-01", "bezeichnung": "Apetito"}, follow=True)
        self.assertContains(r, "5 Position(en) rechnen nach dem Kalender")
        abo = Abo.objects.get()
        self.assertEqual((abo.bezeichnung, abo.naechster_termin), ("Apetito", date(2026, 11, 15)))
        self.assertEqual([(p.regel, p.nur_monatsende) for p in abo.positionen.all()], [
            (Mengenregel.FEST, True), (Mengenregel.FEST, True), (Mengenregel.WOCHENTAGE, False), (Mengenregel.FREI, False),
            (Mengenregel.WOCHENTAGE, False), (Mengenregel.FREI, False), (Mengenregel.ALLE, False)])
        nov = abo.rechnung_erzeugen(date(2026, 11, 15))
        self.assertEqual([p.menge for p in nov.positionen.all()], [10, 5, 10, 5, 15])

    def test_monatlich_bleibt_unveraendert(self):
        b = self.beleg()
        positionen_importieren(b, "a.xlsx", excel(DATEI_30))
        self.client.post(reverse("verkauf:abo_aus_rechnung", args=[b.pk]), {"monate": "1", "start": "2026-11-20"})
        abo = Abo.objects.get()
        self.assertEqual(abo.naechster_termin, date(2026, 11, 20))
        self.assertEqual({p.regel for p in abo.positionen.all()}, {Mengenregel.FEST})
        self.assertEqual([p.menge for p in abo.rechnung_erzeugen(date(2026, 11, 20)).positionen.all()], [11, 11, 11, 5, 11, 5, 16])
        self.assertFalse(abo.letzte_rechnung.positionen.exclude(zusatztext="").exists())

    def test_leere_rechnung_ist_keine_vorlage(self):
        b = self.beleg()
        self.client.post(reverse("verkauf:abo_aus_rechnung", args=[b.pk]), {"monate": "15", "start": "2026-11-01"})
        self.assertFalse(Abo.objects.exists())

    def test_verwaltung_prueft_regel_und_rhythmus(self):
        Formset = inlineformset_factory(Abo, AboPosition, formset=AboPositionFormset, fields=["bezeichnung", "regel", "menge", "nur_monatsende", "einzelpreis", "rabatt_prozent", "steuer_prozent"], extra=1)
        abo = Abo(mandant=self.m, kunde=self.kunde, bezeichnung="x", monate=1, naechster_termin=date(2026, 11, 1))
        daten = {"positionen-TOTAL_FORMS": "1", "positionen-INITIAL_FORMS": "0", "positionen-0-bezeichnung": "x", "positionen-0-regel": "W", "positionen-0-menge": "1",
                 "positionen-0-einzelpreis": "1", "positionen-0-rabatt_prozent": "0", "positionen-0-steuer_prozent": "19"}
        self.assertFalse(Formset(daten, instance=abo).is_valid())
        abo.monate = Abo.HALBMONATLICH
        self.assertTrue(Formset(daten, instance=abo).is_valid())
        abo.monate = 1
        daten["positionen-0-regel"] = "F"
        self.assertTrue(Formset(daten, instance=abo).is_valid())
        daten["positionen-0-nur_monatsende"] = "on"
        self.assertFalse(Formset(daten, instance=abo).is_valid())
        abo.monate = Abo.HALBMONATLICH
        abo.ende = date(2026, 11, 15)
        self.assertFalse(Formset(daten, instance=abo).is_valid())
        abo.ende = date(2026, 11, 30)
        self.assertTrue(Formset(daten, instance=abo).is_valid())
