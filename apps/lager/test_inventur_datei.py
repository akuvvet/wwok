"""Zählliste drucken (nur die Liste als PDF), als CSV/Excel exportieren und gezählte Mengen aus CSV/Excel einlesen
(Wunsch des Nutzers 26.11.2026)."""
import io
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from openpyxl import Workbook, load_workbook
from pypdf import PdfReader

from apps.kern.models import Mandant
from apps.stammdaten.models import Artikel, Steuersatz

from .inventur_datei import import_lesen
from .models import Bewegungsart, bestand
from .services import buche_manuell


@override_settings(SECURE_SSL_REDIRECT=False)
class InventurDateiTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        st = Steuersatz.objects.get(nummer=1)
        self.maus = Artikel.objects.create(nummer="M100", bezeichnung="Funkmaus", steuersatz=st)
        self.kabel = Artikel.objects.create(nummer="K200", bezeichnung="HDMI Kabel", steuersatz=st)
        self.leer = Artikel.objects.create(nummer="L400", bezeichnung="ohne Bestand", steuersatz=st)
        self.leistung = Artikel.objects.create(nummer="S1", bezeichnung="Service", steuersatz=st, typ="L")
        buche_manuell(self.m, self.maus, Bewegungsart.WARENEINGANG, Decimal("5"))
        buche_manuell(self.m, self.kabel, Bewegungsart.WARENEINGANG, Decimal("2"))
        self.client.force_login(get_user_model().objects.create_superuser("chef", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()
        self.url = reverse("lager:inventur_liste")

    def test_druck_ist_pdf_nur_mit_der_liste(self):
        r = self.client.get(self.url, {"ausgabe": "pdf", "q": "maus"})
        self.assertEqual(r["Content-Type"], "application/pdf")
        text = "\n".join(s.extract_text() or "" for s in PdfReader(io.BytesIO(r.content)).pages)
        self.assertIn("Zählliste", text)
        self.assertIn("M100", text)
        self.assertNotIn("K200", text)  # Filter gilt auch für den Druck
        self.assertNotIn("Differenzen buchen", text)

    def test_druck_ganze_liste_nicht_nur_seite(self):
        st = Steuersatz.objects.get(nummer=1)
        for i in range(120):
            a = Artikel.objects.create(nummer=f"V{i:03d}", bezeichnung="viel", steuersatz=st)
            buche_manuell(self.m, a, Bewegungsart.WARENEINGANG, Decimal("1"))
        r = self.client.get(self.url, {"ausgabe": "csv"})
        self.assertEqual(r.content.decode("utf-8-sig").strip().count("\r\n"), 122)  # Kopf + 122 Artikel, kein Blättern

    def test_export_csv_und_excel(self):
        r = self.client.get(self.url, {"ausgabe": "csv"})
        self.assertIn("attachment", r["Content-Disposition"])
        zeilen = r.content.decode("utf-8-sig").splitlines()
        self.assertEqual(zeilen[0], "Lagerplatz;Artikel;Bezeichnung;Soll;gezählt")
        self.assertIn(";M100;Funkmaus;5;", zeilen)
        r = self.client.get(self.url, {"ausgabe": "xlsx"})
        blatt = load_workbook(io.BytesIO(r.content)).active
        werte = [[c.value for c in z] for z in blatt.iter_rows()]
        self.assertEqual(werte[0], ["Lagerplatz", "Artikel", "Bezeichnung", "Soll", "gezählt"])
        self.assertIn(["K200", 2], [[z[1], z[3]] for z in werte[1:]])

    def einlesen(self, name, inhalt):
        return self.client.post(reverse("lager:inventur_import"), {"datei": SimpleUploadedFile(name, inhalt)})

    def test_export_ausfuellen_einlesen_vorschau_buchen(self):
        """Rundweg: exportierte Liste ausfüllen, einlesen, Vorschau prüfen, erst dann buchen."""
        zeilen = self.client.get(self.url, {"ausgabe": "csv"}).content.decode("utf-8-sig").splitlines()
        ausgefuellt = [zeilen[0]] + [z + ("4" if ";M100;" in z else "2") for z in zeilen[1:]]
        ausgefuellt.append(";l400;nachgezählt;0;1,5")  # Artikel ohne Bestand, Kleinschreibung
        r = self.einlesen("zaehlung.csv", ("\r\n".join(ausgefuellt)).encode("utf-8"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context["abweichungen"], 2)
        self.assertEqual(bestand(self.m, self.maus), Decimal("5"))  # Vorschau bucht nichts
        post = {}
        for z in r.context["zeilen"]:
            post[f"soll_{z['artikel'].pk}"] = str(z["soll"])
            post[f"gezaehlt_{z['artikel'].pk}"] = str(z["gezaehlt"])
        self.client.post(self.url, post)
        self.assertEqual(bestand(self.m, self.maus), Decimal("4"))
        self.assertEqual(bestand(self.m, self.kabel), Decimal("2"))
        self.assertEqual(bestand(self.m, self.leer), Decimal("1.5"))

    def test_excel_mit_fehlern_und_doppelten_zeilen(self):
        buch = Workbook()
        blatt = buch.active
        blatt.append(["Inventur Lager 2"])  # Titelzeile vor der Kopfzeile
        blatt.append(["Artikelnummer", "Ist"])
        for zeile in (["M100", 3], ["M100", 1.0], ["S1", 1], ["GIBTESNICHT", 2], ["K200", "abc"], ["K200", -1], ["K200", None]):
            blatt.append(zeile)
        puffer = io.BytesIO()
        buch.save(puffer)
        r = self.einlesen("zaehlung.xlsx", puffer.getvalue())
        zeilen = r.context["zeilen"]
        self.assertEqual([(z["artikel"].nummer, z["gezaehlt"], z["zeilen"]) for z in zeilen], [("M100", Decimal("4.0"), 2)])
        fehler = " ".join(r.context["fehler"])
        for teil in ("S1: kein Lagerartikel", "GIBTESNICHT: Artikel nicht gefunden", "keine Zahl", "negative Menge"):
            self.assertIn(teil, fehler)
        self.assertContains(r, "2 Zeilen addiert")

    def test_ohne_passende_kopfzeile(self):
        r = self.einlesen("x.csv", b"a;b\n1;2\n")
        self.assertRedirects(r, self.url, fetch_redirect_response=False)
        with self.assertRaisesMessage(Exception, "Keine Kopfzeile"):
            import_lesen("x.csv", b"a;b\n1;2\n")

    def test_seite_hat_knoepfe_statt_browserdruck(self):
        seite = self.client.get(self.url).content.decode()
        self.assertNotIn("window.print()", seite)
        for teil in ("ausgabe=pdf", "ausgabe=xlsx", "ausgabe=csv", reverse("lager:inventur_import")):
            self.assertIn(teil, seite)
