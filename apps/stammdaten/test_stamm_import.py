"""Import in der Verwaltung für Artikel, Leistungsartikel, Kategorien, Leistungskategorien und Hersteller, jeweils mit
Beispieldatei (CSV/Excel) zum Herunterladen (Wunsch des Nutzers 26.11.2026)."""
import io
from decimal import Decimal as D

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from openpyxl import load_workbook

from apps.kern.models import Mandant

from .leistung_import import ImportFehler, importieren
from .models import Artikel, Artikeltyp, EkHerkunft, Hersteller, Kategorie, Leistungskategorie, Steuersatz
from .stamm_import import BEISPIELE, beispiel, hersteller_importieren, kategorien_importieren, leistungskategorien_importieren

LISTEN = {"artikel": "artikel", "leistungsartikel": "leistungsartikel", "kategorie": "kategorie",
          "leistungskategorie": "leistungskategorie", "hersteller": "hersteller"}


def csv_datei(*zeilen):
    return ("\r\n".join(";".join(z) for z in zeilen)).encode("utf-8")


@override_settings(SECURE_SSL_REDIRECT=False)
class VerwaltungImportTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.client.force_login(get_user_model().objects.create_superuser("chef", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    def test_knopf_seite_und_beispiel_rundweg_fuer_alle(self):
        """Jede Liste hat „Importieren“; die Importseite bietet beide Beispieldateien; die Beispieldatei lässt sich so, wie sie
        ist, wieder einlesen (CSV und Excel)."""
        for art, modell in LISTEN.items():
            with self.subTest(art):
                liste = reverse(f"admin:stammdaten_{modell}_changelist")
                import_url = reverse(f"admin:stammdaten_{modell}_import")
                self.assertContains(self.client.get(liste), import_url)
                seite = self.client.get(import_url)
                for format_ in ("csv", "xlsx"):
                    url = reverse(f"admin:stammdaten_{modell}_beispiel", args=[format_])
                    self.assertContains(seite, url)
                    datei = self.client.get(url)
                    self.assertIn(f"beispiel-{art}.{format_}", datei["Content-Disposition"])
                    antwort = self.client.post(import_url, {"datei": SimpleUploadedFile(f"b.{format_}", datei.content)}, follow=True)
                    self.assertRedirects(antwort, liste)
                    self.assertContains(antwort, "neu" if format_ == "csv" else "unverändert")
        a = Artikel.objects.get(nummer="99001")
        self.assertEqual((a.typ, a.kategorie.name, a.hersteller.name, a.ean, a.ek_manuell, a.vk1, a.gewicht_kg, a.garantie_monate),
                         ("A", "Netzteile", "LC-Power", "4260070120017", D("18.50"), D("29.90"), D("1.200"), 24))
        self.assertEqual(Artikel.objects.get(nummer="40098").typ, Artikeltyp.L)
        self.assertEqual(Kategorie.objects.get(nummer="110").eltern.nummer, "100")

    def test_beispiel_excel_hat_kopf_und_eine_zeile(self):
        for art, (kopf, zeilen) in BEISPIELE.items():
            blatt = load_workbook(io.BytesIO(beispiel(art, "xlsx"))).active
            werte = [[c.value for c in z] for z in blatt.iter_rows()]
            self.assertEqual(werte[0], kopf)
            self.assertEqual(len(werte), 1 + len(zeilen))
        blatt = load_workbook(io.BytesIO(beispiel("artikel", "xlsx"))).active
        self.assertEqual(blatt["G2"].value, "4260070120017")  # EAN bleibt Text, keine 4,26E+12

    def test_ohne_rechte_kein_import(self):
        nutzer = get_user_model().objects.create_user("lager", password="x" * 14, is_staff=True)
        self.client.force_login(nutzer)
        self.assertEqual(self.client.get(reverse("admin:stammdaten_hersteller_import")).status_code, 403)
        self.assertEqual(self.client.get(reverse("admin:stammdaten_hersteller_beispiel", args=["csv"])).status_code, 403)


class ArtikelImportTest(TestCase):
    def test_neu_aktualisieren_und_fehler(self):
        st19 = Steuersatz.objects.get(nummer=1)
        Artikel.objects.create(nummer="A1", bezeichnung="Alt", steuersatz=st19, vk1=D("10"))
        Artikel.objects.create(nummer="L1", bezeichnung="Leistung", steuersatz=st19, typ="L")
        kopf = ["Nummer", "Bezeichnung", "EK", "VK1", "Hersteller", "Online", "EAN"]
        neu, geaendert, gleich, kategorien = importieren("a.csv", csv_datei(kopf, ["A1", "", "5,50", "", "", "ja", ""],
                                                                           ["A2", "Neu", "", "12", "Acme", "", "12345678"]),
                                                          Artikeltyp.A)
        self.assertEqual((neu, geaendert, gleich), (1, 1, 0))
        a1 = Artikel.objects.get(nummer="A1")
        self.assertEqual((a1.bezeichnung, a1.vk1, a1.ek_manuell, a1.ek_herkunft, a1.online), ("Alt", D("10.00"), D("5.50"),
                                                                                              EkHerkunft.MANUELL, True))
        self.assertEqual(Artikel.objects.get(nummer="A2").hersteller.name, "Acme")
        with self.assertRaises(ImportFehler) as f:
            importieren("a.csv", csv_datei(kopf, ["L1", "x", "", "", "", "", ""], ["A3", "y", "", "", "", "", "12ab"],
                                           ["A4", "z", "", "", "", "vielleicht", ""]), Artikeltyp.A)
        text = " ".join(f.exception.fehler)
        for teil in ("L1 ist schon als", "Spalte ean", "Spalte online"):
            self.assertIn(teil, text)
        self.assertFalse(Artikel.objects.filter(nummer="A3").exists())  # alles oder nichts

    def test_mehrdeutige_kategorie(self):
        Kategorie.objects.create(name="Kabel", eltern=Kategorie.objects.create(name="PC"))
        Kategorie.objects.create(name="Kabel", eltern=Kategorie.objects.create(name="TV"))
        with self.assertRaisesMessage(ImportFehler, "Nichts importiert"):
            importieren("a.csv", csv_datei(["Nummer", "Bezeichnung", "Kategorie"], ["K1", "HDMI", "Kabel"]), Artikeltyp.A)


class KategorieHerstellerImportTest(TestCase):
    def test_kategorien_mit_ebenen(self):
        Kategorie.objects.create(nummer="1", name="Hardware")
        datei = csv_datei(["Nummer", "Name", "Übergeordnet", "Aufschlag VK1"], ["2", "Speicher", "Hardware", "25"],
                          ["3", "SSD", "2", "20,5"])
        self.assertEqual(kategorien_importieren("k.csv", datei), (2, 0, 0))
        self.assertEqual(kategorien_importieren("k.csv", datei), (0, 0, 2))
        ssd = Kategorie.objects.get(nummer="3")
        self.assertEqual((ssd.eltern.name, ssd.aufschlag_vk1, ssd.online), ("Speicher", D("20.50"), False))
        with self.assertRaises(ImportFehler) as f:  # Ring: Hardware unter die eigene Unterkategorie
            kategorien_importieren("k.csv", csv_datei(["Nummer", "Name", "Übergeordnet"], ["1", "Hardware", "3"],
                                                      ["9", "Neu", "Gibtsnicht"]))
        self.assertIn("eigene Unterkategorie", " ".join(f.exception.fehler))
        self.assertIn("nicht gefunden", " ".join(f.exception.fehler))
        self.assertIsNone(Kategorie.objects.get(nummer="1").eltern)

    def test_leistungskategorien_und_hersteller(self):
        Leistungskategorie.objects.create(nummer="10", name="Werkstat")
        self.assertEqual(leistungskategorien_importieren("l.csv", csv_datei(["Nummer", "Name"], ["10", "Werkstatt"], ["", "Büro"])),
                         (1, 1, 0))
        self.assertEqual(Leistungskategorie.objects.get(nummer="10").name, "Werkstatt")
        Hersteller.objects.create(name="lc power")
        self.assertEqual(hersteller_importieren("h.csv", csv_datei(["Kennzeichen", "Hersteller", "Online"], ["LCP", "LC-Power", "ja"],
                                                                   ["", "LC Power", ""])), (1, 1, 0))
        self.assertTrue(Hersteller.objects.get(kennzeichen="LCP").online)
        with self.assertRaisesMessage(ImportFehler, "In der Kopfzeile fehlt: Name"):
            hersteller_importieren("h.csv", csv_datei(["Kennzeichen"], ["X"]))
