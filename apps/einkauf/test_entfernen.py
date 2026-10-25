from decimal import Decimal

from django.test import TestCase

from apps.kern.models import Mandant
from apps.stammdaten.models import Artikel, ArtikelLieferant, Kunde, Lieferant, Preislistenartikel, Steuersatz
from apps.verkauf.models import Beleg, Belegart, Position

from . import preislisten, services
from .models import Preislistenprofil


def csv_text(nummern, preis="10,00"):
    zeilen = ["Best;Hnr;Bez;Hersteller;EK;Bestand"]
    zeilen += [f"B{i};H{i};Artikel {i};Fabrikat;{preis};3" for i in nummern]
    return "\n".join(zeilen).encode()


class VeralteteEntfernenTest(TestCase):
    def setUp(self):
        self.lf = Lieferant.objects.create(nummer="L1", name1="API GmbH", kurzname="API")
        self.anderer = Lieferant.objects.create(nummer="L2", name1="Kosatec")
        self.p = Preislistenprofil.objects.create(
            lieferant=self.lf, name="Voll", sp_bestellnummer="Best", sp_herstellernummer="Hnr", sp_bezeichnung="Bez",
            sp_hersteller="Hersteller", sp_ek="EK", sp_bestand="Bestand")

    def nummern(self, lieferant=None):
        return sorted(Preislistenartikel.objects.filter(lieferant=lieferant or self.lf).values_list("bestellnummer", flat=True))

    def test_nicht_mehr_gelistete_werden_entfernt_neue_kommen_dazu(self):
        preislisten.importieren(self.p, csv_text(range(6)))
        Preislistenartikel.objects.create(lieferant=self.anderer, bestellnummer="B0", bezeichnung="fremd", ek=Decimal("1"))
        prot = preislisten.importieren(self.p, csv_text([0, 1, 2, 3, 10, 11]))  # B4, B5 fallen weg; B10, B11 sind neu
        self.assertEqual((prot.neu, prot.geloescht, prot.unveraendert, prot.fehler), (2, 2, 4, 0))
        self.assertEqual(self.nummern(), ["B0", "B1", "B10", "B11", "B2", "B3"])
        self.assertEqual(self.nummern(self.anderer), ["B0"])  # andere Lieferanten bleiben unberührt
        self.assertIn("2 entfernt", prot.meldung)

    def test_verknuepfter_lieferantenpreis_und_belegverweise(self):
        st = Steuersatz.objects.get(nummer=1)
        eigen = Artikel.objects.create(nummer="EIGEN", bezeichnung="Eigener", herstellernummer="H3", steuersatz=st)
        preislisten.importieren(self.p, csv_text(range(5)))
        self.assertEqual(ArtikelLieferant.objects.get().artikel, eigen)  # B3 ist verknüpft
        m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        kunde = Kunde.objects.create(nummer="1", name1="K")
        beleg = Beleg.objects.create(mandant=m, art=Belegart.ANGEBOT, kunde=kunde)
        eintrag = Preislistenartikel.objects.get(bestellnummer="B4")
        pos = Position.objects.create(beleg=beleg, typ="P", preisliste_artikel=eintrag, bezeichnung="Artikel 4", einzelpreis=Decimal("12"))
        best = services.bestellung_anlegen(m, self.lf)
        bp = services.bestellposition(best, None, Decimal("1"), preisliste_artikel=eintrag)
        preislisten.importieren(self.p, csv_text([0, 1, 2]))  # B3 und B4 fallen weg
        self.assertEqual(self.nummern(), ["B0", "B1", "B2"])
        self.assertEqual(ArtikelLieferant.objects.count(), 0)  # Lieferantenpreis am eigenen Artikel mit entfernt
        self.assertTrue(Artikel.objects.filter(pk=eigen.pk).exists())  # der eigene Artikel selbst bleibt
        pos.refresh_from_db()
        bp.refresh_from_db()
        self.assertEqual((pos.preisliste_artikel, pos.bezeichnung, pos.einzelpreis), (None, "Artikel 4", Decimal("12.00")))  # Beleg bleibt erhalten
        self.assertEqual((bp.preisliste_artikel, bp.bezeichnung), (None, "Artikel 4"))

    def test_fehlerhafte_zeilen_werden_nicht_als_veraltet_gelöscht(self):
        preislisten.importieren(self.p, csv_text(range(4)))
        text = csv_text([0, 1]).decode() + "\nB2;H2;Artikel 2;Fabrikat;kaputt;3"  # B2 steht noch in der Liste, EK nicht lesbar
        prot = preislisten.importieren(self.p, text.encode())
        self.assertEqual((prot.fehler, prot.geloescht), (1, 1))  # nur B3 wird entfernt
        self.assertEqual(self.nummern(), ["B0", "B1", "B2"])

    def test_schalter_aus_behaelt_alles(self):
        Preislistenprofil.objects.filter(pk=self.p.pk).update(entfernen_veraltete=False)
        self.p.refresh_from_db()
        preislisten.importieren(self.p, csv_text(range(5)))
        prot = preislisten.importieren(self.p, csv_text([0]))
        self.assertEqual((prot.geloescht, len(self.nummern())), (0, 5))

    def test_schutz_bei_unvollstaendiger_liste(self):
        preislisten.importieren(self.p, csv_text(range(200)))
        prot = preislisten.importieren(self.p, csv_text(range(60)))  # nur 30 % der bisherigen Artikel: wohl abgeschnitten
        self.assertEqual(prot.geloescht, 0)
        self.assertIn("NICHT entfernt", prot.meldung)
        self.assertEqual(len(self.nummern()), 200)
        prot2 = preislisten.importieren(self.p, csv_text(range(120)))  # 60 % genügen
        self.assertEqual((prot2.geloescht, len(self.nummern())), (80, 120))

    def test_vorschau_nennt_anzahl(self):
        preislisten.importieren(self.p, csv_text(range(8)))
        v = preislisten.vorschau(self.p, csv_text(range(5)))
        self.assertEqual(v["veraltet"], 3)
        self.assertEqual(len(self.nummern()), 8)  # Vorschau löscht nichts
