from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.kern.models import Mandant
from apps.stammdaten.models import Kunde, Zahlungsbedingung
from apps.verkauf.models import Beleg, Belegart, Position

from . import services
from .models import Bankumsatz, Kassenbuch, Mahnung, Zahlung

from .pdf import mahnung_pdf


@override_settings(SECURE_SSL_REDIRECT=False)
class FinanzenTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.kunde = Kunde.objects.create(nummer="1", name1="Kunde GmbH", email="k@example.test")
        self.zb = Zahlungsbedingung.objects.create(bezeichnung="10 Tage", tage_netto=10, tage_mahnung=11,
                                                   druck_text="Zahlbar bis ff.ff.ffff")

    def rechnung(self, tage_alt=0, netto="100"):
        b = Beleg.objects.create(mandant=self.m, art=Belegart.RECHNUNG, kunde=self.kunde, zahlungsbedingung=self.zb,
                                 belegdatum=date.today() - timedelta(days=tage_alt))
        Position.objects.create(beleg=b, bezeichnung="Dienst", einzelpreis=Decimal(netto))
        b.abschliessen()
        b.refresh_from_db()
        return b

    def test_offen_zahlung_teilzahlung_gutschrift(self):
        b = self.rechnung()
        self.assertEqual(services.offen(b), Decimal("119.00"))
        services.zahlung_buchen(b, Decimal("19"), date.today(), "ue")
        self.assertEqual(services.offen(b), Decimal("100.00"))
        g = b.gutschrift_erzeugen()
        self.assertEqual(services.offen(b), Decimal("100.00"))  # Entwurf zählt nicht
        g.abschliessen()
        self.assertEqual(services.offen(b), Decimal("-19.00"))  # Gutschrift über alles: 19 € zu viel gezahlt
        self.assertEqual(len(services.offene_rechnungen(self.m)), 1)

    def test_zahlung_unveraenderlich_und_nur_auf_abgeschlossene_rechnung(self):
        b = self.rechnung()
        z = services.zahlung_buchen(b, Decimal("10"), date.today(), "ba")
        with self.assertRaises(PermissionError):
            z.delete()
        offen = Beleg.objects.create(mandant=self.m, art=Belegart.RECHNUNG, kunde=self.kunde)
        with self.assertRaises(ValueError):
            services.zahlung_buchen(offen, Decimal("1"), date.today(), "ba")

    def test_mahnvorschlag_und_stufen(self):
        neu = self.rechnung(tage_alt=5)
        alt = self.rechnung(tage_alt=30)
        v = services.mahnvorschlag(self.m)
        self.assertEqual([z["beleg"] for z in v], [alt])  # die neue ist noch nicht mahnfällig
        self.assertEqual(v[0]["stufe"].stufe, 1)
        mahnung = services.mahnung_erstellen(alt, 1)
        self.assertEqual(mahnung.frist, date.today() + timedelta(days=7))
        self.assertEqual(services.mahnvorschlag(self.m), [])  # Stufe 2 erst nach 7 Tagen
        spaeter = date.today() + timedelta(days=8)
        v = services.mahnvorschlag(self.m, spaeter)
        self.assertEqual((v[0]["stufe"].stufe, v[0]["stufe"].gebuehr), (2, Decimal("5.00")))
        with self.assertRaises(Exception):
            services.mahnung_erstellen(alt, 1)  # Stufe nur einmal
        services.zahlung_buchen(alt, Decimal("119"), date.today(), "ue")
        self.assertEqual([z["beleg"] for z in services.mahnvorschlag(self.m, spaeter)], [neu])  # bezahlt -> keine Mahnung
        self.assertTrue(mahnung_pdf(mahnung).startswith(b"%PDF"))
        self.assertIn("Rechnung " + alt.nummer, mahnung_text(mahnung))

    def test_kontoauszug_und_kreditlimit(self):
        b = self.rechnung()
        services.zahlung_buchen(b, Decimal("50"), date.today(), "ue")
        zeilen, saldo = services.kontoauszug(self.m, self.kunde)
        self.assertEqual((len(zeilen), saldo), (2, Decimal("69.00")))
        self.assertEqual(services.kunde_offen(self.m, self.kunde), Decimal("69.00"))

    def test_masken(self):
        user = get_user_model().objects.create_user("t", password="x" * 14)
        self.client.force_login(user)
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()
        b = self.rechnung(tage_alt=30)
        self.assertContains(self.client.get(reverse("finanzen:offene_posten")), b.nummer)
        self.client.post(reverse("finanzen:zahlung", args=[b.pk]), {"betrag": "19,00", "datum": date.today().isoformat(), "zahlart": "ue"})
        self.assertEqual(Zahlung.objects.get().betrag, Decimal("19.00"))
        self.assertContains(self.client.get(reverse("finanzen:mahnwesen")), b.nummer)
        self.client.post(reverse("finanzen:mahnungen_erstellen"), {"auswahl": f"{b.pk}:1"})
        m = Mahnung.objects.get()
        self.assertEqual(self.client.get(reverse("finanzen:mahnung_pdf", args=[m.pk]))["Content-Type"], "application/pdf")
        self.assertContains(self.client.get(reverse("finanzen:kontoauszug", args=[self.kunde.pk])), "Saldo")
        self.assertContains(self.client.get(reverse("verkauf:kunde", args=[self.kunde.pk])), "Offene Posten")
        self.assertContains(self.client.get(reverse("verkauf:beleg", args=[b.pk])), "Zahlungen / offene Posten")
        self.assertContains(self.client.get(reverse("finanzen:mahnung_mail", args=[m.pk])), "Zahlungserinnerung")


from .mahntext import text_fuer as mahnung_text  # noqa: E402


@override_settings(SECURE_SSL_REDIRECT=False)
class BuchhaltungTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer", datev_berater="12345", datev_mandant="1")
        self.kunde = Kunde.objects.create(nummer="10072", name1="Kunde GmbH")
        self.zb = Zahlungsbedingung.objects.create(bezeichnung="10 Tage", tage_netto=10, tage_mahnung=11)

    def rechnung(self, netto="100"):
        b = Beleg.objects.create(mandant=self.m, art=Belegart.RECHNUNG, kunde=self.kunde, zahlungsbedingung=self.zb)
        Position.objects.create(beleg=b, bezeichnung="Dienst", einzelpreis=Decimal(netto))
        b.abschliessen()
        b.refresh_from_db()
        return b

    def test_csv_import_automatische_zuordnung_und_dubletten(self):
        from . import bank
        a, b = self.rechnung(), self.rechnung("200")
        csv_text = (
            "Kontostand;123\n\nBuchungstag;Valuta;Name Zahlungsbeteiligter;IBAN Zahlungsbeteiligter;Verwendungszweck;Betrag\n"
            f"24.10.2026;24.10.2026;Kunde GmbH;DE02120300000000202051;Rechnung {a.nummer} danke;119,00\n"
            f"24.10.2026;24.10.2026;Kunde GmbH;DE02120300000000202051;Zahlung {b.nummer};100,00\n"
            "24.10.2026;24.10.2026;Miete;DE00;Miete Oktober;-1.500,00\n"
            "24.10.2026;24.10.2026;Unbekannt;DE11;ohne Bezug;50,00\n")
        erg = bank.importiere(self.m, "auszug.csv", csv_text.encode("cp1252"))
        self.assertEqual(erg, {"gelesen": 4, "neu": 4, "doppelt": 0, "automatisch": 1})
        self.assertEqual(services.offen(a), Decimal("0.00"))
        self.assertEqual(services.offen(b), Decimal("238.00"))  # Betrag weicht ab -> nur Vorschlag
        teil = Bankumsatz.objects.get(betrag=Decimal("100.00"))
        self.assertEqual((teil.status, teil.vorschlag), ("offen", b))
        self.assertEqual(Bankumsatz.objects.get(betrag=Decimal("-1500.00")).status, "ignoriert")
        self.assertEqual(bank.importiere(self.m, "auszug.csv", csv_text.encode("cp1252"))["doppelt"], 4)
        bank.zuordnen(teil, b)  # manuell bestätigen
        self.assertEqual(services.offen(b), Decimal("138.00"))
        with self.assertRaises(ValueError):
            bank.zuordnen(teil, b)

    def test_camt_import(self):
        from . import bank
        a = self.rechnung()
        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.02"><BkToCstmrStmt><Stmt>
<Ntry><Amt Ccy="EUR">119.00</Amt><CdtDbtInd>CRDT</CdtDbtInd><BookgDt><Dt>2026-10-24</Dt></BookgDt>
<NtryDtls><TxDtls><RltdPties><Dbtr><Nm>Kunde GmbH</Nm></Dbtr><DbtrAcct><Id><IBAN>DE02120300000000202051</IBAN></Id></DbtrAcct></RltdPties>
<RmtInf><Ustrd>{a.nummer}</Ustrd></RmtInf></TxDtls></NtryDtls></Ntry>
<Ntry><Amt Ccy="EUR">30.00</Amt><CdtDbtInd>DBIT</CdtDbtInd><BookgDt><Dt>2026-10-24</Dt></BookgDt></Ntry>
</Stmt></BkToCstmrStmt></Document>""".encode()
        erg = bank.importiere(self.m, "camt.xml", xml)
        self.assertEqual((erg["neu"], erg["automatisch"]), (2, 1))
        self.assertEqual(Bankumsatz.objects.get(zahlung__isnull=False).name, "Kunde GmbH")

    def test_kassenbuch_barzahlung_und_unveraenderlich(self):
        b = self.rechnung()
        services.zahlung_buchen(b, Decimal("119"), date.today(), "ba")
        services.kasse_buchen(self.m, "A", Decimal("19"), "Porto")
        self.assertEqual([k.nummer for k in Kassenbuch.objects.order_by("nummer")], [1, 2])
        self.assertEqual(services.kassenstand(self.m), Decimal("100.00"))
        with self.assertRaises(PermissionError):
            Kassenbuch.objects.first().delete()

    def test_datev_export(self):
        from . import datev
        b = self.rechnung()
        services.zahlung_buchen(b, Decimal("119"), date.today(), "ue")
        daten = datev.export(self.m, date.today().replace(day=1), date.today()).decode("cp1252").splitlines()
        self.assertTrue(daten[0].startswith('EXTF;700;21;Buchungsstapel'))
        self.assertIn("12345", daten[0])
        self.assertEqual(daten[2].split(";")[:9], ["119,00", "S", "EUR", "", "", "", "10072", "8400", ""])
        self.assertEqual(daten[3].split(";")[:8], ["119,00", "S", "EUR", "", "", "", "1200", "10072"])

    def test_masken(self):
        user = get_user_model().objects.create_user("t", password="x" * 14)
        self.client.force_login(user)
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()
        for name in ("bank", "kassenbuch", "datev"):
            self.assertEqual(self.client.get(reverse(f"finanzen:{name}")).status_code, 200)
        r = self.client.post(reverse("finanzen:datev"), {"von": "2026-01-01", "bis": "2026-12-31"})
        self.assertIn("EXTF_Buchungsstapel_OKAY", r["Content-Disposition"])
        self.client.post(reverse("finanzen:kassenbuch"), {"art": "E", "betrag": "10,50", "text": "Test"})
        self.assertEqual(Kassenbuch.objects.get().betrag, Decimal("10.50"))
