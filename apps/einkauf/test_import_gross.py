from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from apps.stammdaten.models import Artikel, ArtikelLieferant, Lieferant, Steuersatz

from . import preislisten
from .models import Preislistenprofil


def csv_text(anzahl, preis="10,00"):
    zeilen = ["Best;Hnr;EAN;Bez;Hersteller;EK;Bestand"]
    zeilen += [f"B{i};H{i};;Artikel {i};;{preis};{i % 7}" for i in range(anzahl)]
    return "\n".join(zeilen).encode()


class GrosseListeTest(TestCase):
    def setUp(self):
        self.lf = Lieferant.objects.create(nummer="L1", name1="API GmbH")
        self.p = Preislistenprofil.objects.create(
            lieferant=self.lf, name="Voll", sp_bestellnummer="Best", sp_herstellernummer="Hnr", sp_ean="EAN", sp_bezeichnung="Bez",
            sp_hersteller="Hersteller", sp_ek="EK", sp_bestand="Bestand")

    def test_viele_zeilen_wenige_abfragen_und_wiederholung_ohne_schreiben(self):
        erst = preislisten.importieren(self.p, csv_text(1500))
        self.assertEqual((erst.neu, erst.fehler), (1500, 0))
        self.assertEqual(ArtikelLieferant.objects.count(), 1500)
        self.assertEqual(Artikel.objects.filter(typ="P").count(), 1500)
        with self.assertNumQueries(9):  # unveränderte Liste: nur Laden und Protokoll, kein Schreiben je Zeile
            zweit = preislisten.importieren(self.p, csv_text(1500))
        self.assertEqual((zweit.neu, zweit.unveraendert, zweit.geaendert), (0, 1500, 0))
        drittens = preislisten.importieren(self.p, csv_text(1500, "11,00"))
        self.assertEqual((drittens.geaendert, drittens.unveraendert), (1500, 0))
        self.assertEqual(ArtikelLieferant.objects.get(bestellnummer="B7").ek, Decimal("11.0000"))

    def test_doppelte_zeilen_in_der_datei(self):
        text = (csv_text(1).decode() + "\nB0;H0;;Artikel 0;;12,00;3").encode()  # Bestellnummer B0 zweimal
        prot = preislisten.importieren(self.p, text)
        self.assertEqual((prot.neu, prot.geaendert, prot.fehler), (1, 1, 0))
        self.assertEqual(ArtikelLieferant.objects.get().ek, Decimal("12.0000"))

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

    def test_nummernkollision_bei_gekuerzten_bestellnummern(self):
        lang = "X" * 50
        text = f"Best;Hnr;EAN;Bez;Hersteller;EK;Bestand\n{lang}A;;;Eins;;1,00;1\n{lang}B;;;Zwei;;2,00;1".encode()
        prot = preislisten.importieren(self.p, text)
        self.assertEqual((prot.neu, prot.fehler), (2, 0))
        self.assertEqual(Artikel.objects.filter(typ="P").count(), 2)
