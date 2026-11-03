import json
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.kern.models import Mandant

from .models import Artikel, ArtikelMandant, Kategorie, Stammnummer, Steuersatz


@override_settings(SECURE_SSL_REDIRECT=False)
class ArtikelMaskeTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.st = Steuersatz.objects.create(bezeichnung="19", prozent=Decimal("19"))
        self.client.force_login(get_user_model().objects.create_user("t", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    def daten(self, **extra):
        d = {"nummer": "5000", "typ": "A", "bezeichnung": "Maus", "mengeneinheit": "Stück", "steuersatz": self.st.pk,
             "vk1": "10.00", "md-lagerplatz": "R1", "md-mindestbestand": "2", "md-maximalbestand": "10"}
        d.update(extra)
        return d

    def test_login_noetig(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse("stammdaten:artikel_neu")).status_code, 302)

    def test_neuer_artikel_bekommt_naechste_nummer_und_registerkarten(self):
        Artikel.objects.create(nummer="24007", bezeichnung="Alt", steuersatz=self.st)
        Stammnummer.setzen(Stammnummer.Art.ARTIKEL, 24007)
        r = self.client.get(reverse("stammdaten:artikel_neu"))
        self.assertContains(r, "24008")
        for titel in ("Stammdaten", "Preise", "Maße &amp; Versand", "Online &amp; Texte", "Mandant"):
            self.assertContains(r, titel)

    def test_anlegen_speichert_artikel_und_mandantendaten(self):
        r = self.client.post(reverse("stammdaten:artikel_neu"), self.daten())
        self.assertEqual(r.status_code, 200)
        a = Artikel.objects.get(nummer="5000")
        self.assertEqual(json.loads(r.content)["ziel"], reverse("lager:artikel", args=[a.pk]))
        md = ArtikelMandant.objects.get(artikel=a, mandant=self.m)
        self.assertEqual((md.lagerplatz, md.mindestbestand, md.maximalbestand), ("R1", 2, 10))

    def test_maximalbestand_unter_minimum_und_doppelte_nummer_werden_abgelehnt(self):
        r = self.client.post(reverse("stammdaten:artikel_neu"), self.daten(**{"md-maximalbestand": "1"}))
        self.assertEqual(r.status_code, 422)
        self.assertFalse(Artikel.objects.exists())
        Artikel.objects.create(nummer="5000", bezeichnung="Vorhanden", steuersatz=self.st)
        r = self.client.post(reverse("stammdaten:artikel_neu"), self.daten())
        self.assertEqual(r.status_code, 422)
        self.assertEqual(Artikel.objects.count(), 1)

    def test_p_artikel_nicht_anlegbar_und_typ_beim_bearbeiten_gesperrt(self):
        r = self.client.post(reverse("stammdaten:artikel_neu"), self.daten(typ="P"))
        self.assertEqual(r.status_code, 422)
        a = Artikel.objects.create(nummer="5001", bezeichnung="Lager", steuersatz=self.st)
        r = self.client.post(reverse("stammdaten:artikel_bearbeiten", args=[a.pk]), self.daten(nummer="5001", typ="L"))
        self.assertEqual(r.status_code, 200)
        a.refresh_from_db()
        self.assertEqual(a.typ, "A")  # Typwechsel wird ignoriert

    def test_artikelansicht_zeigt_bestellt_und_bearbeiten(self):
        a = Artikel.objects.create(nummer="5002", bezeichnung="Kabel", steuersatz=self.st)
        r = self.client.get(reverse("lager:artikel", args=[a.pk]))
        self.assertContains(r, "bestellt (offen)")
        self.assertContains(r, reverse("stammdaten:artikel_bearbeiten", args=[a.pk]))

    def test_esd_key_nur_in_kategorie_software(self):
        software = Kategorie.objects.create(name="Software")
        hardware = Kategorie.objects.create(name="Hardware")
        r = self.client.post(reverse("stammdaten:artikel_neu"), self.daten(esd_key="on", kategorie=hardware.pk))
        self.assertEqual(r.status_code, 422)
        r = self.client.post(reverse("stammdaten:artikel_neu"), self.daten(esd_key="on", kategorie=software.pk))
        self.assertEqual(r.status_code, 200)
        self.assertTrue(Artikel.objects.get(nummer="5000").esd_key)

    def png(self, name="a.png", groesse=(4, 4), format="PNG", farbe="red"):
        import io

        from django.core.files.uploadedfile import SimpleUploadedFile
        from PIL import Image
        puffer = io.BytesIO()
        Image.new("RGB", groesse, farbe).save(puffer, format)
        return SimpleUploadedFile(name, puffer.getvalue(), content_type=f"image/{format.lower()}")

    def bilder(self, reihe, **dateien):
        """Formularfelder der Galerie: reihe = Liste aus „b<id>“ / „n<marke>“, dateien = bild_neu_<marke>=Datei."""
        return {"bilder_geladen": "1", "bild_reihe": reihe, **dateien}

    def test_reiter_bilder_zeigt_galerie_und_zaehler(self):
        r = self.client.get(reverse("stammdaten:artikel_neu"))
        self.assertContains(r, "data-bilder")
        self.assertContains(r, "data-bild-neu")
        self.assertContains(r, "(0/10)")

    def test_bilder_hochladen_beim_anlegen_und_reihenfolge_aendern(self):
        import tempfile

        with tempfile.TemporaryDirectory() as ordner, override_settings(MEDIA_ROOT=ordner):
            r = self.client.post(reverse("stammdaten:artikel_neu"), self.daten(
                **self.bilder(["n1", "n2"], bild_neu_1=self.png("a.png"), bild_neu_2=self.png("b.png", farbe="blue")),
                bild_alt_n1="Vorderseite"))
            self.assertEqual(r.status_code, 200)
            a = Artikel.objects.get(nummer="5000")
            erstes, zweites = list(a.bilder.all())
            self.assertEqual((erstes.position, zweites.position, erstes.alt_text), (1, 2, "Vorderseite"))
            self.assertTrue(erstes.bild.name.startswith(f"artikel/{a.pk}/"))
            self.assertNotEqual(erstes.bild.name, zweites.bild.name)
            url = reverse("stammdaten:artikel_bearbeiten", args=[a.pk])
            self.assertContains(self.client.get(url), "(2/10)")
            # Reihenfolge tauschen und ein drittes Bild anhängen
            r = self.client.post(url, self.daten(**self.bilder([f"b{zweites.pk}", f"b{erstes.pk}", "n7"], bild_neu_7=self.png("c.png")),
                                                 **{f"bild_alt_b{erstes.pk}": "neu beschriftet"}))
            self.assertEqual(r.status_code, 200)
            self.assertEqual([(b.pk, b.position) for b in a.bilder.all()][:2], [(zweites.pk, 1), (erstes.pk, 2)])
            self.assertEqual(a.bilder.count(), 3)
            erstes.refresh_from_db()
            self.assertEqual(erstes.alt_text, "neu beschriftet")

    def test_vorschau_ist_verkleinert(self):
        import tempfile

        from PIL import Image
        with tempfile.TemporaryDirectory() as ordner, override_settings(MEDIA_ROOT=ordner):
            self.client.post(reverse("stammdaten:artikel_neu"), self.daten(
                **self.bilder(["n1"], bild_neu_1=self.png("gross.png", groesse=(1600, 1000)))))
            bild = Artikel.objects.get(nummer="5000").bilder.get()
            self.assertTrue(bild.vorschau.name.startswith(f"artikel/{bild.artikel_id}/vorschau/"))
            with Image.open(bild.vorschau.path) as v:
                self.assertLessEqual(max(v.size), 320)
            with Image.open(bild.bild.path) as o:
                self.assertEqual(o.size, (1600, 1000))
            self.assertEqual(bild.vorschau_url, bild.vorschau.url)

    def test_hoechstens_zehn_bilder(self):
        import tempfile

        with tempfile.TemporaryDirectory() as ordner, override_settings(MEDIA_ROOT=ordner):
            marken = [f"n{i}" for i in range(11)]
            dateien = {f"bild_neu_{i}": self.png(f"{i}.png") for i in range(11)}
            r = self.client.post(reverse("stammdaten:artikel_neu"), self.daten(**self.bilder(marken, **dateien)))
            self.assertEqual(r.status_code, 422)
            self.assertContains(r, "höchstens 10 Bilder", status_code=422)
            self.assertFalse(Artikel.objects.exists())
            dateien = {f"bild_neu_{i}": self.png(f"{i}.png") for i in range(10)}
            r = self.client.post(reverse("stammdaten:artikel_neu"), self.daten(**self.bilder(marken[:10], **dateien)))
            self.assertEqual(r.status_code, 200)
            self.assertEqual(Artikel.objects.get(nummer="5000").bilder.count(), 10)

    def test_ungueltige_formate_und_zu_grosse_dateien_werden_abgelehnt(self):
        import tempfile

        from django.core.files.uploadedfile import SimpleUploadedFile
        with tempfile.TemporaryDirectory() as ordner, override_settings(MEDIA_ROOT=ordner):
            url = reverse("stammdaten:artikel_neu")
            kaputt = SimpleUploadedFile("x.png", b"kein bild", content_type="image/png")
            r = self.client.post(url, self.daten(**self.bilder(["n1"], bild_neu_1=kaputt)))
            self.assertEqual(r.status_code, 422)
            gif = self.png("x.gif", format="GIF")
            r = self.client.post(url, self.daten(**self.bilder(["n1"], bild_neu_1=gif)))
            self.assertContains(r, "nur JPG, PNG oder WEBP", status_code=422)
            with mock.patch("apps.stammdaten.masken.MAX_BILD_BYTES", 10):
                r = self.client.post(url, self.daten(**self.bilder(["n1"], bild_neu_1=self.png())))
            self.assertContains(r, "größer als", status_code=422)
            self.assertFalse(Artikel.objects.exists())

    def test_bild_loeschen_entfernt_auch_die_dateien(self):
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as ordner, override_settings(MEDIA_ROOT=ordner):
            self.client.post(reverse("stammdaten:artikel_neu"), self.daten(**self.bilder(["n1", "n2"], bild_neu_1=self.png(), bild_neu_2=self.png("b.png"))))
            a = Artikel.objects.get(nummer="5000")
            eins, zwei = list(a.bilder.all())
            pfade = [eins.bild.path, eins.vorschau.path]
            self.assertTrue(all(os.path.exists(p) for p in pfade))
            r = self.client.post(reverse("stammdaten:artikel_bearbeiten", args=[a.pk]), self.daten(**self.bilder([f"b{zwei.pk}"])))
            self.assertEqual(r.status_code, 200)
            self.assertEqual(list(a.bilder.values_list("pk", "position")), [(zwei.pk, 1)])  # rückt auf Hauptbild
            self.assertFalse(any(os.path.exists(p) for p in pfade))

    def test_ohne_marker_bleiben_bilder_unberuehrt_und_unbekannte_ids_werden_ignoriert(self):
        import tempfile

        with tempfile.TemporaryDirectory() as ordner, override_settings(MEDIA_ROOT=ordner):
            self.client.post(reverse("stammdaten:artikel_neu"), self.daten(**self.bilder(["n1"], bild_neu_1=self.png())))
            a = Artikel.objects.get(nummer="5000")
            url = reverse("stammdaten:artikel_bearbeiten", args=[a.pk])
            self.client.post(url, self.daten())
            self.assertEqual(a.bilder.count(), 1)
            self.client.post(url, self.daten(**self.bilder(["b99999"])))  # unbekannte Bild-ID: wird ignoriert, das vorhandene gelöscht
            self.assertEqual(a.bilder.count(), 0)

    def test_hauptbild_in_der_artikelliste(self):
        import tempfile

        with tempfile.TemporaryDirectory() as ordner, override_settings(MEDIA_ROOT=ordner):
            self.client.post(reverse("stammdaten:artikel_neu"), self.daten(**self.bilder(["n1"], bild_neu_1=self.png())))
            bild = Artikel.objects.get(nummer="5000").bilder.get()
            r = self.client.get(reverse("lager:uebersicht"))
            self.assertContains(r, bild.vorschau.url)
