"""Entscheidungen aus dem Prüfbericht 14.11.2026, Abschnitt C (Einkauf): Bestellvorschlag zählt Entwürfe mit,
Preislistenimport verknüpft nur bei passendem Hersteller oder EAN, Beträge werden kaufmännisch gerundet."""
from decimal import Decimal

from django.test import TestCase

from apps.kern.models import Mandant
from apps.lager.models import Bewegungsart
from apps.lager.services import buche_manuell
from apps.stammdaten.models import Artikel, ArtikelLieferant, ArtikelMandant, Hersteller, Lieferant, Preislistenartikel, Steuersatz

from . import preislisten, services
from .models import BestellPosition, BestellStatus, Bestellung, Lieferantenrechnung, Wareneingang, WareneingangPosition
from .test_preislisten import profil

D = Decimal


class VorschlagEntwurfTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.lf = Lieferant.objects.create(nummer="L1", name1="Techdata GmbH")
        st = Steuersatz.objects.get(nummer=1)
        self.maus = Artikel.objects.create(nummer="M1", bezeichnung="Maus", steuersatz=st, vk1=D("15"))
        ArtikelLieferant.objects.create(artikel=self.maus, lieferant=self.lf, bestellnummer="TD-1", ek=D("9"))
        ArtikelMandant.objects.create(artikel=self.maus, mandant=self.m, mindestbestand=10)
        buche_manuell(self.m, self.maus, Bewegungsart.WARENEINGANG, D("4"))

    def test_zweiter_klick_legt_nichts_doppelt_an(self):
        v = services.bestellvorschlag(self.m)
        self.assertEqual(v[0]["menge"], D("6"))
        liste = services.bestellungen_aus_vorschlag(self.m, [(self.maus.pk, v[0]["menge"])])
        self.assertEqual(liste[0].status, BestellStatus.ENTWURF)
        self.assertEqual(services.bestellvorschlag(self.m), [])  # Entwurf zählt als bestellt

    def test_teilweise_gedeckt_schlaegt_nur_den_rest_vor(self):
        b = services.bestellung_anlegen(self.m, self.lf)
        services.bestellposition(b, self.maus, D("2"))
        self.assertEqual(services.bestellvorschlag(self.m)[0]["menge"], D("4"))

    def test_stornierte_bestellung_zaehlt_nicht(self):
        b = services.bestellung_anlegen(self.m, self.lf)
        services.bestellposition(b, self.maus, D("6"))
        self.assertEqual(services.bestellvorschlag(self.m), [])
        b.status = BestellStatus.STORNIERT
        b.save()
        self.assertEqual(services.bestellvorschlag(self.m)[0]["menge"], D("6"))

    def test_entwurf_anderer_mandant_zaehlt_nicht(self):
        fremd = Mandant.objects.create(kurzname="ZONE", firmenname="ZONE Handel")
        b = services.bestellung_anlegen(fremd, self.lf)
        services.bestellposition(b, self.maus, D("6"))
        self.assertEqual(services.bestellvorschlag(self.m)[0]["menge"], D("6"))

    def test_anzeige_offen_bestellt_bleibt_ohne_entwurf(self):
        b = services.bestellung_anlegen(self.m, self.lf)
        services.bestellposition(b, self.maus, D("3"))
        self.assertEqual(services.offen_bestellt(self.m, self.maus), D("0"))
        self.assertEqual(services.offen_bestellt(self.m, self.maus, mit_entwurf=True), D("3"))


class PreislisteVerknuepfungTest(TestCase):
    def setUp(self):
        self.lf = Lieferant.objects.create(nummer="L1", name1="Techdata GmbH", kurzname="Techdata")
        st = Steuersatz.objects.get(nummer=1)
        self.logitech = Hersteller.objects.create(name="Logitech")
        self.maus = Artikel.objects.create(nummer="M1", bezeichnung="Maus", herstellernummer="MX-100", ean="4012345678901",
                                           hersteller=self.logitech, steuersatz=st)

    def importieren(self, zeile):
        kopf = "Best.-Nr.;Herst.-Nr.;EAN;Bezeichnung;Hersteller;EK;Bestand;Lieferzeit\n"
        preislisten.importieren(profil(self.lf), (kopf + zeile + "\n").encode())
        return Preislistenartikel.objects.get(lieferant=self.lf, bestellnummer="X-1")

    def test_herstellernummer_und_hersteller_verknuepft(self):
        self.assertEqual(self.importieren("X-1;MX-100;;Maus;Logitech;12,50;1;1").artikel, self.maus)

    def test_herstellernummer_und_ean_verknuepft(self):
        self.assertEqual(self.importieren("X-1;MX-100;4012345678901;Maus;;12,50;1;1").artikel, self.maus)

    def test_nur_ean_verknuepft(self):
        self.assertEqual(self.importieren("X-1;;4012345678901;Maus;;12,50;1;1").artikel, self.maus)

    def test_gleiche_herstellernummer_anderer_hersteller_nicht_verknuepft(self):
        self.assertIsNone(self.importieren("X-1;MX-100;4099999999999;Fremdmaus;Razer;12,50;1;1").artikel)

    def test_nur_herstellernummer_ohne_hersteller_und_ean_nicht_verknuepft(self):
        self.assertIsNone(self.importieren("X-1;MX-100;;Maus;;12,50;1;1").artikel)
        self.assertEqual(ArtikelLieferant.objects.count(), 0)

    def test_zweiter_kandidat_mit_passendem_hersteller_wird_gefunden(self):
        st = Steuersatz.objects.get(nummer=1)
        razer = Hersteller.objects.create(name="Razer")
        zweite = Artikel.objects.create(nummer="M2", bezeichnung="Razer Maus", herstellernummer="MX-100", hersteller=razer, steuersatz=st)
        self.assertEqual(self.importieren("X-1;MX-100;;Maus;razer;12,50;1;1").artikel, zweite)

    def test_bestehende_verknuepfung_bleibt_beim_naechsten_import(self):
        e = self.importieren("X-1;MX-100;;Maus;Logitech;12,50;1;1")
        self.assertEqual(e.artikel, self.maus)
        e = self.importieren("X-1;MX-100;;Maus;;13,00;1;1")  # Hersteller/EAN fehlen jetzt: alte Verknüpfung wird nicht gelöscht
        self.assertEqual(e.artikel, self.maus)

    def test_vorschau_nutzt_dieselbe_zuordnung(self):
        self.assertEqual(preislisten.artikel_finden({"herstellernummer": "MX-100", "hersteller": "Logitech"}), self.maus)
        self.assertIsNone(preislisten.artikel_finden({"herstellernummer": "MX-100", "hersteller": "Razer"}))
        self.assertIsNone(preislisten.artikel_finden({"herstellernummer": "MX-100"}))
        self.assertEqual(preislisten.artikel_finden({"ean": "4012345678901"}), self.maus)


class RundungTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.lf = Lieferant.objects.create(nummer="L1", name1="Techdata GmbH")
        self.st = Steuersatz.objects.get(nummer=1)
        self.a = Artikel.objects.create(nummer="M1", bezeichnung="Maus", steuersatz=self.st)

    def test_positionssumme_halber_cent_rundet_auf(self):
        b = services.bestellung_anlegen(self.m, self.lf)
        p = BestellPosition(bestellung=b, artikel=self.a, menge=D("1"), ek=D("0.0250"))  # 0,025 -> 0,03 (vorher 0,02)
        self.assertEqual(p.netto, D("0.03"))
        p2 = BestellPosition(bestellung=b, artikel=self.a, menge=D("1"), ek=D("0.0350"))  # 0,035 -> 0,04
        self.assertEqual(p2.netto, D("0.04"))

    def test_normale_rundung_unveraendert(self):
        p = BestellPosition(menge=D("3"), ek=D("1.2340"))
        self.assertEqual(p.netto, D("3.70"))

    def test_wareneingangsposition(self):
        we = Wareneingang(mandant=self.m, lieferant=self.lf)
        self.assertEqual(WareneingangPosition(wareneingang=we, artikel=self.a, menge=D("1"), ek=D("0.0250")).netto, D("0.03"))

    def test_skonto_zahlbetrag(self):
        from datetime import date
        r = Lieferantenrechnung(mandant=self.m, lieferant=self.lf, netto=D("1"), brutto=D("10.25"), skonto_prozent=D("2"),
                                skonto_bis=date(2099, 1, 1))
        # 10,25 * 0,98 = 10,045 -> 10,05 (vorher 10,04)
        self.assertEqual(r.zahlbetrag(am=date(2026, 1, 1)), D("10.05"))
        self.assertEqual(r.zahlbetrag(am=date(2100, 1, 1)), D("10.25"))  # Skontofrist vorbei: voller Betrag

    def test_import_ek_rundet_kaufmaennisch(self):
        kopf = "Best.-Nr.;Herst.-Nr.;EAN;Bezeichnung;Hersteller;EK;Bestand;Lieferzeit\n"
        preislisten.importieren(profil(self.lf), (kopf + "X-1;;;Maus;;1,00005;1;1\n").encode())
        self.assertEqual(Preislistenartikel.objects.get(bestellnummer="X-1").ek, D("1.0001"))
