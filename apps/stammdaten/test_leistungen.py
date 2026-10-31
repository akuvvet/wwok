"""Leistungsartikel: eigene Kategorien, Kosten als EK, Pflege und Import über die Verwaltung."""
import io
from datetime import date
from decimal import Decimal as D
from importlib import import_module
from unittest.mock import MagicMock, patch

import xlrd
from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db import IntegrityError
from django.test import TestCase
from django.urls import reverse
from openpyxl import Workbook

from apps.kern.models import AenderungsProtokoll, Mandant
from apps.stammdaten.leistung_import import ImportFehler, geld, importieren, zelle_pruefen
from apps.stammdaten.management.commands.import_artikel import SPALTEN
from apps.stammdaten.models import Artikel, GruppenKomponente, Kategorie, Kunde, Leistungsartikel, Leistungskategorie, PreisVorgabe, Steuersatz
from apps.stammdaten.preise import gruppen_kalkulation, stufenpreis, verkaufspreis
from apps.verkauf.models import Beleg, Position


class LeistungTest(TestCase):
    def setUp(self):
        self.st = Steuersatz.objects.get(prozent=D("19"))
        self.st7 = Steuersatz.objects.get(prozent=D("7"))
        self.kat = Leistungskategorie.objects.create(name="Werkstatt")
        self.l = Artikel.objects.create(nummer="40098", typ="L", bezeichnung="Montage", steuersatz=self.st, vk1=D("35"),
                                        kosten=D("20"), leistungskategorie=self.kat)

    def test_kategorien_sind_getrennt(self):
        self.l.kategorie = Kategorie.objects.create(name="Hardware")
        self.l.save()
        self.l.refresh_from_db()
        self.assertIsNone(self.l.kategorie)
        a = Artikel.objects.create(nummer="1", bezeichnung="Ware", steuersatz=self.st, kosten=D("5"), leistungskategorie=self.kat)
        self.assertEqual((a.kosten, a.leistungskategorie), (None, None))

    def test_migration_uebernimmt_alte_kategorie(self):
        alt = Kategorie.objects.create(name="Service", nummer="12")
        Artikel.objects.filter(pk=self.l.pk).update(kategorie=alt, leistungskategorie=None)
        ware = Artikel.objects.create(nummer="1", bezeichnung="Ware", steuersatz=self.st, kategorie=alt)
        import_module("apps.stammdaten.migrations.0018_leistungskategorien").kategorien_uebernehmen(apps, None)
        self.l.refresh_from_db()
        ware.refresh_from_db()
        self.assertEqual((self.l.kategorie, self.l.leistungskategorie.name), (None, "Service"))
        self.assertEqual(self.l.leistungskategorie.nummer, "12")
        self.assertEqual(ware.kategorie, alt)

    def test_kosten_sind_der_ek(self):
        self.assertEqual(self.l.ek_errechnet, D("20"))
        self.assertIsNone(Artikel(typ="L", nummer="2").ek_errechnet)

    def test_kosten_aendern_keinen_preis(self):
        PreisVorgabe.objects.create(aufschlag_vk1=D("50"), aufschlag_vk2=D("10"))
        self.assertIsNone(stufenpreis(self.l, 2))
        kunde = Kunde.objects.create(nummer="1", name1="Händler", preisstufe=2)
        self.assertEqual(verkaufspreis(self.l, kunde), (D("35"), "stufe"))  # Händlerstufe fehlt: VK1, nicht Kosten + Aufschlag
        self.l.vk1 = None
        self.assertIsNone(stufenpreis(self.l, 1))

    def test_kosten_aendern_keinen_gruppenpreis(self):
        PreisVorgabe.objects.create(aufschlag_vk1=D("25"))
        ware = Artikel.objects.create(nummer="1", bezeichnung="Ware", steuersatz=self.st, durchschnitts_ek=D("100"))
        g = Artikel.objects.create(nummer="G1", typ="G", bezeichnung="Paket", steuersatz=self.st)
        GruppenKomponente.objects.create(gruppe=g, komponente=ware, menge=D("1"))
        GruppenKomponente.objects.create(gruppe=g, komponente=self.l, menge=D("1"))
        self.assertEqual(g.ek_errechnet, D("120"))
        self.assertEqual(stufenpreis(g, 1), D("125.00"))  # Aufschlag nur auf die Ware
        self.assertEqual(gruppen_kalkulation(g)["preis_marge"], D("4.00"))

    def test_migration_uebernimmt_vorhandenen_ek(self):
        Artikel.objects.filter(pk=self.l.pk).update(kosten=None, durchschnitts_ek=D("20.1250"))
        ware = Artikel.objects.create(nummer="1", bezeichnung="Ware", steuersatz=self.st, durchschnitts_ek=D("5"))
        import_module("apps.stammdaten.migrations.0018_leistungskategorien").kategorien_uebernehmen(apps, None)
        self.l.refresh_from_db()
        ware.refresh_from_db()
        self.assertEqual((self.l.kosten, ware.kosten), (D("20.13"), None))

    def test_marge_im_beleg(self):
        m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        k = Kunde.objects.create(nummer="1", name1="K")
        b = Beleg.objects.create(mandant=m, art="AN", kunde=k)
        p = Position.objects.create(beleg=b, artikel=self.l, bezeichnung="Montage", menge=D("2"), einzelpreis=D("35"), steuer_prozent=D("19"))
        self.assertEqual(p.ek_stueck, D("20"))
        self.assertEqual(b.ek_und_marge(), (D("40.00"), D("42.86"), False))
        Artikel.objects.filter(pk=self.l.pk).update(kosten=None)
        b = Beleg.objects.get(pk=b.pk)
        self.assertTrue(b.ek_und_marge()[2])  # ohne Kosten bleibt der EK unvollständig

    def test_gruppe_zaehlt_kosten(self):
        ohne = Artikel.objects.create(nummer="40099", typ="L", bezeichnung="Beratung", steuersatz=self.st, vk1=D("10"))
        g = Artikel.objects.create(nummer="G1", typ="G", bezeichnung="Paket", steuersatz=self.st)
        GruppenKomponente.objects.create(gruppe=g, komponente=self.l, menge=D("2"))
        GruppenKomponente.objects.create(gruppe=g, komponente=ohne, menge=D("1"))
        self.assertEqual(g.ek_errechnet, D("40"))
        k = gruppen_kalkulation(g)
        self.assertEqual((k["ek"], k["vollstaendig"]), (D("40"), True))


class ImportTest(TestCase):
    def setUp(self):
        self.st = Steuersatz.objects.get(prozent=D("19"))
        self.st7 = Steuersatz.objects.get(prozent=D("7"))

    def csv(self, *zeilen, kopf="Nummer;Bezeichnung;Kategorie;Kosten;VK1;Einheit;Steuersatz"):
        return "\n".join([kopf, *zeilen]).encode("utf-8")

    def test_csv_legt_an_und_ergaenzt_kategorie(self):
        self.assertEqual(importieren("l.csv", self.csv("L1;Montage;Werkstatt;1.020,50;35,00 €;Std.;7 %", "L2;Beratung;werkstatt;;40;;")), (2, 0, 0, 1))
        a = Artikel.objects.get(nummer="L1")
        self.assertEqual((a.typ, a.kosten, a.vk1, a.mengeneinheit, a.steuersatz, a.leistungskategorie.name),
                         ("L", D("1020.50"), D("35.00"), "Std.", self.st7, "Werkstatt"))
        b = Artikel.objects.get(nummer="L2")
        self.assertEqual((b.kosten, b.steuersatz, b.leistungskategorie), (None, self.st, a.leistungskategorie))

    def test_zweiter_lauf_aendert_nur_gefuellte_zellen(self):
        importieren("l.csv", self.csv("L1;Montage;Werkstatt;20;35;Std.;1"))
        vorher = AenderungsProtokoll.objects.count()
        self.assertEqual(importieren("l.csv", self.csv("L1;Montage;Werkstatt;20;35;Std.;1")), (0, 0, 1, 0))
        self.assertEqual(AenderungsProtokoll.objects.count(), vorher)
        self.assertEqual(importieren("l.csv", self.csv("L1;;;25;;;")), (0, 1, 0, 0))
        a = Artikel.objects.get(nummer="L1")
        self.assertEqual((a.bezeichnung, a.kosten, a.vk1, a.leistungskategorie.name), ("Montage", D("25.00"), D("35.00"), "Werkstatt"))

    def test_excel(self):
        wb = Workbook()
        wb.active.append(["Artikelnummer", "Bezeichnung", "EK-Preis", "VK-Preis 1", "Kategorie", "nicht aktiv"])
        wb.active.append([40098, "Montage", 20.5, 35, 12, 1])
        puffer = io.BytesIO()
        wb.save(puffer)
        self.assertEqual(importieren("l.xlsx", puffer.getvalue()), (1, 0, 0, 1))
        a = Artikel.objects.get(nummer="40098")
        self.assertEqual((a.kosten, a.vk1, a.nicht_aktiv, a.leistungskategorie.nummer), (D("20.50"), D("35.00"), True, "12"))
        self.assertEqual(importieren("l.xlsx", puffer.getvalue())[3], 0)  # Kategorie wird über die Nummer wiedergefunden

    def test_alles_oder_nichts(self):
        Artikel.objects.create(nummer="A1", bezeichnung="Ware", steuersatz=self.st)
        with self.assertRaises(ImportFehler) as f:
            importieren("l.csv", self.csv("L1;Montage;Neu;20;35;;", "A1;Ware;;1;1;;", "L1;Doppelt;;;;;", ";Ohne;;;;;", "L3;;;;;;",
                                          "L4;Minus;;-1;;;", "L5;Text;;abc;;;", "L6;Steuer;;;;;99", "L7;Lang;;;99999999999;;"))
        text = "\n".join(f.exception.fehler)
        for teil in ("Zeile 3: A1 ist schon als A – Lagerartikel", "Zeile 4: Nummer L1 steht doppelt", "Zeile 5: Die Nummer fehlt",
                     "Zeile 6: Die Bezeichnung fehlt", "Zeile 7: Betrag ungültig", "Zeile 8: Betrag ungültig", "Zeile 9: Steuersatz unbekannt",
                     "Zeile 10:"):
            self.assertIn(teil, text)
        self.assertFalse(Artikel.objects.filter(nummer="L1").exists())
        self.assertFalse(Leistungskategorie.objects.exists())

    def test_betraege(self):
        for wert, erwartet in (("1.234", "1234.00"), ("1.234,56 €", "1234.56"), ("12,5", "12.50"), ("12.50", "12.50"), ("0", "0.00"),
                               (20.555, "20.56"), (35, "35.00")):
            self.assertEqual(geld(wert), D(erwartet), wert)
        for wert in ("1,234.56", "12.345,678", "1E5", "abc", "-1", "1.2.3", "0.500", "000.500"):
            with self.assertRaisesMessage(ValueError, "Betrag ungültig"):
                geld(wert)

    def test_kategorie_neu(self):
        self.assertEqual(importieren("l.csv", self.csv("L1;Montage;Neu;;;;", "L2;Dienst;neu;;;;")), (2, 0, 0, 1))
        self.assertEqual(Artikel.objects.get(nummer="L2").leistungskategorie.name, "Neu")

    def test_nummer_der_kategorie_geht_vor(self):
        werkstatt = Leistungskategorie.objects.create(name="Werkstatt", nummer="100")
        Leistungskategorie.objects.create(name="100", nummer="5")
        importieren("l.csv", self.csv("L1;Montage;100;;;;"))
        self.assertEqual(Artikel.objects.get(nummer="L1").leistungskategorie, werkstatt)

    def test_zu_viele_spalten(self):
        with self.assertRaisesMessage(ImportFehler, "Nichts importiert") as f:
            importieren("l.csv", b"Nummer,Bezeichnung,Kosten,VK1\nL1,Montage,20,50,35,00\n")
        self.assertIn("Mehr Spalten", f.exception.fehler[0])

    def test_offenes_anfuehrungszeichen(self):
        with self.assertRaises(ImportFehler) as f:
            importieren("l.csv", b'Nummer;Bezeichnung;Kosten\nL1;"Montage;20\nL2;Beratung;30\n')
        self.assertIn("Anführungszeichen", f.exception.fehler[0])

    def test_nicht_aktiv(self):
        kopf = "Nummer;Bezeichnung;nicht aktiv"
        importieren("l.csv", self.csv("L1;Montage;ja", "L2;Dienst;0", kopf=kopf))
        self.assertEqual(list(Artikel.objects.order_by("nummer").values_list("nicht_aktiv", flat=True)), [True, False])
        with self.assertRaises(ImportFehler) as f:
            importieren("l.csv", self.csv("L1;Montage;vielleicht", kopf=kopf))
        self.assertIn("vielleicht", f.exception.fehler[0])
        self.assertTrue(Artikel.objects.get(nummer="L1").nicht_aktiv)

    def test_excel_zellen_ohne_wert(self):
        wb = Workbook()
        wb.active.append(["Nummer", "Bezeichnung", "Kosten", "nicht aktiv"])
        wb.active.append(["L1", "#REF!", 1, None])
        wb.active.append([date(2024, 3, 1), "Montage", 1, None])
        wb.active.append(["L3", True, 1, True])
        puffer = io.BytesIO()
        wb.save(puffer)
        with self.assertRaises(ImportFehler) as f:
            importieren("l.xlsx", puffer.getvalue())
        self.assertEqual([z.split(":")[0] for z in f.exception.fehler], ["Zeile 2", "Zeile 3", "Zeile 4"])
        self.assertFalse(Artikel.objects.exists())
        with self.assertRaisesMessage(ValueError, "Inhalt ungültig"):
            zelle_pruefen("kosten", float("inf"))

    def test_xls_fehlerzelle_ist_keine_zahl(self):
        kopf = ["Nummer", "Bezeichnung", "Kosten", "nicht aktiv"]
        typen = [[xlrd.XL_CELL_TEXT] * 4, [xlrd.XL_CELL_TEXT, xlrd.XL_CELL_TEXT, xlrd.XL_CELL_ERROR, xlrd.XL_CELL_BOOLEAN],
                 [xlrd.XL_CELL_TEXT, xlrd.XL_CELL_TEXT, xlrd.XL_CELL_DATE, xlrd.XL_CELL_EMPTY]]
        werte = [kopf, ["L1", "Montage", 42, 1], ["L2", "Dienst", 45352.0, ""]]
        blatt = MagicMock(nrows=3)
        blatt.row_values.side_effect = lambda i: werte[i]
        blatt.row_types.side_effect = lambda i: typen[i]
        with patch("xlrd.open_workbook") as buch:
            buch.return_value.sheet_by_index.return_value = blatt
            buch.return_value.datemode = 0
            with self.assertRaises(ImportFehler) as f:
                importieren("l.xls", b"x")
        self.assertIn("#N/A", f.exception.fehler[0])
        self.assertIn("Zeile 3", f.exception.fehler[1])

    def test_gross_klein_schreibung(self):
        Artikel.objects.create(nummer="ABC", bezeichnung="Festplatte", steuersatz=self.st)
        with self.assertRaisesMessage(ImportFehler, "Nichts importiert"):
            importieren("l.csv", self.csv("abc;Montage;;;;;"))
        self.assertEqual(Artikel.objects.count(), 1)

    def test_kollision_der_datenbank_wird_zur_meldung(self):
        with patch("apps.stammdaten.models.Artikel.save", side_effect=IntegrityError):
            with self.assertRaisesMessage(ImportFehler, "gelten für die Datenbank als gleich"):
                importieren("l.csv", self.csv("L1;Montage;;;;;"))

    def test_artikelimport_laesst_leistung_stehen(self):
        kopf = list(SPALTEN.values())
        werte = {"Nummer": "L1", "Bezeichnung": "Neu", "Steuersatz": self.st.nummer, "Kategorie": "K1"}
        zeilen = [kopf, [werte.get(name, "") for name in kopf]]
        blatt = MagicMock(nrows=2)
        blatt.row_values.side_effect = lambda i: zeilen[i]
        Kategorie.objects.create(nummer="K1", name="Hardware")
        l = Artikel.objects.create(nummer="L1", typ="L", bezeichnung="Montage", steuersatz=self.st, kosten=D("20"))
        with patch("xlrd.open_workbook") as buch:
            buch.return_value.sheet_by_index.return_value = blatt
            call_command("import_artikel", "artikel.xls", stdout=io.StringIO())
        l.refresh_from_db()
        self.assertEqual((l.typ, l.kosten, l.bezeichnung, l.kategorie), ("L", D("20"), "Neu", None))

    def test_falsche_dateien(self):
        with self.assertRaisesMessage(ImportFehler, "In der Kopfzeile fehlt: Nummer"):
            importieren("l.csv", b"Bezeichnung;Preis\nMontage;1\n")
        with self.assertRaisesMessage(ImportFehler, "lässt sich nicht lesen"):
            importieren("l.xlsx", b"keine Tabelle")
        with self.assertRaisesMessage(ImportFehler, "CSV- oder Excel-Datei"):
            importieren("l.xml", b"<?xml version='1.0'?><a/>")
        with self.assertRaisesMessage(ImportFehler, "leer"):
            importieren("l.csv", b"")


class VerwaltungTest(TestCase):
    def setUp(self):
        self.st = Steuersatz.objects.get(prozent=D("19"))
        self.admin = get_user_model().objects.create_superuser("chef", password="x")
        self.client.force_login(self.admin)
        self.liste = reverse("admin:stammdaten_leistungsartikel_changelist")

    def test_anlegen_setzt_typ_l(self):
        kat = Leistungskategorie.objects.create(name="Werkstatt")
        r = self.client.post(reverse("admin:stammdaten_leistungsartikel_add"), {
            "nummer": "L1", "bezeichnung": "Montage", "leistungskategorie": kat.pk, "mengeneinheit": "Std.", "steuersatz": self.st.pk,
            "kosten": "20.00", "vk1": "35.00"})
        self.assertRedirects(r, self.liste)
        a = Artikel.objects.get(nummer="L1")
        self.assertEqual((a.typ, a.kosten, a.leistungskategorie), ("L", D("20"), kat))
        self.assertEqual(AenderungsProtokoll.objects.filter(tabelle="stammdaten.Artikel", datensatz_id=str(a.pk)).count(), 1)

    def test_vorbelegung(self):
        Artikel.objects.create(nummer="500", bezeichnung="Ware", steuersatz=self.st)
        r = self.client.get(reverse("admin:stammdaten_leistungsartikel_add"))
        self.assertContains(r, 'value="501"')

    def test_liste_zeigt_nur_leistungen(self):
        Artikel.objects.create(nummer="1", bezeichnung="Festplatte", steuersatz=self.st)
        Artikel.objects.create(nummer="2", typ="L", bezeichnung="Dienst", steuersatz=self.st)
        r = self.client.get(self.liste)
        self.assertContains(r, "Dienst")
        self.assertNotContains(r, "Festplatte")
        self.assertEqual(Leistungsartikel.objects.count(), 2)  # die Sicht filtert erst die Verwaltung

    def test_import_ueber_die_seite(self):
        url = reverse("admin:stammdaten_leistungsartikel_import")
        self.assertContains(self.client.get(self.liste), url)
        datei = SimpleUploadedFile("l.csv", b"Nummer;Bezeichnung;Kosten\nL1;Montage;20\n")
        r = self.client.post(url, {"datei": datei}, follow=True)
        self.assertContains(r, "1 neu, 0 geändert")
        datei = SimpleUploadedFile("l.csv", b"Nummer;Bezeichnung;Kosten\nL2;Montage;abc\n")
        r = self.client.post(url, {"datei": datei})
        self.assertContains(r, "Zeile 2")
        self.assertFalse(Artikel.objects.filter(nummer="L2").exists())

    def test_import_nur_mit_recht(self):
        self.client.force_login(get_user_model().objects.create_user("mit", password="x", is_staff=True))
        url = reverse("admin:stammdaten_leistungsartikel_import")
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.post(url, {"datei": SimpleUploadedFile("l.csv", b"Nummer;Bezeichnung\nL1;M\n")}).status_code, 403)
        self.assertFalse(Artikel.objects.exists())

    def test_typ_ist_im_artikel_admin_gesperrt(self):
        a = Artikel.objects.create(nummer="2", typ="L", bezeichnung="Dienst", steuersatz=self.st)
        r = self.client.get(reverse("admin:stammdaten_artikel_change", args=[a.pk]))
        self.assertNotContains(r, 'name="typ"')
        self.assertContains(self.client.get(reverse("admin:stammdaten_artikel_add")), 'name="typ"')

    def test_artikel_admin_zeigt_keine_leistungsfelder(self):
        a = Artikel.objects.create(nummer="2", typ="L", bezeichnung="Dienst", steuersatz=self.st)
        self.assertNotContains(self.client.get(reverse("admin:stammdaten_artikel_change", args=[a.pk])), 'name="kategorie"')
        self.assertNotContains(self.client.get(reverse("admin:stammdaten_artikel_add")), 'value="L"')
