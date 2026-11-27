"""Bilder beim Artikelimport in der Verwaltung (Wunsch des Nutzers 27.11.2026): Spalten „Abbildung 1–10“ mit Dateiname oder
Pfad, die Dateien als ZIP, höchstens 2 MB pro Bild; alles oder nichts."""
import io
import shutil
import tempfile
import zipfile

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from PIL import Image

from apps.kern.models import Mandant

from .leistung_import import BilderZip, ImportFehler, importieren
from .models import Artikel, ArtikelBild, Artikeltyp, Steuersatz

MEDIEN = tempfile.mkdtemp(prefix="wwok-bildimport-")


def bild(farbe="red", format_="JPEG", groesse=(40, 30)):
    puffer = io.BytesIO()
    Image.new("RGB", groesse, farbe).save(puffer, format_)
    return puffer.getvalue()


def zip_datei(**dateien):
    puffer = io.BytesIO()
    with zipfile.ZipFile(puffer, "w") as z:
        for name, inhalt in dateien.items():
            z.writestr(name.replace("__", "/"), inhalt)
    puffer.seek(0)
    return puffer


def csv_datei(*zeilen):
    return ("\r\n".join(";".join(z) for z in zeilen)).encode("utf-8")


KOPF = ["Nummer", "Bezeichnung", "Abbildung 1", "Abbildung 2"]


@override_settings(MEDIA_ROOT=MEDIEN)
class BilderImportTest(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIEN, ignore_errors=True)

    def _import(self, *zeilen, **dateien):
        statistik = {}
        with BilderZip.oeffnen(zip_datei(**dateien) if dateien else None) as bilder:
            ergebnis = importieren("a.csv", csv_datei(KOPF, *zeilen), Artikeltyp.A, bilder=bilder, statistik=statistik)
        return ergebnis, statistik

    def test_bilder_aus_zip_mit_mention_pfad(self):
        (neu, *_), statistik = self._import(["B1", "Monitor", r"C:\Mention\Bilder\b1.jpg", "b1-seite.PNG"],
                                            **{"ordner__B1.JPG": bild(), "b1-seite.png": bild("blue", "PNG")})
        a = Artikel.objects.get(nummer="B1")
        self.assertEqual((neu, statistik["bilder"]), (1, 2))
        self.assertEqual([b.position for b in a.bilder.all()], [1, 2])
        self.assertTrue(a.bilder.get(position=1).vorschau)  # Vorschau wie beim Hochladen in der Maske

    def test_vorhandenes_bild_ersetzt_gleiches_bleibt_leere_zelle_laesst_stehen(self):
        self._import(["B2", "Maus", "b2.jpg", "b2b.jpg"], **{"b2.jpg": bild(), "b2b.jpg": bild("green")})
        a = Artikel.objects.get(nummer="B2")
        alt1, alt2 = a.bilder.get(position=1).pk, a.bilder.get(position=2).pk
        (_, geaendert, gleich, _), statistik = self._import(["B2", "", "b2.jpg", ""], **{"b2.jpg": bild()})
        self.assertEqual((geaendert, gleich, statistik["bilder"]), (0, 1, 0))  # gleiches Bild: nichts zu tun
        self.assertEqual(a.bilder.get(position=1).pk, alt1)
        (_, geaendert, _, _), statistik = self._import(["B2", "", "neu.jpg", ""], **{"neu.jpg": bild("yellow")})
        self.assertEqual((geaendert, statistik["bilder"]), (1, 1))
        self.assertNotEqual(a.bilder.get(position=1).pk, alt1)
        self.assertEqual(a.bilder.get(position=2).pk, alt2)  # leere Zelle: Bild bleibt
        self.assertEqual(a.bilder.count(), 2)

    def test_fehler_alles_oder_nichts(self):
        gross = bild(groesse=(3000, 3000))
        gross = gross + b"\0" * (2 * 1024 * 1024 + 1 - len(gross)) if len(gross) <= 2 * 1024 * 1024 else gross
        with self.assertRaises(ImportFehler) as f:
            self._import(["F1", "ok", "f1.jpg", ""], ["F2", "fehlt", "weg.jpg", ""], ["F3", "kaputt", "kaputt.jpg", ""],
                         ["F4", "gross", "gross.jpg", ""], ["F5", "text", "notiz.txt", ""],
                         **{"f1.jpg": bild(), "kaputt.jpg": b"kein bild", "gross.jpg": gross, "notiz.txt": b"x"})
        text = " ".join(f.exception.fehler)
        for teil in ("Zeile 3: Bild weg.jpg fehlt im ZIP", "Zeile 4: Bild kaputt.jpg: keine gültige Bilddatei",
                     "Zeile 5: Bild gross.jpg ist größer als 2 MB", "Zeile 6: Bild notiz.txt: nur JPG, PNG oder WEBP"):
            self.assertIn(teil, text)
        self.assertFalse(Artikel.objects.filter(nummer="F1").exists())
        self.assertFalse(ArtikelBild.objects.exists())

    def test_doppelter_name_im_zip_ist_mehrdeutig(self):
        with self.assertRaisesMessage(ImportFehler, "Nichts importiert"):
            self._import(["D1", "x", "d.jpg", ""], **{"a__d.jpg": bild(), "b__D.jpg": bild("blue")})

    def test_ohne_zip_werden_bildspalten_uebersprungen(self):
        (neu, *_), statistik = self._import(["O1", "ohne", "o1.jpg", "o2.jpg"])
        self.assertEqual((neu, statistik["ohne_zip"], statistik["bilder"]), (1, 2, 0))
        self.assertFalse(ArtikelBild.objects.exists())

    def test_kein_zip(self):
        with self.assertRaisesMessage(ImportFehler, "kein gültiges ZIP"):
            BilderZip(io.BytesIO(b"kein zip"))


@override_settings(MEDIA_ROOT=MEDIEN, SECURE_SSL_REDIRECT=False)
class BilderImportSeiteTest(TestCase):
    def setUp(self):
        m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.client.force_login(get_user_model().objects.create_superuser("chef", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = m.pk
        s.save()

    def test_artikel_seite_hat_zip_feld_andere_nicht(self):
        self.assertContains(self.client.get(reverse("admin:stammdaten_artikel_import")), 'name="bilder"')
        self.assertNotContains(self.client.get(reverse("admin:stammdaten_hersteller_import")), 'name="bilder"')

    def test_hochladen_mit_zip(self):
        Steuersatz.objects.get(nummer=1)
        antwort = self.client.post(reverse("admin:stammdaten_artikel_import"), {
            "datei": SimpleUploadedFile("a.csv", csv_datei(KOPF, ["S1", "Tastatur", "s1.webp", ""])),
            "bilder": SimpleUploadedFile("bilder.zip", zip_datei(**{"s1.webp": bild(format_="WEBP")}).getvalue())},
            follow=True)
        self.assertContains(antwort, "1 Bild(er) übernommen")
        self.assertEqual(Artikel.objects.get(nummer="S1").bilder.count(), 1)

    def test_ohne_zip_hinweis(self):
        antwort = self.client.post(reverse("admin:stammdaten_artikel_import"), {
            "datei": SimpleUploadedFile("a.csv", csv_datei(KOPF, ["S2", "Maus", "s2.jpg", ""]))}, follow=True)
        self.assertContains(antwort, "1 Bildangabe(n) übersprungen")
