"""Produktivstart (Entscheidung des Nutzers 25.11.2026): Probedaten löschen, Einstellungen und Lieferanten mit Preislisten-Profil
behalten, Belegnummern auf Anfang."""
import gzip
import io
import os
import tempfile
from datetime import date
from decimal import Decimal as D

from django.core.management import CommandError, call_command
from django.test import TestCase

from apps.einkauf import services as einkauf
from apps.einkauf.models import Lieferantenrechnung, Preislistenprofil, Wareneingang
from apps.finanzen import services as finanzen
from apps.finanzen.models import Zahlung
from apps.kern import produktivstart as ps
from apps.kern.models import AenderungsProtokoll, Mandant
from apps.lager.models import Lagerbewegung
from apps.stammdaten.models import (Ansprechpartner, Artikel, Hersteller, Kategorie, Kunde, Lieferant, Stammnummer, Steuersatz,
                                    Zahlungsbedingung)
from apps.verkauf.models import Beleg, Belegart, Nummernkreis, Position


class ProduktivstartTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer", iban="DE02120300000000202051", ust_id="DE1")
        self.zb = Zahlungsbedingung.objects.create(bezeichnung="sofort", tage_netto=0)
        self.kat = Kategorie.objects.create(name="Netzteile")
        self.herst = Hersteller.objects.create(name="be quiet!")
        self.mit_profil = Lieferant.objects.create(nummer="70016", name1="KOSATEC", iban="DE89370400440532013000")
        self.ohne_profil = Lieferant.objects.create(nummer="70272", name1="api GmbH")
        Ansprechpartner.objects.create(lieferant=self.mit_profil, mandant=self.m, nachname="Schulz")
        Ansprechpartner.objects.create(lieferant=self.ohne_profil, mandant=self.m, nachname="Weg")
        self.profil = Preislistenprofil.objects.create(lieferant=self.mit_profil, name="Kosatec CSV", quelle="ftp",
                                                       adresse="ftp.kosatec.test", benutzer="okay")
        self.kunde = Kunde.objects.create(nummer="40001", name1="Test GmbH", strasse="a", plz="1", ort="b")
        Ansprechpartner.objects.create(kunde=self.kunde, nachname="Kunde")
        self.artikel = Artikel.objects.create(nummer="A1", bezeichnung="Netzteil", steuersatz=Steuersatz.objects.get(nummer=1),
                                              vk1=D("80"), kategorie=self.kat, hersteller=self.herst)
        Stammnummer.objects.update_or_create(art="K", defaults={"letzte_nummer": 40667})
        # Bewegungsdaten: Wareneingang (Lager), Rechnung mit Zahlung, Lieferantenrechnung
        we = einkauf.wareneingang_anlegen(self.m, self.mit_profil)
        einkauf.wareneingang_erfassen(we, self.artikel, D("5"), D("50"))
        einkauf.wareneingang_buchen(we, bestaetigt=True)
        einkauf.rechnung_erfassen(self.m, self.mit_profil, "R-1", date.today(), D("250"), D("297.50"), wareneingaenge=[we])
        b = Beleg.objects.create(mandant=self.m, art=Belegart.RECHNUNG, kunde=self.kunde, zahlungsbedingung=self.zb)
        Position.objects.create(beleg=b, artikel=self.artikel, bezeichnung="Netzteil", menge=1, einzelpreis=D("80"))
        b.abschliessen()
        finanzen.zahlung_buchen(b, D("95.20"), date.today(), "bank")

    def test_jedes_modell_ist_zugeordnet(self):
        self.assertEqual(ps.unklassifiziert(), [])  # neue Tabelle? → in BEHALTEN, LOESCHEN oder TEILWEISE eintragen
        self.assertFalse(ps.BEHALTEN & ps.LOESCHEN)

    def test_ohne_bestaetigung_passiert_nichts(self):
        with self.assertRaisesMessage(ValueError, "PRODUKTIVSTART"):
            ps.ausfuehren("ja")
        self.assertTrue(Beleg.objects.exists())

    def test_loescht_probedaten_und_behaelt_einstellungen(self):
        self.assertTrue(AenderungsProtokoll.objects.exists())
        ps.ausfuehren(ps.BESTAETIGUNG)
        for modell in (Beleg, Position, Zahlung, Lagerbewegung, Wareneingang, Lieferantenrechnung, Artikel, Kunde, Nummernkreis,
                       AenderungsProtokoll):
            with self.subTest(modell.__name__):
                self.assertFalse(modell.objects.exists())
        # bleibt
        self.assertTrue(Mandant.objects.filter(pk=self.m.pk, iban="DE02120300000000202051").exists())
        self.assertEqual(list(Lieferant.objects.all()), [self.mit_profil])
        self.assertEqual(list(Ansprechpartner.objects.values_list("nachname", flat=True)), ["Schulz"])
        self.assertTrue(Preislistenprofil.objects.filter(pk=self.profil.pk, adresse="ftp.kosatec.test").exists())
        for modell, pk in ((Kategorie, self.kat.pk), (Hersteller, self.herst.pk), (Zahlungsbedingung, self.zb.pk)):
            self.assertTrue(modell.objects.filter(pk=pk).exists())
        self.assertEqual(Stammnummer.objects.get(art="K").letzte_nummer, 40667)  # stellt der Nutzer selbst ein

    def test_danach_beginnen_die_belegnummern_bei_eins_und_sind_frei_einstellbar(self):
        ps.ausfuehren(ps.BESTAETIGUNG)
        k = Kunde.objects.create(nummer="1", name1="Neu", strasse="a", plz="1", ort="b")
        b = Beleg.objects.create(mandant=self.m, art=Belegart.RECHNUNG, kunde=k)
        Position.objects.create(beleg=b, bezeichnung="x", menge=1, einzelpreis=D("1"))
        b.abschliessen()
        b.refresh_from_db()
        self.assertTrue(b.nummer.endswith("-00001"))
        Nummernkreis.naechste_setzen(self.m, "AN", 500)  # andere Kreise frei einstellbar
        self.assertEqual(Nummernkreis.objects.get(mandant=self.m, art="AN").letzte_nummer, 499)

    def test_befehl_vorschau_loescht_nichts_und_ausfuehren_braucht_bestaetigung(self):
        aus = io.StringIO()
        call_command("produktivstart", stdout=aus)
        self.assertIn("Nur Vorschau", aus.getvalue())
        self.assertIn("verkauf.Beleg", aus.getvalue())
        self.assertTrue(Beleg.objects.exists())
        with self.assertRaisesMessage(CommandError, "Bestätigung fehlt"):
            call_command("produktivstart", "--ausfuehren", stdout=io.StringIO())
        self.assertTrue(Beleg.objects.exists())

    def test_befehl_sichert_vorher(self):
        with tempfile.TemporaryDirectory() as ordner:
            call_command("produktivstart", "--ausfuehren", "--bestaetigung", "PRODUKTIVSTART", "--sicherung", ordner, stdout=io.StringIO())
            dateien = os.listdir(ordner)
            self.assertEqual(len(dateien), 1)
            with gzip.open(os.path.join(ordner, dateien[0]), "rt", encoding="utf-8") as datei:
                inhalt = datei.read()
            self.assertIn('"model": "verkauf.beleg"', inhalt)  # Sicherung enthält die gelöschten Belege
        self.assertFalse(Beleg.objects.exists())
