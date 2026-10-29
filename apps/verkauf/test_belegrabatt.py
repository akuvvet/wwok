import xml.etree.ElementTree as ET
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.finanzen.datev import beleg_zeilen
from apps.kern.models import Mandant
from apps.stammdaten.models import Kunde

from .models import Beleg, Belegart, Position, rechnung_aus_lieferscheinen
from .zugferd import xml_erzeugen
from .pdf import beleg_pdf

RAM = "{urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100}"


@override_settings(SECURE_SSL_REDIRECT=False)
class BelegrabattTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer", strasse="Weg 1", plz="42651", ort="Solingen",
                                        ust_id="DE123456789", iban="DE02120300000000202051", email="info@okay.test")
        self.k = Kunde.objects.create(nummer="1", name1="Muster GmbH", strasse="Str. 2", plz="10115", ort="Berlin")
        self.client.force_login(get_user_model().objects.create_user("t", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    def rechnung(self, rabatt="10", versand="5"):
        b = Beleg.objects.create(mandant=self.m, art=Belegart.RECHNUNG, kunde=self.k, rabatt_prozent=Decimal(rabatt),
                                 versandkosten=Decimal(versand))
        Position.objects.create(beleg=b, bezeichnung="Dienst 19", einzelpreis=Decimal("100"), steuer_prozent=Decimal("19"))
        Position.objects.create(beleg=b, bezeichnung="Buch 7", einzelpreis=Decimal("50"), steuer_prozent=Decimal("7"))
        b.berechne()
        return b

    def test_rabatt_wirkt_auf_positionen_je_steuersatz_nicht_auf_versand(self):
        b = self.rechnung()
        # 100 -> 90 (19 %), 50 -> 45 (7 %); Versand 5 läuft mit 19 %: Basis 95
        self.assertEqual(b.summe_netto, Decimal("140.00"))
        self.assertEqual(b.summe_steuer, Decimal("18.05") + Decimal("3.15"))
        self.assertEqual(b.summe_brutto, Decimal("161.20"))

    def test_ohne_rabatt_unveraendert(self):
        b = self.rechnung(rabatt="0")
        self.assertEqual(b.summe_netto, Decimal("155.00"))
        self.assertEqual(b.summe_steuer, Decimal("23.45"))  # 19 % auf 105 (inkl. Versand) + 7 % auf 50

    def test_folgebeleg_und_gutschrift_uebernehmen_rabatt(self):
        b = self.rechnung()
        b.abschliessen()
        b.refresh_from_db()
        gs = b.gutschrift_erzeugen()
        self.assertEqual((gs.rabatt_prozent, gs.summe_brutto), (Decimal("10.00"), b.summe_brutto))

    def test_sammelrechnung_mit_unterschiedlichem_rabatt_wird_abgelehnt(self):
        a = Beleg.objects.create(mandant=self.m, art=Belegart.LIEFERSCHEIN, kunde=self.k, rabatt_prozent=Decimal("5"))
        b = Beleg.objects.create(mandant=self.m, art=Belegart.LIEFERSCHEIN, kunde=self.k)
        for x in (a, b):
            Position.objects.create(beleg=x, bezeichnung="Dienst", einzelpreis=Decimal("10"))
            x.abschliessen()
        with self.assertRaises(ValueError):
            rechnung_aus_lieferscheinen([a, b])
        gleich = Beleg.objects.create(mandant=self.m, art=Belegart.LIEFERSCHEIN, kunde=self.k, rabatt_prozent=Decimal("5"))
        Position.objects.create(beleg=gleich, bezeichnung="Dienst", einzelpreis=Decimal("10"))
        gleich.abschliessen()
        re = rechnung_aus_lieferscheinen([a, gleich])
        self.assertEqual((re.rabatt_prozent, re.summe_netto), (Decimal("5.00"), Decimal("19.00")))

    def test_zugferd_summen_stimmen_mit_beleg(self):
        b = self.rechnung()
        b.abschliessen()
        b.refresh_from_db()
        summen = ET.fromstring(xml_erzeugen(b)).find(f".//{RAM}SpecifiedTradeSettlementHeaderMonetarySummation")
        wert = lambda n: Decimal(summen.find(RAM + n).text)
        self.assertEqual(wert("LineTotalAmount"), Decimal("150.00"))
        self.assertEqual(wert("AllowanceTotalAmount"), Decimal("15.00"))
        self.assertEqual(wert("ChargeTotalAmount"), Decimal("5.00"))
        self.assertEqual(wert("TaxBasisTotalAmount"), b.summe_netto)
        self.assertEqual(wert("TaxTotalAmount"), b.summe_steuer)
        self.assertEqual(wert("GrandTotalAmount"), b.summe_brutto)

    def test_datev_summe_stimmt_mit_brutto(self):
        b = self.rechnung()
        self.assertEqual(sum(brutto for _, brutto in beleg_zeilen(b)), b.summe_brutto)

    def test_pdf_und_eingabe(self):
        b = self.rechnung()
        self.assertTrue(beleg_pdf(b).startswith(b"%PDF"))
        url = reverse("verkauf:beleg_kopf", args=[b.pk])
        self.client.post(url, {"rabatt_prozent": "20", "versandkosten": "5"})
        b.refresh_from_db()
        self.assertEqual(b.rabatt_prozent, Decimal("20.00"))
        self.assertEqual(b.summe_netto, Decimal("125.00"))
        r = self.client.post(url, {"rabatt_prozent": "101"}, follow=True)
        self.assertContains(r, "zwischen 0 und 100")
        b.refresh_from_db()
        self.assertEqual(b.rabatt_prozent, Decimal("20.00"))
