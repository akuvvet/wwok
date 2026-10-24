from datetime import timedelta
from decimal import Decimal

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.stammdaten.models import Artikel, ArtikelLieferant, Lieferant, Preislistenartikel

from . import preislisten
from .models import Preislistenprofil


def csv_text(anzahl, preis="10,00"):
    zeilen = ["Best;Hnr;EAN;Bez;Hersteller;EK;Bestand"]
    zeilen += [f"B{i};H{i};;Artikel {i};Fabrikat;{preis};{i % 7}" for i in range(anzahl)]
    return "\n".join(zeilen).encode()


class GrosseListeTest(TestCase):
    def setUp(self):
        self.lf = Lieferant.objects.create(nummer="L1", name1="API GmbH", kurzname="API")
        self.p = Preislistenprofil.objects.create(
            lieferant=self.lf, name="Voll", sp_bestellnummer="Best", sp_herstellernummer="Hnr", sp_ean="EAN", sp_bezeichnung="Bez",
            sp_hersteller="Hersteller", sp_ek="EK", sp_bestand="Bestand")

    def test_viele_zeilen_wenige_abfragen_und_wiederholung_ohne_schreiben(self):
        erst = preislisten.importieren(self.p, csv_text(1500))
        self.assertEqual((erst.neu, erst.fehler), (1500, 0))
        self.assertEqual(Preislistenartikel.objects.count(), 1500)
        self.assertEqual(Artikel.objects.count(), 0)  # keine eigenen Artikel durch den Import
        with CaptureQueriesContext(connection) as abfragen:  # unveränderte Liste: Laden und Protokoll, nichts je Zeile
            zweit = preislisten.importieren(self.p, csv_text(1500))
        self.assertLess(len(abfragen), 15)
        self.assertEqual((zweit.neu, zweit.unveraendert, zweit.geaendert), (0, 1500, 0))
        drittens = preislisten.importieren(self.p, csv_text(1500, "11,00"))
        self.assertEqual((drittens.geaendert, drittens.unveraendert), (1500, 0))
        self.assertEqual(Preislistenartikel.objects.get(bestellnummer="B7").ek, Decimal("11.0000"))

    def test_doppelte_zeilen_in_der_datei(self):
        text = (csv_text(1).decode() + "\nB0;H0;;Artikel 0;Fabrikat;12,00;3").encode()  # Bestellnummer B0 zweimal
        prot = preislisten.importieren(self.p, text)
        self.assertEqual((prot.neu, prot.geaendert, prot.fehler), (1, 1, 0))
        self.assertEqual(Preislistenartikel.objects.get().ek, Decimal("12.0000"))

    def test_gleiche_bestellnummer_bei_verschiedenen_lieferanten_ist_erlaubt(self):
        anderer = Lieferant.objects.create(nummer="L2", name1="Kosatec")
        p2 = Preislistenprofil.objects.create(lieferant=anderer, name="Kosatec", sp_bestellnummer="Best", sp_bezeichnung="Bez",
                                              sp_ek="EK", sp_herstellernummer="Hnr")
        preislisten.importieren(self.p, csv_text(3))
        preislisten.importieren(p2, csv_text(3, "9,00"))
        self.assertEqual(Preislistenartikel.objects.count(), 6)
        self.assertEqual(Preislistenartikel.objects.get(lieferant=anderer, bestellnummer="B1").ek, Decimal("9.0000"))
        self.assertEqual(Preislistenartikel.objects.get(lieferant=self.lf, bestellnummer="B1").ek, Decimal("10.0000"))
        self.assertEqual(Preislistenartikel.objects.get(lieferant=anderer, bestellnummer="B1").lieferant.kurz, "Kosatec")

    def test_sperre_gegen_doppellauf(self):
        Preislistenprofil.objects.filter(pk=self.p.pk).update(laeuft_seit=timezone.now() - timedelta(minutes=5))
        with self.assertRaisesMessage(preislisten.PreislistenFehler, "läuft bereits"):
            preislisten.importieren(self.p, csv_text(3))
        Preislistenprofil.objects.filter(pk=self.p.pk).update(laeuft_seit=timezone.now() - timedelta(hours=4))  # abgestürzt
        self.assertEqual(preislisten.importieren(self.p, csv_text(3)).neu, 3)
        self.assertIsNone(Preislistenprofil.objects.get(pk=self.p.pk).laeuft_seit)  # Sperre wieder frei

    def test_formatfehler_hinterlaesst_keine_sperre(self):
        with self.assertRaises(preislisten.PreislistenFehler):
            preislisten.importieren(self.p, b"Falsch;Spalten\n1;2")
        self.assertIsNone(Preislistenprofil.objects.get(pk=self.p.pk).laeuft_seit)

    def test_artikel_wird_spaeter_angelegt_und_beim_naechsten_import_verknuepft(self):
        preislisten.importieren(self.p, csv_text(2))
        self.assertFalse(Preislistenartikel.objects.exclude(artikel=None).exists())
        a = Artikel.objects.create(nummer="EIGEN1", bezeichnung="Eigener", herstellernummer="H1",
                                   steuersatz=__import__("apps.stammdaten.models", fromlist=["Steuersatz"]).Steuersatz.objects.get(nummer=1))
        prot = preislisten.importieren(self.p, csv_text(2))
        self.assertEqual(prot.geaendert, 1)  # nur die Zeile mit der passenden Herstellernummer bekommt die Verknüpfung
        self.assertEqual(Preislistenartikel.objects.get(bestellnummer="B1").artikel, a)
        self.assertEqual(ArtikelLieferant.objects.get().artikel, a)
