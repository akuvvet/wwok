"""Lastschrift im Bankabgleich (Prüfbericht A8): Zahlung erst mit der Gutschrift der Bank, Rücklastschrift storniert sie."""
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.kern.models import Mandant
from apps.stammdaten.models import Kunde
from apps.verkauf.models import Beleg, Belegart, Position

from . import bank, services
from .models import Bankumsatz, Lastschrift, Zahlart

CAMT = """<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.02"><BkToCstmrStmt><Stmt>
<Ntry><Amt Ccy="EUR">238.00</Amt><CdtDbtInd>CRDT</CdtDbtInd><BookgDt><Dt>2026-11-17</Dt></BookgDt>
  <NtryDtls><Btch><PmtInfId>DATEI-A-FRST</PmtInfId></Btch></NtryDtls><AddtlNtryInf>SEPA-Lastschrift Sammler</AddtlNtryInf></Ntry>
<Ntry><Amt Ccy="EUR">123.50</Amt><CdtDbtInd>DBIT</CdtDbtInd><BookgDt><Dt>2026-11-19</Dt></BookgDt>
  <NtryDtls><TxDtls><Refs><EndToEndId>{nummer}</EndToEndId></Refs><RmtInf><Ustrd>Rueckgabe mangels Deckung</Ustrd></RmtInf></TxDtls></NtryDtls></Ntry>
</Stmt></BkToCstmrStmt></Document>"""


@override_settings(SECURE_SSL_REDIRECT=False)
class LastschriftBankTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.k = Kunde.objects.create(nummer="1", name1="Muster GmbH")
        self.r1, self.r2, self.r3 = self.rechnung(), self.rechnung(), self.rechnung()
        self.l1, self.l2 = self.lastschrift(self.r1), self.lastschrift(self.r2)
        self.client.force_login(get_user_model().objects.create_superuser("chef", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    def rechnung(self):
        b = Beleg.objects.create(mandant=self.m, art=Belegart.RECHNUNG, kunde=self.k, belegdatum=date(2026, 11, 1))
        Position.objects.create(beleg=b, bezeichnung="Dienst", einzelpreis=Decimal("100"))
        b.abschliessen()
        Beleg.objects.filter(pk=b.pk).update(belegdatum=date(2026, 11, 1))  # der Abschluss setzt das Tagesdatum
        b.refresh_from_db()
        return b

    def lastschrift(self, beleg, datei="DATEI-A", sequenz="FRST", einzug=date(2026, 11, 16)):
        return Lastschrift.objects.create(mandant=self.m, beleg=beleg, betrag=beleg.summe_brutto, einzugsdatum=einzug,
                                          mandatsreferenz="M-1", sequenz=sequenz, datei=datei)

    def umsatz(self, betrag, tag=date(2026, 11, 17), zweck="", referenz=""):
        return Bankumsatz.objects.create(mandant=self.m, buchungsdatum=tag, betrag=Decimal(betrag), zweck=zweck, referenz=referenz,
                                         pruefsumme=f"{betrag}|{tag}|{zweck}|{referenz}")

    def test_einzug_allein_bucht_keine_zahlung(self):
        self.assertEqual((services.offen(self.r1), self.r1.zahlungen.count()), (Decimal("119.00"), 0))
        self.assertContains(self.client.get(reverse("finanzen:lastschrift")), "Gutschrift noch nicht im Kontoauszug")

    def test_sammelgutschrift_bucht_je_rechnung_eine_zahlung(self):
        u = self.umsatz("238.00", zweck="SEPA-Lastschrift Sammler")  # ohne Referenz: Summe und Datum passen zu genau einer Datei
        self.assertTrue(bank.zuordnen_automatisch(u))
        u.refresh_from_db(), self.l1.refresh_from_db(), self.l2.refresh_from_db()
        self.assertEqual((u.status, u.zahlungen.count()), (Bankumsatz.ZUGEORDNET, 2))
        self.assertEqual((services.offen(self.r1), services.offen(self.r2)), (Decimal("0.00"), Decimal("0.00")))
        z = self.l1.zahlung
        self.assertEqual((z.beleg, z.betrag, z.datum, z.zahlart), (self.r1, Decimal("119.00"), date(2026, 11, 17), Zahlart.LASTSCHRIFT))
        self.assertContains(self.client.get(reverse("finanzen:lastschrift")), "Zahlung gebucht am 17.11.2026")
        self.assertFalse(bank.zuordnen_automatisch(self.umsatz("238.00", tag=date(2026, 11, 18))))  # nicht noch einmal

    def test_summe_oder_datum_passt_nicht_bleibt_offen(self):
        u = self.umsatz("200.00")
        self.assertFalse(bank.zuordnen_automatisch(u))
        spaet = self.umsatz("238.00", tag=date(2026, 12, 20))
        self.assertFalse(bank.zuordnen_automatisch(spaet))
        self.assertEqual(services.offen(self.r1), Decimal("119.00"))
        mit_referenz = self.umsatz("238.00", tag=date(2026, 12, 21), referenz="DATEI-A-FRST")  # Referenz der Datei: Datum egal
        self.assertTrue(bank.zuordnen_automatisch(mit_referenz))

    def test_zwei_gleiche_sammelbuchungen_nur_mit_referenz(self):
        self.lastschrift(self.r3, datei="DATEI-B")
        r4 = self.rechnung()
        self.lastschrift(r4, datei="DATEI-B")
        u = self.umsatz("238.00")
        self.assertFalse(bank.zuordnen_automatisch(u))  # zwei Dateien mit gleicher Summe: nicht raten
        v = self.umsatz("238.00", referenz="DATEI-B-FRST")
        self.assertTrue(bank.zuordnen_automatisch(v))
        self.assertEqual((services.offen(self.r3), services.offen(self.r1)), (Decimal("0.00"), Decimal("119.00")))

    def test_ruecklastschrift_storniert_zahlung_und_hebt_auf(self):
        gut = self.umsatz("238.00")
        bank.zuordnen_automatisch(gut)
        rueck = self.umsatz("-123.50", tag=date(2026, 11, 19), zweck=f"Rueckgabe {self.r1.nummer} mangels Deckung")  # 119 + 4,50 Gebühr
        self.assertTrue(bank.zuordnen_automatisch(rueck))
        rueck.refresh_from_db(), gut.refresh_from_db(), self.l1.refresh_from_db(), self.l2.refresh_from_db()
        self.assertEqual((rueck.status, rueck.vorschlag, gut.status), (Bankumsatz.ZUGEORDNET, self.r1, Bankumsatz.ZUGEORDNET))
        self.assertTrue(self.l1.aufgehoben and not self.l2.aufgehoben)
        self.assertIn("Rücklastschrift", self.l1.aufgehoben_grund)
        self.assertEqual((services.offen(self.r1), services.offen(self.r2)), (Decimal("119.00"), Decimal("0.00")))
        self.assertIn(self.r1, [z["beleg"] for z in services.offene_rechnungen(self.m)])  # wieder offen: neuer Einzug oder Mahnung
        self.assertContains(self.client.get(reverse("finanzen:lastschrift")), self.r1.nummer)

    def test_einzelne_gutschrift_bleibt_nach_ruecklastschrift_erledigt(self):
        self.l2.aufgehoben = True
        self.l2.save()
        gut = self.umsatz("119.00", zweck=f"Rechnung {self.r1.nummer}")
        bank.zuordnen_automatisch(gut)
        bank.zuordnen_automatisch(self.umsatz("-119.00", tag=date(2026, 11, 19), referenz=self.r1.nummer))
        gut.refresh_from_db()
        self.assertEqual((gut.status, services.offen(self.r1)), (Bankumsatz.ZUGEORDNET, Decimal("119.00")))
        self.assertEqual(bank.vorschlaege_nachbuchen(self.m), 0)  # die alte Gutschrift wird nicht noch einmal gebucht
        self.assertEqual(services.offen(self.r1), Decimal("119.00"))

    def test_ruecklastschrift_vor_der_gutschrift_wartet(self):
        rueck = self.umsatz("-119.00", tag=date(2026, 11, 19), zweck=self.r1.nummer)
        self.assertFalse(bank.zuordnen_automatisch(rueck))
        rueck.refresh_from_db()
        self.assertEqual((rueck.status, rueck.vorschlag), (Bankumsatz.OFFEN, self.r1))
        self.umsatz("238.00")
        self.assertEqual(bank.vorschlaege_nachbuchen(self.m), 2)
        rueck.refresh_from_db(), self.l1.refresh_from_db()
        self.assertEqual((rueck.status, self.l1.aufgehoben, services.offen(self.r1)), (Bankumsatz.ZUGEORDNET, True, Decimal("119.00")))

    def test_sonstige_abbuchung_wird_weiter_ignoriert(self):
        u = self.umsatz("-50.00", zweck="Miete November")
        self.assertFalse(bank.zuordnen_automatisch(u))
        u.refresh_from_db()
        self.assertEqual(u.status, Bankumsatz.IGNORIERT)

    def test_aufheben_von_hand_storniert_gebuchte_zahlung(self):
        bank.zuordnen_automatisch(self.umsatz("238.00"))
        self.client.post(reverse("finanzen:lastschrift_aufheben", args=[self.l2.pk]), {"grund": "Rücklastschrift laut Bank"})
        self.l2.refresh_from_db()
        self.assertTrue(self.l2.aufgehoben)
        self.assertEqual(services.offen(self.r2), Decimal("119.00"))
        self.client.post(reverse("finanzen:lastschrift_aufheben", args=[self.l2.pk]), {"grund": "nochmal"})  # nur einmal
        self.assertEqual(self.r2.zahlungen.count(), 2)

    def test_camt_import_mit_referenzen(self):
        ergebnis = bank.importiere(self.m, "auszug.xml", CAMT.format(nummer=self.r2.nummer).encode())
        self.assertEqual((ergebnis["neu"], ergebnis["automatisch"]), (2, 2))
        self.assertEqual(Bankumsatz.objects.get(betrag__gt=0).referenz, "DATEI-A-FRST")
        self.l2.refresh_from_db()
        self.assertTrue(self.l2.aufgehoben)
        self.assertEqual((services.offen(self.r1), services.offen(self.r2)), (Decimal("0.00"), Decimal("119.00")))
