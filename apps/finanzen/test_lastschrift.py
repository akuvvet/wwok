import xml.etree.ElementTree as ET
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.kern.models import Mandant
from apps.stammdaten.models import Kunde
from apps.verkauf.models import Beleg, Belegart, Position

from .models import Lastschrift

NS = {"p": "urn:iso:std:iso:20022:tech:xsd:pain.008.001.02"}


@override_settings(SECURE_SSL_REDIRECT=False)
class LastschriftTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer", iban="DE02120300000000202051",
                                        glaeubiger_id="DE98ZZZ09999999999")
        self.k = Kunde.objects.create(nummer="1", name1="Muster GmbH", iban="DE89370400440532013000",
                                      sepa_mandatsreferenz="M-1", sepa_mandat_datum=date(2026, 1, 5))
        self.ohne = Kunde.objects.create(nummer="2", name1="Ohne Mandat")
        self.re = self.rechnung(self.k)
        self.client.force_login(get_user_model().objects.create_user("t", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()
        self.morgen = (date.today() + timedelta(days=1)).isoformat()

    def rechnung(self, kunde):
        b = Beleg.objects.create(mandant=self.m, art=Belegart.RECHNUNG, kunde=kunde)
        Position.objects.create(beleg=b, bezeichnung="Dienst", einzelpreis=Decimal("100"))
        b.abschliessen()
        b.refresh_from_db()
        return b

    def export(self, belege, einzug=None):
        return self.client.post(reverse("finanzen:lastschrift_export"),
                                {"auswahl": [b.pk for b in belege], "einzug": einzug or self.morgen})

    def test_datei_aufbau_und_protokoll(self):
        r = self.export([self.re])
        self.assertEqual(r.status_code, 200)
        wurzel = ET.fromstring(r.content)
        self.assertEqual(wurzel.find(".//p:PmtInf/p:PmtTpInf/p:SeqTp", NS).text, "FRST")
        self.assertEqual(wurzel.find(".//p:DrctDbtTxInf/p:InstdAmt", NS).text, "119.00")
        self.assertEqual(wurzel.find(".//p:MndtRltdInf/p:MndtId", NS).text, "M-1")
        self.assertEqual(wurzel.find(".//p:CdtrSchmeId//p:Othr/p:Id", NS).text, "DE98ZZZ09999999999")
        self.assertEqual(Lastschrift.objects.get().betrag, Decimal("119.00"))

    def test_zweiter_einzug_ist_folgelastschrift_und_doppelt_gesperrt(self):
        self.export([self.re])
        r = self.export([self.re])  # derselbe Beleg nochmal
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Lastschrift.objects.count(), 1)
        ls = Lastschrift.objects.get()
        self.client.post(reverse("finanzen:lastschrift_aufheben", args=[ls.pk]), {"grund": "Rücklastschrift"})
        self.assertTrue(Lastschrift.objects.get().aufgehoben)
        zweite = self.rechnung(self.k)
        r = self.export([zweite])
        self.assertEqual(ET.fromstring(r.content).find(".//p:SeqTp", NS).text, "FRST")  # aufgehobene zählt nicht

    def test_mandat_fehlt_blockiert(self):
        re2 = self.rechnung(self.ohne)
        r = self.export([re2], einzug=self.morgen)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Lastschrift.objects.count(), 0)
        self.assertContains(self.client.get(reverse("finanzen:lastschrift")), "fehlt: IBAN, Mandatsreferenz, Mandatsdatum")

    def test_einzugsdatum_nicht_heute(self):
        r = self.export([self.re], einzug=date.today().isoformat())
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Lastschrift.objects.count(), 0)
