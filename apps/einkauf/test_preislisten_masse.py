from decimal import Decimal

from django.test import TestCase

from apps.stammdaten.kopie import neuer_artikel_aus_preisliste
from apps.stammdaten.models import Artikel, Lieferant, Preislistenartikel, Steuersatz

from . import assistent, preislisten
from .models import Preislistenprofil

D = Decimal

CSV = (
    "Best.-Nr.;Herst.-Nr.;Bezeichnung;EK;Gewicht;Länge;Breite;Höhe\n"
    "TD-1;MX-100;Maus;12,50;450 g;120 mm;6,5;0,04 m\n"
    "TD-2;;Tastatur;20,00;1,2 kg;;;\n"
    "TD-3;;Kabel;1,00;abc;0;;\n"
)


def profil(lieferant, **kw):
    daten = dict(lieferant=lieferant, name="Masse", dateityp="csv", zeichensatz="utf-8", trennzeichen=";", dezimaltrennzeichen=",",
                 sp_bestellnummer="Best.-Nr.", sp_herstellernummer="Herst.-Nr.", sp_bezeichnung="Bezeichnung", sp_ek="EK",
                 sp_gewicht="Gewicht", sp_laenge="Länge", sp_breite="Breite", sp_hoehe="Höhe")
    daten.update(kw)
    return Preislistenprofil.objects.create(**daten)


class EinheitenTest(TestCase):
    def test_gewicht_wird_in_kg_normalisiert(self):
        self.assertEqual(preislisten.mass("450 g", "gewicht", ","), D("0.450"))
        self.assertEqual(preislisten.mass("1,2 kg", "gewicht", ","), D("1.200"))
        self.assertEqual(preislisten.mass("0,5", "gewicht", ","), D("0.500"))  # ohne Einheit: kg
        self.assertEqual(preislisten.mass("1.5", "gewicht", "."), D("1.500"))

    def test_masse_werden_in_cm_normalisiert(self):
        self.assertEqual(preislisten.mass("120 mm", "laenge", ","), D("12.00"))
        self.assertEqual(preislisten.mass("0,3 m", "breite", ","), D("30.00"))
        self.assertEqual(preislisten.mass("6,5", "hoehe", ","), D("6.50"))
        self.assertEqual(preislisten.mass("6,5cm", "hoehe", ","), D("6.50"))

    def test_leer_unlesbar_oder_null_ist_none(self):
        for wert in ("", "abc", "0", "5 zoll", "-3"):
            self.assertIsNone(preislisten.mass(wert, "laenge", ","), wert)


class MasseImportTest(TestCase):
    def setUp(self):
        self.lf = Lieferant.objects.create(nummer="L1", name1="Techdata GmbH", kurzname="Techdata")
        self.st = Steuersatz.objects.get(nummer=1)
        self.maus = Artikel.objects.create(nummer="M1", bezeichnung="Maus", herstellernummer="MX-100", steuersatz=self.st)

    def test_spaltenzuordnung_und_vorschlag(self):
        self.assertEqual(assistent.spalten_raten(["Best.-Nr.", "Länge", "Breite", "Höhe", "Gewicht"]).keys() & {"laenge", "breite", "hoehe", "gewicht"},
                         {"laenge", "breite", "hoehe", "gewicht"})
        self.assertIn("laenge", preislisten.FELDER)

    def test_import_fuellt_preislistenartikel_und_aktualisiert_bei_jedem_lauf(self):
        p = profil(self.lf)
        preislisten.importieren(p, CSV.encode())
        e = Preislistenartikel.objects.get(bestellnummer="TD-1")
        self.assertEqual((e.gewicht_kg, e.laenge_cm, e.breite_cm, e.hoehe_cm), (D("0.450"), D("12.00"), D("6.50"), D("4.00")))
        kein = Preislistenartikel.objects.get(bestellnummer="TD-3")  # unlesbar/0: Preis kommt trotzdem an
        self.assertEqual((kein.ek, kein.gewicht_kg, kein.laenge_cm), (D("1.0000"), None, None))
        prot = preislisten.importieren(p, CSV.replace("450 g", "500 g").encode())
        self.assertEqual(prot.geaendert, 1)
        self.assertEqual(Preislistenartikel.objects.get(bestellnummer="TD-1").gewicht_kg, D("0.500"))

    def test_profil_ohne_massspalten_laeuft_unveraendert(self):
        p = profil(self.lf, sp_gewicht="", sp_laenge="", sp_breite="", sp_hoehe="")
        prot = preislisten.importieren(p, CSV.encode())
        self.assertEqual((prot.neu, prot.fehler), (3, 0))
        e = Preislistenartikel.objects.get(bestellnummer="TD-1")
        self.assertEqual((e.gewicht_kg, e.laenge_cm, e.breite_cm, e.hoehe_cm), (None, None, None, None))
        self.maus.refresh_from_db()
        self.assertIsNone(self.maus.gewicht_kg)

    def test_nur_leere_felder_am_artikel_werden_gefuellt(self):
        self.maus.gewicht_kg, self.maus.hoehe_cm = D("0.100"), D("9.00")  # manuell gepflegt
        self.maus.save()
        preislisten.importieren(profil(self.lf), CSV.encode())
        self.maus.refresh_from_db()
        self.assertEqual((self.maus.gewicht_kg, self.maus.hoehe_cm), (D("0.100"), D("9.00")))  # bleiben
        self.assertEqual((self.maus.laenge_cm, self.maus.breite_cm), (D("12.00"), D("6.50")))  # waren leer

    def test_ek_rangfolge_unveraendert_import_ek_maßgebend(self):
        self.maus.ek_manuell = D("20")
        self.maus.save()
        preislisten.importieren(profil(self.lf), CSV.encode())
        self.maus.refresh_from_db()
        self.assertEqual(self.maus.ek_herkunft, "import")
        self.assertEqual(self.maus.lieferanten.get().ek, D("12.5000"))

    def test_kopie_aus_preislistenzeile_uebernimmt_masse(self):
        preislisten.importieren(profil(self.lf), CSV.encode())
        neu = neuer_artikel_aus_preisliste(Preislistenartikel.objects.get(bestellnummer="TD-1"))
        self.assertEqual((neu.gewicht_kg, neu.laenge_cm, neu.breite_cm, neu.hoehe_cm), (D("0.450"), D("12.00"), D("6.50"), D("4.00")))
