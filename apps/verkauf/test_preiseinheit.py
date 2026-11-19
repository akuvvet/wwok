"""Artikelstamm-Felder im Beleg: Preiseinheit (VK je n Stück), Preisgrenzen-Warnung, Grundpreis im PDF."""
import io
import xml.etree.ElementTree as ET
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from pypdf import PdfReader

from apps.kern.models import Mandant
from apps.stammdaten.models import Artikel, Kunde, Steuersatz

from .models import Beleg, Belegart, Position, kopiere_positionen
from .pdf import beleg_pdf
from .zugferd import xml_erzeugen

RAM = "{urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100}"
D = Decimal


def pdf_text(beleg) -> str:
    return "\n".join(s.extract_text() or "" for s in PdfReader(io.BytesIO(beleg_pdf(beleg))).pages)


@override_settings(SECURE_SSL_REDIRECT=False)
class PreiseinheitTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer", strasse="Weg 1", plz="42651", ort="Solingen",
                                        ust_id="DE123456789", iban="DE02120300000000202051", email="info@okay.test")
        self.k = Kunde.objects.create(nummer="1", name1="Muster GmbH", strasse="Str. 2", plz="10115", ort="Berlin")
        self.st = Steuersatz.objects.create(bezeichnung="19", prozent=D("19"))
        # Schrauben: 100 Stück kosten 12,00 netto, Gewicht 25 g je Stück -> Grundpreis je kg
        self.schraube = Artikel.objects.create(
            nummer="S1", bezeichnung="Schraube", steuersatz=self.st, vk1=D("12.00"), preiseinheit=100,
            grundpreis_faktor=D("0.04"), grundpreis_einheit="kg", vk_min=D("10.00"), vk_max=D("15.00"))
        self.maus = Artikel.objects.create(nummer="A1", bezeichnung="Maus", steuersatz=self.st, vk1=D("10"))
        from apps.lager.models import Bewegungsart
        from apps.lager.services import buche_manuell
        for a in (self.schraube, self.maus):
            buche_manuell(self.m, a, Bewegungsart.WARENEINGANG, D("1000"))
        self.client.force_login(get_user_model().objects.create_user("t", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    def beleg(self, art=Belegart.ANGEBOT):
        return Beleg.objects.create(mandant=self.m, art=art, kunde=self.k)

    def maske(self, beleg):
        return self.client.get(reverse("verkauf:beleg", args=[beleg.pk]))

    # --- Preiseinheit: Übernahme und Rechnung ------------------------------------------------------------

    def test_position_uebernimmt_preiseinheit_aus_dem_artikel(self):
        b = self.beleg()
        self.client.post(reverse("verkauf:position_neu", args=[b.pk]), {"artikel": "S1", "menge": "250", "preis": "12,00"})
        p = b.positionen.get()
        self.assertEqual(p.preiseinheit, 100)
        self.assertEqual(p.netto_gesamt, D("30.00"))  # 250 × 12,00 ÷ 100
        b.refresh_from_db()
        self.assertEqual(b.summe_netto, D("30.00"))

    def test_artikel_ohne_preiseinheit_rechnet_wie_bisher(self):
        b = self.beleg()
        self.client.post(reverse("verkauf:position_neu", args=[b.pk]), {"artikel": "A1", "menge": "3", "preis": "10,00"})
        p = b.positionen.get()
        self.assertEqual((p.preiseinheit, p.netto_gesamt), (1, D("30.00")))

    def test_summe_mit_rabatt_und_rundung_auf_cent(self):
        b = self.beleg()
        p = Position.objects.create(beleg=b, artikel=self.schraube, bezeichnung="Schraube", menge=D("33"), einzelpreis=D("12.34"),
                                    preiseinheit=100, rabatt_prozent=D("10"))
        # 33 × 12,34 ÷ 100 = 4,0722; −10 % = 3,66498 -> 3,66
        self.assertEqual(p.netto_gesamt, D("3.66"))
        b.berechne()
        b.refresh_from_db()
        self.assertEqual(b.summe_netto, D("3.66"))
        self.assertEqual(b.summe_brutto, D("4.36"))  # 3,66 + 19 % = 4,3554

    def test_position_aendern_behaelt_preiseinheit(self):
        b = self.beleg()
        p = Position.objects.create(beleg=b, artikel=self.schraube, bezeichnung="Schraube", menge=D("100"), einzelpreis=D("12"),
                                    preiseinheit=100)
        self.client.post(reverse("verkauf:position_aendern", args=[b.pk, p.pk]), {"menge": "500", "preis": "11,00", "rabatt": "0"})
        p.refresh_from_db()
        self.assertEqual((p.preiseinheit, p.netto_gesamt), (100, D("55.00")))

    def test_gruppe_summiert_mit_preiseinheit(self):
        b = self.beleg()
        gruppe = Position.objects.create(beleg=b, typ="G", bezeichnung="Set", menge=D("1"), einzelpreis=D("0"))
        self.client.post(reverse("verkauf:position_neu", args=[b.pk]),
                         {"artikel": "S1", "menge": "200", "preis": "12,00", "gruppe": gruppe.pk})
        b.refresh_from_db()
        gruppe.refresh_from_db()
        self.assertEqual(gruppe.einzelpreis, D("24.00"))  # 200 × 12,00 ÷ 100
        self.assertEqual(b.summe_netto, D("24.00"))

    def test_kopie_und_folgebeleg_behalten_preiseinheit(self):
        b = self.beleg(Belegart.AUFTRAG)
        Position.objects.create(beleg=b, artikel=self.schraube, bezeichnung="Schraube", menge=D("50"), einzelpreis=D("12"), preiseinheit=100)
        ziel = self.beleg(Belegart.LIEFERSCHEIN)
        kopiere_positionen(b, ziel)
        self.assertEqual(ziel.positionen.get().preiseinheit, 100)
        ls = b.folgebeleg_erzeugen(art=Belegart.LIEFERSCHEIN)
        kopie = ls.positionen.get()
        self.assertEqual((kopie.preiseinheit, kopie.netto_gesamt), (100, D("6.00")))
        ls.abschliessen()
        re_ = ls.folgebeleg_erzeugen(art=Belegart.RECHNUNG)
        self.assertEqual(re_.positionen.get().preiseinheit, 100)

    def test_altbestand_preiseinheit_1_unveraendert(self):
        b = self.beleg()
        p = Position.objects.create(beleg=b, bezeichnung="Alt", menge=D("3"), einzelpreis=D("9.99"))
        self.assertEqual(p.preiseinheit, 1)
        self.assertEqual(p.netto_gesamt, D("29.97"))

    def test_letzter_preis_in_preiseinheit_des_artikels(self):
        from .models import letzte_preise
        b = self.beleg(Belegart.RECHNUNG)
        Position.objects.create(beleg=b, artikel=self.schraube, bezeichnung="Schraube", menge=D("5"), einzelpreis=D("0.10"), preiseinheit=1)
        b.abgeschlossen = True
        Beleg.objects.filter(pk=b.pk).update(abgeschlossen=True)
        erg = letzte_preise(self.m, self.k, [self.schraube.pk])
        self.assertEqual(erg[("a", self.schraube.pk)]["preis"], D("10.00"))  # 0,10 je Stück = 10,00 je 100

    def test_dashboard_top_artikel_rechnet_mit_preiseinheit(self):
        from apps.kern import dashboard
        b = self.beleg(Belegart.RECHNUNG)
        Position.objects.create(beleg=b, artikel=self.schraube, bezeichnung="Schraube", menge=D("250"), einzelpreis=D("12"), preiseinheit=100)
        Beleg.objects.filter(pk=b.pk).update(abgeschlossen=True)
        liste = dashboard.top_artikel(self.m, b.belegdatum, b.belegdatum)
        self.assertEqual(liste[0]["umsatz"], D("30.00"))

    def test_aufruf_der_maske_zeigt_je_100_stueck(self):
        b = self.beleg()
        Position.objects.create(beleg=b, artikel=self.schraube, bezeichnung="Schraube", menge=D("50"), einzelpreis=D("12"), preiseinheit=100)
        self.assertContains(self.maske(b), "je 100 Stück")

    # --- PDF ---------------------------------------------------------------------------------------------------

    def test_pdf_zeigt_je_100_stueck_und_grundpreis(self):
        b = self.beleg()
        Position.objects.create(beleg=b, artikel=self.schraube, bezeichnung="Schraube", menge=D("250"), einzelpreis=D("12.00"),
                                preiseinheit=100)
        b.berechne()
        text = pdf_text(b)
        self.assertIn("je 100 Stück", text)
        # brutto je Stück: 12,00 ÷ 100 × 1,19 = 0,1428; × Faktor 0,04 = 0,0057 -> 0,01 (Faktor hier nur Prüfwert)
        self.assertIn("Grundpreis: ", text)
        self.assertIn(self.schraube.grundpreis_text(D("12.00") / 100 * D("1.19")), text)

    def test_grundpreis_aus_brutto_nach_rabatt(self):
        a = Artikel.objects.create(nummer="K1", bezeichnung="Kabel", steuersatz=self.st, vk1=D("10"),
                                   grundpreis_faktor=D("10"), grundpreis_einheit="kg")
        b = self.beleg()
        p = Position.objects.create(beleg=b, artikel=a, bezeichnung="Kabel", menge=D("1"), einzelpreis=D("10.00"),
                                    rabatt_prozent=D("50"))
        self.assertEqual(p.grundpreis_text, "59,50 € / kg")  # 10 − 50 % = 5 netto = 5,95 brutto × 10
        self.assertIn("Grundpreis: 59,50 € / kg", pdf_text(b))

    def test_kein_grundpreis_ohne_faktor_im_lieferschein_und_in_der_gutschrift(self):
        b = self.beleg()
        Position.objects.create(beleg=b, artikel=self.maus, bezeichnung="Maus", menge=D("1"), einzelpreis=D("10"))
        self.assertNotIn("Grundpreis", pdf_text(b))
        for art in (Belegart.LIEFERSCHEIN, Belegart.GUTSCHRIFT):
            x = self.beleg(art)
            Position.objects.create(beleg=x, artikel=self.schraube, bezeichnung="Schraube", menge=D("1"), einzelpreis=D("12"),
                                    preiseinheit=100)
            with self.subTest(art=art):
                self.assertNotIn("Grundpreis", pdf_text(x))

    def test_pdf_zeigt_keine_preiswarnung(self):
        b = self.beleg()
        Position.objects.create(beleg=b, artikel=self.schraube, bezeichnung="Schraube", menge=D("1"), einzelpreis=D("5"), preiseinheit=100)
        text = pdf_text(b)
        self.assertNotIn("Mindest-VK", text)
        self.assertNotIn("Achtung", text)

    # --- ZUGFeRD -----------------------------------------------------------------------------------------------

    def test_zugferd_summen_und_basismenge(self):
        b = self.beleg(Belegart.RECHNUNG)
        Position.objects.create(beleg=b, artikel=self.schraube, bezeichnung="Schraube", menge=D("250"), einzelpreis=D("12.00"),
                                preiseinheit=100, rabatt_prozent=D("10"))
        Position.objects.create(beleg=b, artikel=self.maus, bezeichnung="Maus", menge=D("2"), einzelpreis=D("10"))
        b.abschliessen()
        b.refresh_from_db()
        w = ET.fromstring(xml_erzeugen(b))
        zeilen = w.findall(f".//{RAM}IncludedSupplyChainTradeLineItem")
        preis0 = zeilen[0].find(f".//{RAM}NetPriceProductTradePrice")
        self.assertEqual(Decimal(preis0.find(RAM + "ChargeAmount").text), D("10.8000"))  # je 100 Stück nach Rabatt
        self.assertEqual(Decimal(preis0.find(RAM + "BasisQuantity").text), D("100"))
        self.assertIsNone(zeilen[1].find(f".//{RAM}BasisQuantity"))  # Preiseinheit 1: keine Basismenge
        # Zeilensumme laut XML = Menge × Preis ÷ Basismenge = Beleg
        menge = Decimal(zeilen[0].find(f".//{RAM}BilledQuantity").text)
        self.assertEqual((menge * D("10.8") / 100).quantize(D("0.01")), D("27.00"))
        self.assertEqual(Decimal(zeilen[0].find(f".//{RAM}LineTotalAmount").text), D("27.00"))
        summen = w.find(f".//{RAM}SpecifiedTradeSettlementHeaderMonetarySummation")
        self.assertEqual(Decimal(summen.find(RAM + "LineTotalAmount").text), D("47.00"))
        self.assertEqual(Decimal(summen.find(RAM + "GrandTotalAmount").text), b.summe_brutto)

    # --- Preisgrenzen-Warnung ----------------------------------------------------------------------------------

    def test_warnung_unter_und_ueber_den_grenzen(self):
        b = self.beleg()
        p = Position.objects.create(beleg=b, artikel=self.schraube, bezeichnung="Schraube", menge=D("1"), einzelpreis=D("9.99"),
                                    preiseinheit=100)
        self.assertEqual(p.preisgrenzen_warnung, "unter Mindest-VK 10,00 €")
        self.assertContains(self.maske(b), "unter Mindest-VK 10,00 €")
        p.einzelpreis = D("15.01")
        p.save()
        self.assertEqual(p.preisgrenzen_warnung, "über Höchst-VK 15,00 €")
        self.assertContains(self.maske(b), "über Höchst-VK 15,00 €")

    def test_keine_warnung_innerhalb_der_grenzen_und_auf_der_grenze(self):
        b = self.beleg()
        for preis in ("10.00", "12.50", "15.00"):
            p = Position.objects.create(beleg=b, artikel=self.schraube, bezeichnung="Schraube", menge=D("1"), einzelpreis=D(preis))
            self.assertEqual(p.preisgrenzen_warnung, "", preis)
        self.assertNotContains(self.maske(b), "Mindest-VK")
        self.assertNotContains(self.maske(b), "Höchst-VK")

    def test_warnung_nach_positionsrabatt(self):
        b = self.beleg()
        p = Position.objects.create(beleg=b, artikel=self.schraube, bezeichnung="Schraube", menge=D("1"), einzelpreis=D("12.00"),
                                    rabatt_prozent=D("20"))  # 9,60 nach Rabatt
        self.assertEqual(p.preisgrenzen_warnung, "unter Mindest-VK 10,00 €")

    def test_keine_warnung_ohne_grenzen_ohne_artikel_und_in_der_gutschrift(self):
        b = self.beleg()
        ohne = Position.objects.create(beleg=b, artikel=self.maus, bezeichnung="Maus", menge=D("1"), einzelpreis=D("0.01"))
        frei = Position.objects.create(beleg=b, bezeichnung="Frei", menge=D("1"), einzelpreis=D("0.01"))
        self.assertEqual((ohne.preisgrenzen_warnung, frei.preisgrenzen_warnung), ("", ""))
        gs = self.beleg(Belegart.GUTSCHRIFT)
        Position.objects.create(beleg=gs, artikel=self.schraube, bezeichnung="Schraube", menge=D("1"), einzelpreis=D("1"))
        self.assertNotContains(self.maske(gs), "Mindest-VK")

    def test_warnung_ist_keine_sperre(self):
        b = self.beleg()
        self.client.post(reverse("verkauf:position_neu", args=[b.pk]), {"artikel": "S1", "menge": "1", "preis": "1,00"})
        self.assertEqual(b.positionen.count(), 1)
        self.assertEqual(b.positionen.get().einzelpreis, D("1.00"))

    def test_keine_warnung_im_abgeschlossenen_beleg(self):
        b = self.beleg(Belegart.RECHNUNG)
        Position.objects.create(beleg=b, artikel=self.schraube, bezeichnung="Schraube", menge=D("1"), einzelpreis=D("1"), preiseinheit=100)
        b.abschliessen()
        self.assertNotContains(self.maske(b), "Mindest-VK")
