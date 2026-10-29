import xml.etree.ElementTree as ET
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from apps.kern.testhilfe import mit_logo

from apps.kern.models import Mandant
from apps.lager.models import Bewegungsart, Seriennummer, bestand
from apps.lager.services import buche_manuell
from apps.stammdaten.models import (Artikel, ArtikelLieferant, ArtikelMandant, Kunde, Lieferant, Steuersatz,
                                    Zahlungsbedingung)
from apps.verkauf.models import Beleg, Belegart, Position

from . import erechnung, sepa, services
from .models import BestellStatus, Lieferantenrechnung, RechnungStatus


@override_settings(SECURE_SSL_REDIRECT=False)
class EinkaufTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer", iban="DE02120300000000202051")
        zb = Zahlungsbedingung.objects.create(bezeichnung="14 T / 2 % 7 T", tage_netto=14, skonto_tage=7, skonto_prozent=Decimal("2"))
        self.lf = Lieferant.objects.create(nummer="L1", name1="Techdata GmbH", iban="DE89370400440532013000", ust_id="DE123456789",
                                           zahlungsbedingung=zb)
        self.lf2 = Lieferant.objects.create(nummer="L2", name1="Billig AG", iban="DE02500105170137075030")
        st = Steuersatz.objects.create(bezeichnung="19", prozent=Decimal("19"))
        self.maus = Artikel.objects.create(nummer="M1", bezeichnung="Maus", steuersatz=st, ean="4012345678901",
                                           vk1=Decimal("15"), durchschnitts_ek=Decimal("8"))
        self.pa = Artikel.objects.create(nummer="P1", typ="P", bezeichnung="Preislistenartikel", steuersatz=st, vk1=Decimal("30"))
        ArtikelLieferant.objects.create(artikel=self.maus, lieferant=self.lf, bestellnummer="TD-100", ek=Decimal("9.5000"), bestand=10)
        ArtikelLieferant.objects.create(artikel=self.maus, lieferant=self.lf2, bestellnummer="B-1", ek=Decimal("8.7000"), bestand=0)
        ArtikelLieferant.objects.create(artikel=self.pa, lieferant=self.lf, bestellnummer="TD-P", ek=Decimal("20.0000"), bestand=5)
        self.user = get_user_model().objects.create_user("t", password="x" * 14)

    def login(self):
        self.client.force_login(self.user)
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    def test_bestellung_wareneingang_scan_ek_und_status(self):
        b = services.bestellung_anlegen(self.m, self.lf)
        self.assertTrue(b.nummer.startswith("BE"))
        services.bestellposition(b, self.maus, Decimal("10"))
        pos = b.positionen.get()
        self.assertEqual((pos.ek, pos.bestellnummer), (Decimal("9.5000"), "TD-100"))  # EK aus der Preisliste
        services.bestellen(b)
        we = services.wareneingang_anlegen(self.m, self.lf, b)
        services.wareneingang_aus_bestellung(we)
        self.assertEqual(we.positionen.get().menge, Decimal("10"))
        wp = we.positionen.get()
        wp.menge = Decimal("4")
        wp.save()
        services.wareneingang_erfassen(we, services.artikel_per_code("4012345678901"))  # Scan über EAN: +1
        self.assertEqual(we.positionen.get().menge, Decimal("5"))
        buche_manuell(self.m, self.maus, Bewegungsart.WARENEINGANG, Decimal("5"))  # vorhandener Bestand mit EK 8
        services.wareneingang_buchen(we)
        b.refresh_from_db()
        self.assertEqual(b.status, BestellStatus.TEIL)
        self.assertEqual(bestand(self.m, self.maus), Decimal("10"))
        self.assertEqual(ArtikelMandant.objects.get(artikel=self.maus, mandant=self.m).letzter_ek, Decimal("9.5000"))
        self.maus.refresh_from_db()
        self.assertEqual(self.maus.durchschnitts_ek, Decimal("8.7500"))  # (5 × 8 + 5 × 9,5) / 10
        with self.assertRaises(ValueError):
            services.wareneingang_buchen(we)  # nicht doppelt
        we2 = services.wareneingang_anlegen(self.m, self.lf, b)
        services.wareneingang_aus_bestellung(we2)
        self.assertEqual(we2.positionen.get().menge, Decimal("5"))  # Rest
        services.wareneingang_buchen(we2)
        b.refresh_from_db()
        self.assertEqual(b.status, BestellStatus.GELIEFERT)

    def test_p_artikel_wird_a_und_seriennummern_pruefung(self):
        we = services.wareneingang_anlegen(self.m, self.lf)
        services.wareneingang_erfassen(we, self.pa, Decimal("2"), seriennummern="S1\nS2")
        self.assertEqual(services.wareneingang_buchen(we), 1)
        self.pa.refresh_from_db()
        self.assertEqual(self.pa.typ, "A")
        self.assertEqual(Seriennummer.objects.filter(artikel=self.pa).count(), 2)
        we2 = services.wareneingang_anlegen(self.m, self.lf)
        services.wareneingang_erfassen(we2, self.pa, Decimal("2"), seriennummern="S3")  # falsche Anzahl
        with self.assertRaises(ValueError):
            services.wareneingang_buchen(we2)
        self.assertEqual(bestand(self.m, self.pa), Decimal("2"))  # nichts halb gebucht

    def test_bestellvorschlag_und_aus_beleg(self):
        am = ArtikelMandant.objects.create(artikel=self.maus, mandant=self.m, mindestbestand=10)
        buche_manuell(self.m, self.maus, Bewegungsart.WARENEINGANG, Decimal("4"))
        v = services.bestellvorschlag(self.m)
        self.assertEqual((v[0]["artikel"], v[0]["menge"]), (self.maus, Decimal("6")))
        # der Billigste (lf2) hat Bestand 0 -> der Lieferant mit Bestand wird bevorzugt
        self.assertEqual(v[0]["lieferant"], self.lf)
        liste = services.bestellungen_aus_vorschlag(self.m, [(self.maus.pk, Decimal("6"))])
        self.assertEqual(liste[0].lieferant, self.lf)
        services.bestellen(liste[0])
        self.assertEqual(services.bestellvorschlag(self.m), [])  # bereits bestellt

        kunde = Kunde.objects.create(nummer="1", name1="K")
        angebot = Beleg.objects.create(mandant=self.m, art=Belegart.ANGEBOT, kunde=kunde)
        from apps.stammdaten.models import Preislistenartikel
        eintrag = Preislistenartikel.objects.create(lieferant=self.lf, bestellnummer="TD-P", herstellernummer="PX-1",
                                                    bezeichnung="Preislistenartikel", ek=Decimal("20.0000"))
        Position.objects.create(beleg=angebot, preisliste_artikel=eintrag, typ="P", bezeichnung="P", menge=Decimal("3"), einzelpreis=Decimal("30"))
        neu = services.bestellungen_aus_beleg(angebot)
        self.assertEqual((neu[0].lieferant, neu[0].positionen.get().menge, neu[0].positionen.get().ek), (self.lf, Decimal("3"), Decimal("20")))
        self.assertIsNone(neu[0].positionen.get().artikel)  # bleibt Preislistenartikel bis zum Wareneingang

    def test_rechnung_skonto_abgleich_und_sepa(self):
        we = services.wareneingang_anlegen(self.m, self.lf)
        services.wareneingang_erfassen(we, self.maus, Decimal("10"))
        services.wareneingang_buchen(we)
        r = services.rechnung_erfassen(self.m, self.lf, "R-1", date.today(), Decimal("95.00"), Decimal("113.05"), wareneingaenge=[we])
        self.assertTrue(r.abgleich()["ok"])
        self.assertEqual(r.faellig, date.today() + timedelta(days=14))
        self.assertEqual(r.zahlbetrag(), Decimal("110.79"))  # 2 % Skonto innerhalb 7 Tage
        self.assertEqual(r.zahlbetrag(date.today() + timedelta(days=8)), Decimal("113.05"))
        r2 = services.rechnung_erfassen(self.m, self.lf2, "X9", date.today(), Decimal("100.00"), Decimal("119.00"))
        r3 = services.rechnung_erfassen(self.m, self.lf, "R-2", date.today(), Decimal("50.00"), Decimal("59.50"), wareneingaenge=[we])
        self.assertFalse(r3.abgleich()["ok"])
        daten = sepa.erzeuge(self.m, [r, r2])
        wurzel = ET.fromstring(daten)
        ns = {"p": sepa.NS}
        self.assertEqual(wurzel.find(".//p:GrpHdr/p:NbOfTxs", ns).text, "2")
        self.assertEqual(wurzel.find(".//p:GrpHdr/p:CtrlSum", ns).text, "229.79")
        self.assertEqual([e.text for e in wurzel.findall(".//p:CdtTrfTxInf/p:CdtrAcct/p:Id/p:IBAN", ns)],
                         ["DE89370400440532013000", "DE02500105170137075030"])
        self.assertIn("Skonto", wurzel.findall(".//p:RmtInf/p:Ustrd", ns)[0].text)
        self.lf2.iban = ""
        self.lf2.save()
        with self.assertRaises(sepa.SepaFehler):
            sepa.erzeuge(self.m, [Lieferantenrechnung.objects.get(pk=r2.pk)])

    def test_erechnung_cii_und_ubl(self):
        cii = b"""<?xml version="1.0"?>
<rsm:CrossIndustryInvoice xmlns:rsm="urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100"
 xmlns:ram="urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100"
 xmlns:udt="urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100">
<rsm:ExchangedDocument><ram:ID>RE-2026-77</ram:ID><ram:IssueDateTime><udt:DateTimeString format="102">20261015</udt:DateTimeString></ram:IssueDateTime></rsm:ExchangedDocument>
<rsm:SupplyChainTradeTransaction><ram:ApplicableHeaderTradeAgreement><ram:SellerTradeParty><ram:Name>Techdata GmbH</ram:Name>
<ram:SpecifiedTaxRegistration><ram:ID schemeID="VA">DE123456789</ram:ID></ram:SpecifiedTaxRegistration></ram:SellerTradeParty></ram:ApplicableHeaderTradeAgreement>
<ram:ApplicableHeaderTradeSettlement><ram:SpecifiedTradePaymentTerms><ram:DueDateDateTime><udt:DateTimeString format="102">20261029</udt:DateTimeString></ram:DueDateDateTime></ram:SpecifiedTradePaymentTerms>
<ram:SpecifiedTradeSettlementHeaderMonetarySummation><ram:TaxBasisTotalAmount>100.00</ram:TaxBasisTotalAmount><ram:TaxTotalAmount currencyID="EUR">19.00</ram:TaxTotalAmount><ram:GrandTotalAmount>119.00</ram:GrandTotalAmount></ram:SpecifiedTradeSettlementHeaderMonetarySummation>
</ram:ApplicableHeaderTradeSettlement></rsm:SupplyChainTradeTransaction></rsm:CrossIndustryInvoice>"""
        d = erechnung.lese("re.xml", cii)
        self.assertEqual((d["nummer"], d["datum"], d["faellig"], d["netto"], d["brutto"], d["ust_id"]),
                         ("RE-2026-77", date(2026, 10, 15), date(2026, 10, 29), Decimal("100.00"), Decimal("119.00"), "DE123456789"))
        ubl = b"""<?xml version="1.0"?><Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
 xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2" xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2">
<cbc:ID>U-5</cbc:ID><cbc:IssueDate>2026-10-01</cbc:IssueDate><cbc:DueDate>2026-10-31</cbc:DueDate>
<cac:AccountingSupplierParty><cac:Party><cac:PartyName><cbc:Name>Billig AG</cbc:Name></cac:PartyName></cac:Party></cac:AccountingSupplierParty>
<cac:TaxTotal><cbc:TaxAmount>19.00</cbc:TaxAmount></cac:TaxTotal>
<cac:LegalMonetaryTotal><cbc:TaxExclusiveAmount>100.00</cbc:TaxExclusiveAmount><cbc:TaxInclusiveAmount>119.00</cbc:TaxInclusiveAmount></cac:LegalMonetaryTotal></Invoice>"""
        u = erechnung.lese("u.xml", ubl)
        self.assertEqual((u["nummer"], u["lieferant"], u["faellig"], u["brutto"]), ("U-5", "Billig AG", date(2026, 10, 31), Decimal("119.00")))
        with self.assertRaises(erechnung.ERechnungFehler):
            erechnung.lese("x.xml", b"<foo/>")

    def test_masken(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        self.login()
        r = self.client.post(reverse("einkauf:bestellung_neu"), {"lieferant": self.lf.pk})
        b = services.Bestellung.objects.get()
        self.assertRedirects(r, reverse("einkauf:bestellung", args=[b.pk]))
        self.client.post(reverse("einkauf:bestellung_position", args=[b.pk]), {"artikel": "TD-100", "menge": "3"})  # Bestellnr. des Lieferanten
        self.assertEqual(b.positionen.get().artikel, self.maus)
        self.assertContains(self.client.get(reverse("einkauf:bestellung", args=[b.pk])), "Maus")
        self.assertEqual(self.client.get(reverse("einkauf:bestellung_pdf", args=[b.pk]))["Content-Type"], "application/pdf")
        ohne = len(self.client.get(reverse("einkauf:bestellung_pdf", args=[b.pk])).content)
        with mit_logo(self.m):
            self.assertGreater(len(self.client.get(reverse("einkauf:bestellung_pdf", args=[b.pk])).content), ohne)  # Logo eingebettet
        self.assertEqual(self.client.get(reverse("einkauf:bestellung_mail", args=[b.pk])).status_code, 200)
        self.client.post(reverse("einkauf:bestellung_bestellen", args=[b.pk]))
        self.client.post(reverse("einkauf:wareneingang_neu"), {"bestellung": b.pk})
        we = services.Wareneingang.objects.get()
        self.client.post(reverse("einkauf:wareneingang_scan", args=[we.pk]), {"code": "4012345678901", "menge": "1"})
        self.assertEqual(we.positionen.get().menge, Decimal("4"))
        self.client.post(reverse("einkauf:wareneingang_buchen", args=[we.pk]), {"lieferschein": "LS-77"})
        self.assertEqual(bestand(self.m, self.maus), Decimal("4"))
        for name in ("bestellungen", "vorschlag", "wareneingaenge", "rechnungen", "rma_liste"):
            self.assertEqual(self.client.get(reverse(f"einkauf:{name}")).status_code, 200, name)
        self.client.post(reverse("einkauf:rechnung_neu"), {"lieferant": self.lf.pk, "rechnungsnummer": "A1", "datum": date.today().isoformat(),
                                                            "netto": "38", "brutto": "45,22"})
        rech = Lieferantenrechnung.objects.get()
        self.assertContains(self.client.get(reverse("einkauf:rechnung", args=[rech.pk])), "A1")
        r = self.client.post(reverse("einkauf:sepa"), {"auswahl": [rech.pk], "ausfuehrung": date.today().isoformat()})
        self.assertEqual(r["Content-Type"], "application/xml")
        rech.refresh_from_db()
        self.assertEqual(rech.status, RechnungStatus.EXPORTIERT)
        self.client.post(reverse("einkauf:rechnung_bezahlt", args=[rech.pk]), {})
        rech.refresh_from_db()
        self.assertEqual(rech.status, RechnungStatus.BEZAHLT)

    def test_rma_aus_reparatur(self):
        from apps.werkstatt import services as ws
        from apps.werkstatt.models import Status
        self.login()
        kunde = Kunde.objects.create(nummer="1", name1="K")
        rep = ws.annehmen(self.m, kunde, "Notebook", "defekt", seriennummer="SN1", artikel=self.maus)
        self.client.post(reverse("einkauf:rma_aus_reparatur", args=[rep.pk]), {"lieferant": self.lf.pk})
        rep.refresh_from_db()
        rma = rep.rmas.get()
        self.assertEqual((rep.status, rma.seriennummer, rma.lieferant), (Status.RMA, "SN1", self.lf))
        self.client.post(reverse("einkauf:rma", args=[rma.pk]), {"status": "gutschrift", "gutschrift_betrag": "120,00"})
        rma.refresh_from_db()
        self.assertEqual((rma.status, rma.gutschrift_betrag, rma.erledigt_am), ("gutschrift", Decimal("120.00"), date.today()))
        self.assertContains(self.client.get(reverse("werkstatt:detail", args=[rep.pk])), rma.nummer)
