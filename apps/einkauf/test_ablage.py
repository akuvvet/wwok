"""Prüfbericht B8: Hochgeladene Preislisten liegen nicht mehr im öffentlich ausgelieferten Medienordner und bleiben nicht liegen."""
import os
import tempfile
import time
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.kern.models import Mandant
from apps.stammdaten.models import Lieferant

from . import ablage
from .test_preislisten import profil

LISTE = "Best.-Nr.;Herst.-Nr.;EAN;Bezeichnung;Hersteller;EK;Bestand;Lieferzeit\nTD-1;MX-100;;Maus;Logitech;9,80;40;2\n".encode()


@override_settings(SECURE_SSL_REDIRECT=False)
class AblageTest(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.medien, self.privat = Path(self.tmp.name) / "medien", Path(self.tmp.name) / "privat"
        ueber = override_settings(MEDIA_ROOT=self.medien, PRIVAT_ROOT=self.privat)
        ueber.enable()
        self.addCleanup(ueber.disable)

    def dateien(self, ordner):
        return sorted(p.name for p in ordner.rglob("*") if p.is_file()) if ordner.exists() else []

    def test_speichern_lesen_loeschen_ausserhalb_der_medien(self):
        token = ablage.speichern(b"inhalt", "liste.csv")
        self.assertEqual(self.dateien(self.privat), [token])
        self.assertEqual(self.dateien(self.medien), [])
        self.assertEqual(ablage.lesen(token), (b"inhalt", "liste.csv"))
        ablage.loeschen(token)
        self.assertIsNone(ablage.lesen(token))
        self.assertIsNone(ablage.lesen(""))
        ablage.loeschen(token)  # zweimal löschen ist kein Fehler

    def test_token_kann_nicht_aus_dem_ordner_ausbrechen(self):
        geheim = Path(self.tmp.name) / "geheim.txt"
        geheim.write_bytes(b"x")
        self.assertIsNone(ablage.lesen("../../geheim.txt"))
        ablage.loeschen("../../geheim.txt")
        self.assertTrue(geheim.exists())
        token = ablage.speichern(b"a", "../../boese.csv")
        self.assertEqual(self.dateien(Path(self.tmp.name)), sorted(["geheim.txt", token]))
        self.assertTrue((self.privat / "preislisten" / token).is_file())

    def test_aufraeumen_loescht_alte_und_die_fruehere_oeffentliche_ablage(self):
        alt = ablage.speichern(b"alt", "alt.csv")
        gestern = time.time() - ablage.HALTBAR_SEKUNDEN - 60
        os.utime(self.privat / "preislisten" / alt, (gestern, gestern))
        frueher = self.medien / "tmp" / "preislisten"
        frueher.mkdir(parents=True)
        (frueher / "abc__liste.csv").write_bytes(b"ek-preise")
        neu = ablage.speichern(b"neu", "neu.csv")  # räumt beim Speichern auf
        self.assertEqual(self.dateien(self.privat), [neu])
        self.assertEqual(self.dateien(self.medien), [])

    def test_vorschau_und_import_ueber_die_maske(self):
        m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.client.force_login(get_user_model().objects.create_superuser("chef", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = m.pk
        s.save()
        p = profil(Lieferant.objects.create(nummer="70001", name1="Techdata GmbH"))
        p.quelle = "upload"
        p.save()
        url = reverse("einkauf:preisliste", args=[p.pk])
        from django.core.files.uploadedfile import SimpleUploadedFile
        seite = self.client.post(url, {"aktion": "vorschau", "datei": SimpleUploadedFile("liste.csv", LISTE)})
        token = seite.context["token"]
        self.assertEqual((self.dateien(self.privat), self.dateien(self.medien)), ([token], []))
        self.assertEqual(self.client.get(f"/medien/tmp/preislisten/{token}").status_code, 404)
        antwort = self.client.post(url, {"aktion": "importieren", "token": token})
        self.assertEqual(antwort.status_code, 302)
        self.assertEqual(self.dateien(self.privat), [])  # nach dem Import gelöscht
        nochmal = self.client.post(url, {"aktion": "importieren", "token": token}, follow=True)
        self.assertContains(nochmal, "Die Vorschau ist abgelaufen")
