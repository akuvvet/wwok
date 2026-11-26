"""Entscheidungen aus dem Prüfbericht (Abschnitt C): Nummern bleiben verbraucht, Käuferanschrift am Beleg, Kundensperre."""
import xml.etree.ElementTree as ET
from datetime import date
from decimal import Decimal

from django.test import TestCase

from apps.kern.models import Mandant
from apps.stammdaten.models import Anschriftart, Kundenanschrift, Artikel, Kunde, Steuersatz, Zahlungsbedingung

from .models import Abo, AboPosition, Beleg, Belegart, Nummernkreis, Position
from .zugferd import ERechnungAusgangFehler, xml_erzeugen

RAM = "{urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100}"


class Basis(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer", strasse="Weg 1", plz="42651", ort="Solingen",
                                        ust_id="DE123456789", iban="DE02120300000000202051", email="info@okay.test")
        self.kunde = Kunde.objects.create(nummer="1", name1="Muster GmbH", strasse="Str. 2", plz="10115", ort="Berlin")
        self.st = Steuersatz.objects.create(bezeichnung="19", prozent=Decimal("19"))

    def bestellung(self, kunde=None):
        b = Beleg.objects.create(mandant=self.m, art=Belegart.AUFTRAG, kunde=kunde or self.kunde)
        Position.objects.create(beleg=b, typ="L", bezeichnung="Dienst", menge=1, einzelpreis=Decimal("10"))
        return b


class NummernVerbrauchtTest(Basis):
    def test_geloeschte_bestellung_gibt_ihre_nummer_nicht_frei(self):
        b1 = self.bestellung()
        b2 = self.bestellung()
        nummer2 = b2.nummer
        b2.delete()
        self.assertEqual(Nummernkreis.objects.get(mandant=self.m, art="AB").hoechste_vergeben, 2)
        self.assertNotIn(nummer2, Nummernkreis.vergebene(self.m, "AB"))  # aus den Belegen allein wäre sie wieder frei
        with self.assertRaisesMessage(ValueError, "höchste vergebene (2)"):
            Nummernkreis.naechste_setzen(self.m, "AB", 2)
        neu = self.bestellung()
        self.assertNotEqual(neu.nummer, nummer2)
        self.assertNotEqual(neu.nummer, b1.nummer)

    def test_geloeschtes_angebot_mit_folgebeleg_bleibt_verbraucht(self):
        an = Beleg.objects.create(mandant=self.m, art=Belegart.ANGEBOT, kunde=self.kunde)
        Position.objects.create(beleg=an, typ="L", bezeichnung="Dienst", menge=1, einzelpreis=Decimal("10"))
        an.kopieren_als(Belegart.AUFTRAG)  # Kopie: Folgebeleg, das Angebot bleibt löschbar
        alt = an.nummer
        an.delete()
        with self.assertRaises(ValueError):
            Nummernkreis.naechste_setzen(self.m, "AN", 1)
        self.assertNotEqual(Beleg.objects.create(mandant=self.m, art=Belegart.ANGEBOT, kunde=self.kunde).nummer, alt)

    def test_zaehler_darf_weiter_nach_oben_aber_nie_unter_den_merker(self):
        Nummernkreis.naechste(self.m, "LS", 2026)
        Nummernkreis.naechste_setzen(self.m, "LS", 50)
        self.assertEqual(Nummernkreis.naechste(self.m, "LS", 2026), "LS2026-00050")
        with self.assertRaises(ValueError):
            Nummernkreis.naechste_setzen(self.m, "LS", 50)

    def test_vorbelegung_der_migration(self):
        import importlib
        from django.apps import apps as django_apps
        migration = importlib.import_module("apps.verkauf.migrations.0037_kaeuferanschrift_und_nummernmerker")
        Beleg.objects.bulk_create([Beleg(mandant=self.m, art="AN", kunde=self.kunde, nummer="AN2026-00007")])
        Nummernkreis.objects.create(mandant=self.m, art="AN", letzte_nummer=3)
        migration.merker_vorbelegen(django_apps, None)
        self.assertEqual(Nummernkreis.objects.get(mandant=self.m, art="AN").hoechste_vergeben, 7)
        self.assertEqual(Nummernkreis.objects.get(mandant=self.m, art="AN").letzte_nummer, 3)  # der Zähler selbst bleibt


class KaeuferanschriftTest(Basis):
    def setUp(self):
        super().setUp()
        Artikel.objects.create(nummer="A1", bezeichnung="Maus", steuersatz=self.st, vk1=Decimal("10"))
        self.zb = Zahlungsbedingung.objects.create(bezeichnung="14 Tage", tage_netto=14, druck_text="Zahlbar in 14 Tagen")

    def rechnung(self, anschrift):
        b = Beleg.objects.create(mandant=self.m, art=Belegart.RECHNUNG, kunde=self.kunde, zahlungsbedingung=self.zb,
                                 rechnungsanschrift=anschrift)
        Position.objects.create(beleg=b, typ="L", bezeichnung="Dienst", menge=1, einzelpreis=Decimal("10"))
        return b

    def kaeufer_xml(self, b):
        w = ET.fromstring(xml_erzeugen(b))
        partei = w.find(f".//{RAM}BuyerTradeParty")
        adresse = partei.find(RAM + "PostalTradeAddress")
        return (partei.find(RAM + "Name").text, adresse.find(RAM + "LineOne").text, adresse.find(RAM + "PostcodeCode").text,
                adresse.find(RAM + "CityName").text, adresse.find(RAM + "CountryID").text)

    def test_felder_werden_aus_der_rechnungsanschrift_gefuellt(self):
        b = self.rechnung("Abweichende GmbH\nz. H. Frau Nord\nHafenweg 5\n20457 Hamburg")
        self.assertEqual((b.kaeufer_name, b.kaeufer_strasse, b.kaeufer_plz, b.kaeufer_ort, b.kaeufer_land),
                         ("Abweichende GmbH z. H. Frau Nord", "Hafenweg 5", "20457", "Hamburg", "DE"))

    def test_land_wird_erkannt(self):
        b = self.rechnung("Firma AT\nRingstraße 1\n1010 Wien\nAT")
        self.assertEqual((b.kaeufer_ort, b.kaeufer_land), ("Wien", "AT"))

    def test_e_rechnung_nimmt_die_anschrift_des_belegs_nicht_des_kunden(self):
        b = self.rechnung("Abweichende GmbH\nHafenweg 5\n20457 Hamburg")
        b.abschliessen()
        b.refresh_from_db()
        Kunde.objects.filter(pk=self.kunde.pk).update(strasse="Neue Str. 9", plz="80331", ort="München")  # Stammdaten ändern sich später
        self.assertEqual(self.kaeufer_xml(b), ("Abweichende GmbH", "Hafenweg 5", "20457", "Hamburg", "DE"))

    def test_bearbeitete_anschrift_wird_beim_abschluss_uebernommen(self):
        b = self.rechnung("Alt GmbH\nAlte Str. 1\n10115 Berlin")
        Beleg.objects.filter(pk=b.pk).update(rechnungsanschrift="Neu GmbH\nNeue Str. 2\n50667 Köln")
        b.refresh_from_db()
        b.abschliessen()
        b.refresh_from_db()
        self.assertEqual((b.kaeufer_name, b.kaeufer_ort), ("Neu GmbH", "Köln"))

    def test_altbeleg_ohne_felder_faellt_auf_kundenstamm_zurueck(self):
        b = self.rechnung("")
        b.abschliessen()
        Beleg.objects.filter(pk=b.pk).update(kaeufer_name="", kaeufer_strasse="", kaeufer_plz="", kaeufer_ort="", kaeufer_land="")
        b = Beleg.objects.get(pk=b.pk)
        self.assertEqual(self.kaeufer_xml(b), ("Muster GmbH", "Str. 2", "10115", "Berlin", "DE"))

    def test_unzerlegbarer_text_faellt_auf_rechnungsanschrift_des_kunden_zurueck(self):
        Kundenanschrift.objects.create(kunde=self.kunde, art=Anschriftart.RECHNUNG, name1="Buchhaltung Muster", strasse="Konto 3",
                                 plz="04109", ort="Leipzig", standard=True)
        b = self.rechnung("nur eine Zeile")
        self.assertEqual((b.kaeufer_name, b.kaeufer_ort), ("Buchhaltung Muster", "Leipzig"))

    def test_unvollstaendige_anschrift_gibt_fehler(self):
        Kunde.objects.filter(pk=self.kunde.pk).update(strasse="", plz="", ort="")
        self.kunde.refresh_from_db()
        b = self.rechnung("")
        b.abschliessen()
        b.refresh_from_db()
        with self.assertRaisesMessage(ERechnungAusgangFehler, "Anschrift unvollständig"):
            xml_erzeugen(b)

    def test_abgeschlossener_beleg_bleibt_unveraendert(self):
        from .models import BelegGesperrt
        b = self.rechnung("Abweichende GmbH\nHafenweg 5\n20457 Hamburg")
        b.abschliessen()
        b.refresh_from_db()
        b.kunde_id = Kunde.objects.create(nummer="2", name1="Anderer").pk
        with self.assertRaises(BelegGesperrt):
            b.save()


class KundensperreTest(Basis):
    def sperren(self):
        Kunde.objects.filter(pk=self.kunde.pk).update(gesperrt=True)

    def test_kopieren_als_neuer_vorgang_ist_gesperrt(self):
        re = Beleg.objects.create(mandant=self.m, art=Belegart.ANGEBOT, kunde=self.kunde)
        Position.objects.create(beleg=re, typ="L", bezeichnung="Dienst", menge=1, einzelpreis=Decimal("10"))
        self.sperren()
        vorher = Beleg.objects.count()
        with self.assertRaisesMessage(ValueError, "gesperrt"):
            re.kopieren_als(Belegart.ANGEBOT)
        with self.assertRaisesMessage(ValueError, "gesperrt"):
            re.kopieren_als(Belegart.RECHNUNG)
        self.assertEqual(Beleg.objects.count(), vorher)

    def test_kopieren_als_ohne_sperre_funktioniert(self):
        an = Beleg.objects.create(mandant=self.m, art=Belegart.ANGEBOT, kunde=self.kunde)
        Position.objects.create(beleg=an, typ="L", bezeichnung="Dienst", menge=1, einzelpreis=Decimal("10"))
        self.assertEqual(an.kopieren_als(Belegart.ANGEBOT).art, Belegart.ANGEBOT)

    def test_laufender_vorgang_darf_weitergefuehrt_werden(self):
        ab = self.bestellung()
        self.sperren()
        ls = ab.kopieren_als(Belegart.LIEFERSCHEIN)
        self.assertEqual(ls.art, Belegart.LIEFERSCHEIN)

    def test_gutschrift_bleibt_erlaubt(self):
        Artikel.objects.create(nummer="A1", bezeichnung="Maus", steuersatz=self.st, vk1=Decimal("10"))
        re = Beleg.objects.create(mandant=self.m, art=Belegart.RECHNUNG, kunde=self.kunde)
        Position.objects.create(beleg=re, typ="L", bezeichnung="Dienst", menge=1, einzelpreis=Decimal("10"))
        re.abschliessen()
        self.sperren()
        self.assertEqual(re.gutschrift_erzeugen().art, Belegart.GUTSCHRIFT)

    def abo(self):
        abo = Abo.objects.create(mandant=self.m, kunde=self.kunde, bezeichnung="Wartung", monate=3, naechster_termin=date(2026, 10, 1))
        AboPosition.objects.create(abo=abo, bezeichnung="Pauschale", menge=1, einzelpreis=Decimal("100"))
        return abo

    def test_abo_bei_gesperrtem_kunden_fehler_und_termin_bleibt(self):
        abo = self.abo()
        self.sperren()
        with self.assertRaisesMessage(ValueError, "ist gesperrt"):
            abo.rechnung_erzeugen(date(2026, 10, 1))
        abo.refresh_from_db()
        self.assertEqual(abo.naechster_termin, date(2026, 10, 1))
        self.assertFalse(Beleg.objects.filter(abo=abo).exists())

    def test_abo_nach_entsperren_laeuft_wieder(self):
        abo = self.abo()
        self.sperren()
        with self.assertRaises(ValueError):
            abo.rechnung_erzeugen(date(2026, 10, 1))
        Kunde.objects.filter(pk=self.kunde.pk).update(gesperrt=False)
        abo = Abo.objects.get(pk=abo.pk)
        self.assertEqual(abo.rechnung_erzeugen(date(2026, 10, 1)).art, Belegart.AUFTRAG)
        abo.refresh_from_db()
        self.assertEqual(abo.naechster_termin, date(2027, 1, 1))


class RechnungsdatumTest(Basis):
    """Punkt 5: Jede Rechnung trägt das Datum des Abschlusses, auch wenn sie früher angelegt wurde (Werkstatt, wiederkehrend)."""

    def beleg(self, art, datum):
        b = Beleg.objects.create(mandant=self.m, art=art, kunde=self.kunde, belegdatum=datum)
        Position.objects.create(beleg=b, typ="L", bezeichnung="Dienst", menge=1, einzelpreis=Decimal("10"))
        return b

    def test_frueher_angelegte_rechnung_bekommt_das_tagesdatum(self):
        from datetime import timedelta
        from django.utils import timezone
        heute = timezone.localdate()
        r = self.beleg(Belegart.RECHNUNG, heute - timedelta(days=9))
        self.assertFalse(r.vorlaeufig)
        r.abschliessen()
        r.refresh_from_db()
        self.assertEqual(r.belegdatum, heute)

    def test_angebot_und_spaeteres_datum_bleiben(self):
        from datetime import timedelta
        from django.utils import timezone
        heute = timezone.localdate()
        a = self.beleg(Belegart.ANGEBOT, heute - timedelta(days=9))
        a.abschliessen()
        a.refresh_from_db()
        self.assertEqual(a.belegdatum, heute - timedelta(days=9))
        with self.assertRaisesMessage(ValueError, "liegt in der Zukunft"):  # Entscheidung des Nutzers 19.11./26.11.2026: nie in der Zukunft
            self.beleg(Belegart.RECHNUNG, heute + timedelta(days=3))


class MinusbelegTest(Basis):
    """Prüfbericht E6: Negative Einzelpreise sind erlaubt, die Belegsumme darf nicht negativ werden."""

    def beleg(self, *preise):
        b = Beleg.objects.create(mandant=self.m, art=Belegart.ANGEBOT, kunde=self.kunde)
        for preis in preise:
            Position.objects.create(beleg=b, typ="L", bezeichnung="Zeile", menge=1, einzelpreis=Decimal(preis))
        return b

    def test_minuszeile_mit_positiver_summe_ist_erlaubt(self):
        b = self.beleg("100", "-20")
        b.abschliessen()
        b.refresh_from_db()
        self.assertTrue(b.abgeschlossen)
        self.assertEqual(b.summe_netto, Decimal("80.00"))

    def test_summe_null_ist_erlaubt(self):
        b = self.beleg("20", "-20")
        b.abschliessen()
        b.refresh_from_db()
        self.assertTrue(b.abgeschlossen)

    def test_negative_belegsumme_wird_abgelehnt(self):
        b = self.beleg("10", "-20")
        with self.assertRaisesMessage(ValueError, "Die Belegsumme ist negativ"):
            b.abschliessen()
        b.refresh_from_db()
        self.assertFalse(b.abgeschlossen)
