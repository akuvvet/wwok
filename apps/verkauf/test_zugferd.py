import xml.etree.ElementTree as ET
from decimal import Decimal

from django.test import TestCase

from apps.einkauf import erechnung
from apps.kern.models import Mandant
from apps.stammdaten.models import Artikel, Kunde, Steuersatz, Zahlungsbedingung

from .models import Beleg, Belegart, Position
from .zugferd import ERechnungAusgangFehler, beleg_pdf_mit_xml, xml_erzeugen

RAM = "{urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100}"


class ZugferdTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer", strasse="Weg 1", plz="42651", ort="Solingen",
                                        ust_id="DE123456789", iban="DE02120300000000202051", email="info@okay.test")
        self.k = Kunde.objects.create(nummer="1", name1="Muster GmbH", strasse="Str. 2", plz="10115", ort="Berlin")
        st = Steuersatz.objects.create(bezeichnung="19", prozent=Decimal("19"))
        self.zb = Zahlungsbedingung.objects.create(bezeichnung="14 Tage", tage_netto=14, druck_text="Zahlbar in 14 Tagen")
        self.a = Artikel.objects.create(nummer="A1", bezeichnung="Maus", steuersatz=st, vk1=Decimal("10"))
        from apps.lager.models import Bewegungsart
        from apps.lager.services import buche_manuell
        buche_manuell(self.m, self.a, Bewegungsart.WARENEINGANG, Decimal("50"))

    def rechnung(self):
        b = Beleg.objects.create(mandant=self.m, art=Belegart.RECHNUNG, kunde=self.k, zahlungsbedingung=self.zb,
                                 versandkosten=Decimal("5"))
        Position.objects.create(beleg=b, artikel=self.a, bezeichnung="Maus", menge=3, einzelpreis=Decimal("10"),
                                rabatt_prozent=Decimal("10"))
        b.abschliessen()
        b.refresh_from_db()
        return b

    def test_summen_stimmen_mit_beleg(self):
        b = self.rechnung()
        w = ET.fromstring(xml_erzeugen(b))
        summen = w.find(f".//{RAM}SpecifiedTradeSettlementHeaderMonetarySummation")
        wert = lambda n: Decimal(summen.find(RAM + n).text)
        self.assertEqual(wert("LineTotalAmount"), Decimal("27.00"))
        self.assertEqual(wert("ChargeTotalAmount"), Decimal("5.00"))
        self.assertEqual(wert("TaxBasisTotalAmount"), b.summe_netto)
        self.assertEqual(wert("TaxTotalAmount"), b.summe_steuer)
        self.assertEqual(wert("GrandTotalAmount"), b.summe_brutto)

    def test_pdf_enthaelt_xml_und_ist_rueckwaerts_lesbar(self):
        b = self.rechnung()
        pdf, hinweis = beleg_pdf_mit_xml(b)
        self.assertEqual(hinweis, "")
        daten = erechnung.lese(f"{b.nummer}.pdf", pdf)
        self.assertEqual(daten["nummer"], b.nummer)
        self.assertEqual(daten["brutto"], b.summe_brutto)

    def test_fehlende_ust_id_gibt_normales_pdf_mit_hinweis(self):
        b = self.rechnung()
        Mandant.objects.filter(pk=self.m.pk).update(ust_id="", steuernummer="")
        b.refresh_from_db()
        pdf, hinweis = beleg_pdf_mit_xml(Beleg.objects.get(pk=b.pk))
        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertIn("USt-ID", hinweis)
        with self.assertRaises(ERechnungAusgangFehler):
            xml_erzeugen(Beleg.objects.get(pk=b.pk))

    def test_entwurf_und_angebot_ohne_xml(self):
        b = Beleg.objects.create(mandant=self.m, art=Belegart.ANGEBOT, kunde=self.k)
        Position.objects.create(beleg=b, bezeichnung="X", menge=1, einzelpreis=Decimal("1"))
        self.assertEqual(beleg_pdf_mit_xml(b)[1], "")

    def test_xrechnung_braucht_leitweg_id_und_kontakt(self):
        b = self.rechnung()
        with self.assertRaisesMessage(ERechnungAusgangFehler, "Leitweg-ID"):
            xml_erzeugen(b, xrechnung=True)
        self.k.leitweg_id = "04011000-12345-67"
        self.k.save()
        b = Beleg.objects.get(pk=b.pk)
        with self.assertRaisesMessage(ERechnungAusgangFehler, "Telefon"):
            xml_erzeugen(b, xrechnung=True)
        self.m.telefon = "0212 123456"
        self.m.save()
        b = Beleg.objects.get(pk=b.pk)
        xml = xml_erzeugen(b, xrechnung=True)
        self.assertIn(b"xrechnung_3.0", xml)
        self.assertIn(b"04011000-12345-67", xml)
        self.assertEqual(erechnung.lese("x.xml", xml)["nummer"], b.nummer)  # wieder einlesbar
