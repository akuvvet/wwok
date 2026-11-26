"""Steuerart des Kunden (Vorgabe des Nutzers 26.11.2026): 1 = mit MwSt (Standard), 2 = EG-Ausland, 3 = Drittland.
Bei 2 und 3 werden alle Preise ohne MwSt berechnet; EG-Ausland nur mit USt-ID; offene Belege rechnen bei einer Änderung neu,
abgeschlossene bleiben. Steuersatz am Artikel über die ID: 1 = 19 % (Standard), 2 = 7 %."""
import xml.etree.ElementTree as ET
from decimal import Decimal as D

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from apps.finanzen.datev import beleg_zeilen
from apps.kern.models import Mandant
from apps.lager.models import Bewegungsart
from apps.lager.services import buche_manuell
from apps.stammdaten.forms import KundeForm
from apps.stammdaten.management.commands.import_kunden import steuerart
from apps.stammdaten.models import Artikel, Artikeltyp, Kunde, Steuerart, Steuersatz

from .models import Beleg, Belegart, Position
from .pdf import beleg_pdf
from .zugferd import xml_erzeugen

RAM = "{urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100}"


@override_settings(SECURE_SSL_REDIRECT=False)
class SteuerartTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer", strasse="Weg 1", plz="42651", ort="Solingen",
                                        ust_id="DE123456789", iban="DE02120300000000202051", email="info@okay.test")
        self.s19 = Steuersatz.objects.get(nummer=1)  # aus der Migration: 1 = 19 %, 2 = 7 %
        self.s7 = Steuersatz.objects.get(nummer=2)
        self.ware = Artikel.objects.create(nummer="A1", bezeichnung="Monitor", typ=Artikeltyp.A, steuersatz=self.s19)
        self.buch = Artikel.objects.create(nummer="A2", bezeichnung="Buch", typ=Artikeltyp.A, steuersatz=self.s7)
        self.dienst = Artikel.objects.create(nummer="L1", bezeichnung="Einrichtung", typ=Artikeltyp.L, steuersatz=self.s19)
        self.k = Kunde.objects.create(nummer="1", name1="Muster BV", strasse="Str. 2", plz="1011", ort="Amsterdam", land="NL")
        for a in (self.ware, self.buch):
            buche_manuell(self.m, a, Bewegungsart.ANFANGSBESTAND, D("100"))

    def beleg(self, art=Belegart.RECHNUNG, versand="0"):
        b = Beleg.objects.create(mandant=self.m, art=art, kunde=self.k, versandkosten=D(versand))
        for a, preis in ((self.ware, "100"), (self.buch, "50")):
            Position.objects.create(beleg=b, artikel=a, typ=a.typ, bezeichnung=a.bezeichnung, einzelpreis=D(preis),
                                    steuer_prozent=b.steuer_fuer(a.steuersatz.prozent))
        b.berechne()
        return b

    def kunde_auf(self, art, ust_id=""):
        self.k.steuerart, self.k.ust_id = art, ust_id
        self.k.save()

    def test_standard_ist_mit_mwst(self):
        self.assertEqual(Kunde.objects.create(nummer="2", name1="X").steuerart, Steuerart.INLAND)
        b = self.beleg(versand="10")
        self.assertEqual((b.steuerart, b.summe_netto, b.summe_steuer, b.summe_brutto), (1, D("160.00"), D("24.40"), D("184.40")))
        self.assertEqual(b.steuerhinweise(), [])

    def test_eg_und_drittland_ohne_mwst(self):
        for art in (Steuerart.EG, Steuerart.DRITTLAND):
            with self.subTest(art):
                self.kunde_auf(art, "NL123456789B01")
                b = self.beleg(versand="10")
                self.assertEqual((b.steuerart, b.summe_netto, b.summe_steuer, b.summe_brutto), (art, D("160.00"), D("0"), D("160.00")))
                self.assertEqual(set(b.positionen.values_list("steuer_prozent", flat=True)), {D("0")})

    def test_eg_ohne_ust_id_laesst_sich_nicht_abschliessen(self):
        self.kunde_auf(Steuerart.EG)
        b = self.beleg()
        self.assertEqual(b.summe_steuer, D("0"))
        with self.assertRaisesMessage(ValueError, "keine USt-ID"):
            b.abschliessen()
        self.assertFalse(Beleg.objects.get(pk=b.pk).abgeschlossen)
        self.kunde_auf(Steuerart.EG, "NL123456789B01")
        b.refresh_from_db()
        b.abschliessen()
        self.assertTrue(Beleg.objects.get(pk=b.pk).abgeschlossen)
        angebot = self.beleg(Belegart.ANGEBOT)  # Angebote gehen auch ohne USt-ID
        self.kunde_auf(Steuerart.EG)
        angebot.refresh_from_db()
        angebot.abschliessen()

    def test_drittland_braucht_keine_ust_id(self):
        self.kunde_auf(Steuerart.DRITTLAND)
        b = self.beleg()
        b.abschliessen()
        self.assertTrue(Beleg.objects.get(pk=b.pk).abgeschlossen)

    def test_aenderung_rechnet_offene_belege_neu_abgeschlossene_bleiben(self):
        fertig = self.beleg()
        fertig.abschliessen()
        offen = self.beleg(Belegart.AUFTRAG)
        frei = Position.objects.create(beleg=offen, bezeichnung="Freitext", einzelpreis=D("10"), steuer_prozent=D("19"))
        offen.berechne()
        self.kunde_auf(Steuerart.EG, "NL123456789B01")
        offen.refresh_from_db()
        fertig.refresh_from_db()
        self.assertEqual((offen.steuerart, offen.summe_steuer, offen.summe_brutto), (2, D("0"), D("160.00")))
        self.assertEqual((fertig.steuerart, fertig.summe_steuer), (1, D("22.50")))
        self.kunde_auf(Steuerart.INLAND)  # zurück: Satz des Artikels, ohne Artikel der Standardsatz
        offen.refresh_from_db()
        self.assertEqual(dict(offen.positionen.values_list("bezeichnung", "steuer_prozent")),
                         {"Monitor": D("19"), "Buch": D("7"), "Freitext": D("19")})
        self.assertEqual(offen.summe_steuer, D("24.40"))
        frei.refresh_from_db()

    def test_gutschrift_folgt_der_rechnung(self):
        self.kunde_auf(Steuerart.DRITTLAND)
        re = self.beleg()
        re.abschliessen()
        re.refresh_from_db()
        self.kunde_auf(Steuerart.INLAND)
        gs = re.gutschrift_erzeugen()
        self.assertEqual((gs.steuerart, gs.summe_steuer, gs.summe_brutto), (3, D("0"), re.summe_brutto))

    def test_pdf_hinweise(self):
        self.kunde_auf(Steuerart.EG, "NL123456789B01")
        b = self.beleg()
        Position.objects.create(beleg=b, artikel=self.dienst, typ="L", bezeichnung="Einrichtung", einzelpreis=D("30"), steuer_prozent=D("19"))
        hinweise = b.steuerhinweise()
        self.assertIn("innergemeinschaftliche Lieferung", hinweise[0])
        self.assertIn("Steuerschuldnerschaft des Leistungsempfängers", hinweise[1])
        self.assertIn("NL123456789B01", hinweise[2])
        self.kunde_auf(Steuerart.DRITTLAND)
        b.refresh_from_db()
        self.assertIn("Ausfuhrlieferung", b.steuerhinweise()[0])
        self.assertTrue(beleg_pdf(b).startswith(b"%PDF"))

    def test_erechnung_kategorien(self):
        self.kunde_auf(Steuerart.EG, "NL123456789B01")
        b = self.beleg(versand="10")
        Position.objects.create(beleg=b, artikel=self.dienst, typ="L", bezeichnung="Einrichtung", einzelpreis=D("30"), steuer_prozent=D("19"))
        b.berechne()
        b.abschliessen()
        xml = ET.fromstring(xml_erzeugen(b))
        kopf = xml.find(f".//{RAM}ApplicableHeaderTradeSettlement")
        steuern = {t.find(f"{RAM}CategoryCode").text: (t.find(f"{RAM}BasisAmount").text, t.find(f"{RAM}ExemptionReasonCode").text)
                   for t in kopf.findall(f"{RAM}ApplicableTradeTax")}
        self.assertEqual(steuern, {"K": ("160.00", "VATEX-EU-IC"), "AE": ("30.00", "VATEX-EU-AE")})
        self.assertEqual(kopf.find(f".//{RAM}TaxTotalAmount").text, "0.00")

    def test_datev_konten(self):
        self.kunde_auf(Steuerart.EG, "NL123456789B01")
        b = self.beleg(versand="10")
        Position.objects.create(beleg=b, artikel=self.dienst, typ="L", bezeichnung="Einrichtung", einzelpreis=D("30"), steuer_prozent=D("19"))
        b.berechne()
        self.assertEqual(dict(beleg_zeilen(b)), {"8125": D("160.00"), "8338": D("30.00")})
        self.kunde_auf(Steuerart.DRITTLAND)
        b.refresh_from_db()
        self.assertEqual(dict(beleg_zeilen(b)), {"8120": D("190.00")})

    def test_kundenmaske_leer_ist_1(self):
        daten = {"nummer": "9", "name1": "Neu", "land": "DE", "kundenart": "endkunde", "preisstufe": "1", "steuerart": ""}
        form = KundeForm(daten)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["steuerart"], Steuerart.INLAND)
        self.assertTrue(KundeForm({**daten, "steuerart": "3"}).is_valid())

    def test_mention_import_steuerart(self):
        self.assertEqual([steuerart(w) for w in ("", 1.0, 2.0, "3", "9", "x")], [1, 1, 2, 3, 1, 1])


class SteuersatzIdTest(TestCase):
    def setUp(self):
        Steuersatz.objects.all().delete()

    def test_id_1_ist_19_und_standard(self):
        s19 = Steuersatz.objects.create(bezeichnung="Inland Mehrwertsteuer 19%", prozent=D("19"))  # Datensatz 1 ohne eigene ID
        s7 = Steuersatz.objects.create(bezeichnung="Inland Mehrwertsteuer 7%", prozent=D("7"))
        self.assertEqual(Steuersatz.standard(), s19)
        self.assertEqual(Steuersatz.finden(str(s7.pk)), s7)
        self.assertEqual(Steuersatz.finden("7 %"), s7)
        self.assertEqual(Steuersatz.finden("19,00"), s19)
        self.assertIsNone(Steuersatz.finden("16"))

    def test_mention_id_geht_vor(self):
        Steuersatz.objects.create(bezeichnung="Null", prozent=D("0"))
        s7 = Steuersatz.objects.create(nummer=2, bezeichnung="Inland Mehrwertsteuer 7%", prozent=D("7"))
        s19 = Steuersatz.objects.create(nummer=1, bezeichnung="Inland Mehrwertsteuer 19%", prozent=D("19"))
        self.assertEqual((Steuersatz.standard(), Steuersatz.finden("1"), Steuersatz.finden("2.0")), (s19, s19, s7))
