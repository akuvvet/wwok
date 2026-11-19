"""Lieferanten-Gutschrift (Entscheidung Nutzer 19.11.2026): Belegdaten erfassen, mit Lieferantenrechnungen verrechnen (auch
teilweise), Zahlung des Lieferanten buchen (auch aus dem Bankabgleich), Rückgängig und Storno mit Pflichtgrund."""
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from decimal import Decimal as D

from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.finanzen import bank
from apps.finanzen.models import Bankumsatz
from apps.kern.models import Mandant
from apps.stammdaten.models import Artikel, Lieferant, Steuersatz

from . import sepa, services
from .models import (Lieferantengutschrift, LieferantengutschriftVerrechnung, LieferantengutschriftZahlung, Lieferantenrechnung,
                     RechnungStatus, Ruecksendung, RuecksendungStatus)

HEUTE = date.today()


def meldungen(antwort):
    return " | ".join(str(m) for m in get_messages(antwort.wsgi_request))


@override_settings(SECURE_SSL_REDIRECT=False)
class LieferantengutschriftTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer", iban="DE02120300000000202051")
        self.m2 = Mandant.objects.create(kurzname="ZONE", firmenname="ZONE Handel")
        self.lf = Lieferant.objects.create(nummer="L1", name1="Techdata GmbH", kurzname="TD", iban="DE89370400440532013000")
        self.lf2 = Lieferant.objects.create(nummer="L2", name1="Billig AG", kurzname="BAG", iban="DE02500105170137075030")
        self.maus = Artikel.objects.create(nummer="M1", bezeichnung="Maus", steuersatz=Steuersatz.objects.get(nummer=1), vk1=D("15"))
        self.admin = get_user_model().objects.create_superuser("chef", password="x" * 14)
        self.normal = get_user_model().objects.create_user("mitarbeiter", password="x" * 14)

    # ---- Hilfen --------------------------------------------------------------------------------
    def anmelden(self, benutzer=None, mandant=None):
        self.client.force_login(benutzer or self.admin)
        s = self.client.session
        s["mandant_id"] = (mandant or self.m).pk
        s.save()

    def rechnung(self, nummer="R1", brutto=D("119.00"), lieferant=None, mandant=None, **zusatz):
        netto = (brutto / D("1.19")).quantize(D("0.01"))
        return Lieferantenrechnung.objects.create(
            mandant=mandant or self.m, lieferant=lieferant or self.lf, rechnungsnummer=nummer, datum=HEUTE, faellig=HEUTE + timedelta(days=14),
            netto=netto, steuer=brutto - netto, brutto=brutto, **zusatz)

    def gutschrift(self, nummer="G1", brutto=D("119.00"), lieferant=None, mandant=None):
        netto = (brutto / D("1.19")).quantize(D("0.01"))
        return services.gutschrift_erfassen(mandant or self.m, lieferant or self.lf, nummer, HEUTE, netto, brutto - netto, brutto, "", None, self.admin)

    def ruecksendung(self):
        we = services.wareneingang_anlegen(self.m, self.lf)
        p = services.wareneingang_erfassen(we, self.maus, D("10"), D("9"))
        services.wareneingang_buchen(we, bestaetigt=True)
        return services.ruecksendung_anlegen(we, [(p, D("5"), [])], "gutschrift", "zu viel")

    # ---- Erfassen ------------------------------------------------------------------------------
    def test_erfassen_normalfall(self):
        g = services.gutschrift_erfassen(self.m, self.lf, " G-1 ", HEUTE, D("100"), D("19"), D("119"), "Notiz", None, self.admin)
        self.assertEqual((g.nummer, g.netto, g.steuer, g.brutto, g.restguthaben), ("G-1", D("100.00"), D("19.00"), D("119.00"), D("119.00")))
        self.assertIn("erfasst", g.historie)

    def test_pflichtfelder(self):
        f = (self.m, self.lf)
        for args, text in (
                (("", HEUTE, D("1"), D("0.19"), D("1.19")), "Gutschrift-Nr."),
                (("G", None, D("1"), D("0.19"), D("1.19")), "Gutschriftdatum"),
                (("G", HEUTE, None, D("0.19"), D("1.19")), "Netto"),
                (("G", HEUTE, D("1"), None, D("1.19")), "USt"),
                (("G", HEUTE, D("1"), D("0.19"), None), "Brutto"),
                (("G", HEUTE, D("0"), D("0"), D("0")), "größer als 0"),
                (("G", HEUTE, D("-1"), D("0"), D("-1")), "größer als 0"),
                (("G", HEUTE, D("1"), D("-0.1"), D("0.9")), "mindestens 0")):
            with self.subTest(text), self.assertRaisesMessage(ValueError, text):
                services.gutschrift_erfassen(*f, *args)
        self.assertFalse(Lieferantengutschrift.objects.exists())

    def test_brutto_pruefung_auf_einen_cent(self):
        with self.assertRaisesMessage(ValueError, "Netto plus USt ergibt nicht den Bruttobetrag"):
            services.gutschrift_erfassen(self.m, self.lf, "G1", HEUTE, D("100"), D("19"), D("119.02"))
        g = services.gutschrift_erfassen(self.m, self.lf, "G2", HEUTE, D("100"), D("19"), D("119.01"))  # 1 Cent Abweichung ist erlaubt
        self.assertEqual(g.brutto, D("119.01"))

    def test_datum_nicht_in_der_zukunft(self):
        with self.assertRaisesMessage(ValueError, "Zukunft"):
            services.gutschrift_erfassen(self.m, self.lf, "G1", HEUTE + timedelta(days=1), D("100"), D("19"), D("119"))

    def test_doppelte_nummer_je_mandant_und_lieferant(self):
        self.gutschrift("G1")
        with self.assertRaisesMessage(ValueError, "bereits erfasst"):
            self.gutschrift("g1")  # Groß-/Kleinschreibung zählt nicht
        self.gutschrift("G1", lieferant=self.lf2)  # anderer Lieferant: erlaubt
        self.gutschrift("G1", mandant=self.m2)  # anderer Mandant: erlaubt
        self.assertEqual(Lieferantengutschrift.objects.count(), 3)

    def test_zu_grosser_betrag(self):
        with self.assertRaisesMessage(ValueError, "zu groß"):
            services.gutschrift_erfassen(self.m, self.lf, "G1", HEUTE, D("10000000000"), D("0"), D("10000000000"))

    def test_ruecksendung_gutschrift_legt_gutschrift_an(self):
        r = self.ruecksendung()
        r = services.ruecksendung_gutschrift(r, "G-9", HEUTE, D("45"), D("8.55"), D("53.55"), "Überlieferung", self.admin)
        g = r.lieferantengutschrift
        self.assertEqual((r.status, r.gutschrift_betrag, g.nummer, g.brutto, g.notiz), (RuecksendungStatus.GUTSCHRIFT, D("45.00"), "G-9", D("53.55"), "Überlieferung"))
        with self.assertRaisesMessage(ValueError, "schon erledigt"):  # nicht zweimal
            services.ruecksendung_gutschrift(r, "G-10", HEUTE, D("45"), D("8.55"), D("53.55"))

    def test_ruecksendung_fehler_aendert_nichts(self):
        r = self.ruecksendung()
        with self.assertRaisesMessage(ValueError, "Gutschrift-Nr."):
            services.ruecksendung_gutschrift(r, "", HEUTE, D("45"), D("8.55"), D("53.55"))
        r.refresh_from_db()
        self.assertEqual(r.status, RuecksendungStatus.ANGELEGT)
        self.assertIsNone(r.gutschrift_betrag)
        self.assertFalse(Lieferantengutschrift.objects.exists())

    def test_gutschrift_einer_fremden_ruecksendung_abgewiesen(self):
        r = self.ruecksendung()
        with self.assertRaisesMessage(ValueError, "Rücksendung gehört nicht"):
            services.gutschrift_erfassen(self.m, self.lf2, "G1", HEUTE, D("1"), D("0.19"), D("1.19"), ruecksendung=r)
        with self.assertRaisesMessage(ValueError, "Rücksendung gehört nicht"):
            services.gutschrift_erfassen(self.m2, self.lf, "G1", HEUTE, D("1"), D("0.19"), D("1.19"), ruecksendung=r)

    # ---- Verrechnen --------------------------------------------------------------------------
    def test_teilverrechnung_senkt_zahlbetrag(self):
        r, g = self.rechnung("R1", D("119.00")), self.gutschrift("G1", D("50.00"))
        v = services.gutschrift_verrechnen(g, r, D("50.00"), None, self.admin)
        r.refresh_from_db()
        r = Lieferantenrechnung.objects.get(pk=r.pk)
        self.assertEqual((v.betrag, r.status, r.zahlbetrag(), r.verrechnet, r.offener_betrag), (D("50.00"), RechnungStatus.OFFEN, D("69.00"), D("50.00"), D("69.00")))
        self.assertEqual(g.restguthaben, D("0.00"))
        self.assertIn("50.00 € mit Gutschrift G1 verrechnet", r.historie)
        self.assertIn("mit Rechnung R1 verrechnet", Lieferantengutschrift.objects.get(pk=g.pk).historie)

    def test_teilverrechnung_der_gutschrift_auf_zwei_rechnungen(self):
        r1, r2, g = self.rechnung("R1", D("40.00")), self.rechnung("R2", D("100.00")), self.gutschrift("G1", D("100.00"))
        services.gutschrift_verrechnen(g, r1, D("40.00"))
        services.gutschrift_verrechnen(g, r2, D("60.00"))
        g = Lieferantengutschrift.objects.get(pk=g.pk)
        self.assertEqual((g.verrechnet, g.restguthaben), (D("100.00"), D("0.00")))
        self.assertEqual(Lieferantenrechnung.objects.get(pk=r1.pk).status, RechnungStatus.BEZAHLT)
        self.assertEqual(Lieferantenrechnung.objects.get(pk=r2.pk).zahlbetrag(), D("40.00"))
        with self.assertRaisesMessage(ValueError, "Höchstens 0.00 €"):  # nichts mehr übrig
            services.gutschrift_verrechnen(g, r2, D("1"))

    def test_vollverrechnung_gilt_als_bezahlt(self):
        r, g = self.rechnung("R1", D("119.00")), self.gutschrift("G1", D("200.00"))
        services.gutschrift_verrechnen(g, r, D("119.00"), HEUTE - timedelta(days=1), self.admin)
        r = Lieferantenrechnung.objects.get(pk=r.pk)
        self.assertEqual((r.status, r.bezahlt_am, r.bezahlter_betrag, r.zahlbetrag()), (RechnungStatus.BEZAHLT, HEUTE - timedelta(days=1), D("0"), D("0")))
        self.assertTrue(r.durch_verrechnung_bezahlt)
        self.assertIn("durch Verrechnung vollständig bezahlt", r.historie)
        self.assertEqual(Lieferantengutschrift.objects.get(pk=g.pk).restguthaben, D("81.00"))  # Restguthaben bleibt offen

    def test_betragsgrenzen(self):
        r, g = self.rechnung("R1", D("100.00")), self.gutschrift("G1", D("59.50"))
        for betrag, text in ((None, "größer als 0"), (D("0"), "größer als 0"), (D("-5"), "größer als 0"), (D("NaN"), "größer als 0"),
                             (D("59.51"), "Restguthaben"), (D("100.01"), "Restguthaben")):
            with self.subTest(betrag), self.assertRaisesMessage(ValueError, text):
                services.gutschrift_verrechnen(g, r, betrag)
        g2 = self.gutschrift("G2", D("300.00"))
        with self.assertRaisesMessage(ValueError, "offener Betrag der Rechnung"):
            services.gutschrift_verrechnen(g2, r, D("100.01"))
        self.assertFalse(LieferantengutschriftVerrechnung.objects.exists())

    def test_zukuenftiges_datum_abgewiesen(self):
        r, g = self.rechnung(), self.gutschrift()
        with self.assertRaisesMessage(ValueError, "Zukunft"):
            services.gutschrift_verrechnen(g, r, D("10"), HEUTE + timedelta(days=1))

    def test_nur_offene_rechnungen(self):
        g = self.gutschrift("G1", D("500.00"))
        bezahlt, exportiert = self.rechnung("R1", status=RechnungStatus.BEZAHLT), self.rechnung("R2", status=RechnungStatus.EXPORTIERT)
        with self.assertRaisesMessage(ValueError, "schon bezahlt"):
            services.gutschrift_verrechnen(g, bezahlt, D("10"))
        with self.assertRaisesMessage(ValueError, "SEPA"):
            services.gutschrift_verrechnen(g, exportiert, D("10"))
        self.assertEqual(services.verrechenbare_rechnungen(g), [])

    def test_fremder_lieferant_und_mandant_abgewiesen(self):
        g = self.gutschrift("G1", D("500.00"))
        for fremd in (self.rechnung("R1", lieferant=self.lf2), self.rechnung("R2", mandant=self.m2)):
            with self.assertRaisesMessage(ValueError, "nicht zum selben Lieferanten und Mandanten"):
                services.gutschrift_verrechnen(g, fremd, D("10"))
        self.assertEqual(services.verrechenbare_rechnungen(g), [])

    def test_offene_abweichung_sperrt_verrechnung(self):
        from .models import Wareneingang  # noqa: F401
        we = services.wareneingang_anlegen(self.m, self.lf)
        services.wareneingang_erfassen(we, self.maus, D("10"), D("9"))
        services.wareneingang_buchen(we, bestaetigt=True)
        r = self.rechnung("R1", D("595.00"))  # Wareneingang 90 netto, Rechnung 500 netto
        r.wareneingaenge.set([we])
        with self.assertRaisesMessage(ValueError, "weicht"):
            services.gutschrift_verrechnen(self.gutschrift(), r, D("10"))

    def test_skonto_bezieht_sich_auf_rechnungsbrutto(self):
        r = self.rechnung("R1", D("100.00"), skonto_prozent=D("2"), skonto_bis=HEUTE + timedelta(days=5))
        g = self.gutschrift("G1", D("100.00"))
        self.assertEqual(r.zahlbetrag(), D("98.00"))
        services.gutschrift_verrechnen(g, r, D("30.00"))
        r = Lieferantenrechnung.objects.get(pk=r.pk)
        self.assertEqual(r.zahlbetrag(), D("68.00"))  # 98 − 30
        self.assertEqual(r.zahlbetrag(HEUTE + timedelta(days=9)), D("70.00"))  # nach der Skontofrist: 100 − 30
        with self.assertRaisesMessage(ValueError, "unter dem Skontobetrag"):  # Rest 1 € läge unter dem Skonto: 98 − 99 < 0
            services.gutschrift_verrechnen(g, r, D("69.00"))
        services.gutschrift_verrechnen(g, r, D("70.00"))  # voll: bezahlt
        self.assertEqual(Lieferantenrechnung.objects.get(pk=r.pk).status, RechnungStatus.BEZAHLT)

    def test_zahlbetrag_nie_unter_null(self):
        r = self.rechnung("R1", D("100.00"), skonto_prozent=D("2"), skonto_bis=HEUTE + timedelta(days=5))
        LieferantengutschriftVerrechnung.objects.create(gutschrift=self.gutschrift("G1", D("100")), rechnung=r, betrag=D("99.50"))
        self.assertEqual(Lieferantenrechnung.objects.get(pk=r.pk).zahlbetrag(), D("0"))

    def test_sepa_betrag_sinkt_und_vollverrechnete_fehlen(self):
        r1, r2, g = self.rechnung("R1", D("119.00")), self.rechnung("R2", D("50.00")), self.gutschrift("G1", D("169.00"))
        services.gutschrift_verrechnen(g, r1, D("19.00"))
        services.gutschrift_verrechnen(g, r2, D("50.00"))  # voll verrechnet = bezahlt
        self.anmelden()
        antwort = self.client.post(reverse("einkauf:sepa"), {"auswahl": [r1.pk, r2.pk], "ausfuehrung": HEUTE.isoformat()})
        wurzel = ET.fromstring(antwort.content)
        ns = {"p": sepa.NS}
        self.assertEqual(wurzel.find(".//p:GrpHdr/p:CtrlSum", ns).text, "100.00")
        self.assertEqual(wurzel.find(".//p:GrpHdr/p:NbOfTxs", ns).text, "1")
        self.assertIn("abzgl. Gutschrift 19.00 EUR", wurzel.find(".//p:RmtInf/p:Ustrd", ns).text)
        self.assertEqual(Lieferantenrechnung.objects.get(pk=r1.pk).status, RechnungStatus.EXPORTIERT)

    def test_als_bezahlt_buchen_schlaegt_den_restbetrag_vor(self):
        r, g = self.rechnung("R1", D("119.00")), self.gutschrift("G1", D("19.00"))
        services.gutschrift_verrechnen(g, r, D("19.00"))
        self.anmelden()
        seite = self.client.get(reverse("einkauf:rechnung", args=[r.pk]))
        self.assertContains(seite, 'name="betrag" value="100,00"')
        self.client.post(reverse("einkauf:rechnung_bezahlt", args=[r.pk]), {"datum": HEUTE.isoformat()})
        r = Lieferantenrechnung.objects.get(pk=r.pk)
        self.assertEqual((r.status, r.bezahlter_betrag), (RechnungStatus.BEZAHLT, D("100.00")))
        self.assertFalse(r.durch_verrechnung_bezahlt)

    # ---- Rückgängig ----------------------------------------------------------------------------
    def test_rueckgaengig_oeffnet_die_rechnung_wieder(self):
        r, g = self.rechnung("R1", D("119.00")), self.gutschrift("G1", D("119.00"))
        v = services.gutschrift_verrechnen(g, r, D("119.00"))
        with self.assertRaisesMessage(ValueError, "Grund"):
            services.verrechnung_aufheben(v, "  ", self.admin)
        services.verrechnung_aufheben(v, "falsche Rechnung", self.admin)
        r, g, v = Lieferantenrechnung.objects.get(pk=r.pk), Lieferantengutschrift.objects.get(pk=g.pk), LieferantengutschriftVerrechnung.objects.get(pk=v.pk)
        self.assertEqual((r.status, r.bezahlt_am, r.bezahlter_betrag, r.zahlbetrag()), (RechnungStatus.OFFEN, None, None, D("119.00")))
        self.assertEqual((g.restguthaben, v.aufgehoben, v.aufgehoben_grund, v.aufgehoben_von), (D("119.00"), True, "falsche Rechnung", self.admin))
        self.assertIn("rückgängig gemacht – falsche Rechnung", r.historie)  # Historie an beiden
        self.assertIn("rückgängig gemacht – falsche Rechnung", g.historie)
        with self.assertRaisesMessage(ValueError, "schon rückgängig"):
            services.verrechnung_aufheben(v, "nochmal")

    def test_rueckgaengig_teilverrechnung_laesst_rechnung_offen(self):
        r, g = self.rechnung("R1", D("119.00")), self.gutschrift("G1", D("19.00"))
        v = services.gutschrift_verrechnen(g, r, D("19.00"))
        services.verrechnung_aufheben(v, "Irrtum")
        self.assertEqual(Lieferantenrechnung.objects.get(pk=r.pk).zahlbetrag(), D("119.00"))

    def test_rueckgaengig_nicht_nach_export_oder_zahlung(self):
        r, g = self.rechnung("R1", D("119.00")), self.gutschrift("G1", D("19.00"))
        v = services.gutschrift_verrechnen(g, r, D("19.00"))
        Lieferantenrechnung.objects.filter(pk=r.pk).update(status=RechnungStatus.EXPORTIERT)
        with self.assertRaisesMessage(ValueError, "exportiert"):
            services.verrechnung_aufheben(v, "x")
        Lieferantenrechnung.objects.filter(pk=r.pk).update(status=RechnungStatus.BEZAHLT, bezahlt_am=HEUTE, bezahlter_betrag=D("100"))
        with self.assertRaisesMessage(ValueError, "anders bezahlt"):
            services.verrechnung_aufheben(v, "x")
        self.assertFalse(LieferantengutschriftVerrechnung.objects.get(pk=v.pk).aufgehoben)

    def test_rechnung_schutz_vor_korrektur_loeschen_und_zahlungsstorno(self):
        r, g = self.rechnung("R1", D("119.00")), self.gutschrift("G1", D("119.00"))
        v = services.gutschrift_verrechnen(g, r, D("100.00"))
        r = Lieferantenrechnung.objects.get(pk=r.pk)
        with self.assertRaisesMessage(ValueError, "unter den schon verrechneten"):
            r.betraege_korrigieren(D("50"), D("9.50"), D("59.50"))
        with self.assertRaisesMessage(ValueError, "verrechnet"):
            services.rechnung_loeschen(r, "weg")
        services.gutschrift_verrechnen(g, r, D("19.00"))  # jetzt voll
        r = Lieferantenrechnung.objects.get(pk=r.pk)
        with self.assertRaisesMessage(ValueError, "Verrechnung rückgängig"):
            r.zahlung_stornieren("x")
        for verr in LieferantengutschriftVerrechnung.objects.filter(aufgehoben=False):
            services.verrechnung_aufheben(verr, "alles zurück")
        r = Lieferantenrechnung.objects.get(pk=r.pk)
        with self.assertRaisesMessage(ValueError, "Nachweis"):  # aufgehobene Verrechnungen bleiben stehen
            services.rechnung_loeschen(r, "weg")
        self.assertEqual(v.pk, LieferantengutschriftVerrechnung.objects.first().pk)

    # ---- Zahlung des Lieferanten ---------------------------------------------------------------
    def test_zahlung_buchen_und_restguthaben(self):
        g = self.gutschrift("G1", D("119.00"))
        z = services.gutschrift_zahlung_buchen(g, D("100.00"), HEUTE, self.admin)
        g = Lieferantengutschrift.objects.get(pk=g.pk)
        self.assertEqual((z.betrag, g.eingegangen, g.restguthaben), (D("100.00"), D("100.00"), D("19.00")))
        self.assertIn("Zahlung des Lieferanten 100.00 € erhalten", g.historie)

    def test_zahlung_grenzen(self):
        g = self.gutschrift("G1", D("119.00"))
        for betrag, datum, text in ((None, HEUTE, "größer als 0"), (D("0"), HEUTE, "größer als 0"), (D("-1"), HEUTE, "größer als 0"),
                                    (D("119.01"), HEUTE, "Höchstens 119.00 €"), (D("1"), HEUTE + timedelta(days=1), "Zukunft")):
            with self.subTest(text=text, betrag=betrag), self.assertRaisesMessage(ValueError, text):
                services.gutschrift_zahlung_buchen(g, betrag, datum)
        self.assertFalse(LieferantengutschriftZahlung.objects.exists())

    def test_verrechnung_und_zahlung_zusammen_nicht_ueber_brutto(self):
        r, g = self.rechnung("R1", D("100.00")), self.gutschrift("G1", D("100.00"))
        services.gutschrift_verrechnen(g, r, D("60.00"))
        with self.assertRaisesMessage(ValueError, "Höchstens 40.00 €"):
            services.gutschrift_zahlung_buchen(g, D("50.00"), HEUTE)
        services.gutschrift_zahlung_buchen(g, D("40.00"), HEUTE)
        with self.assertRaisesMessage(ValueError, "Höchstens 0.00 €"):
            services.gutschrift_verrechnen(g, r, D("1"))

    def test_zahlung_storno_mit_pflichtgrund(self):
        g = self.gutschrift("G1", D("119.00"))
        z = services.gutschrift_zahlung_buchen(g, D("119.00"), HEUTE)
        with self.assertRaisesMessage(ValueError, "Stornogrund"):
            services.gutschrift_zahlung_stornieren(z, " ")
        services.gutschrift_zahlung_stornieren(z, "falsch gebucht", self.admin)
        g, z = Lieferantengutschrift.objects.get(pk=g.pk), LieferantengutschriftZahlung.objects.get(pk=z.pk)
        self.assertEqual((z.storniert, z.storno_grund, g.restguthaben), (True, "falsch gebucht", D("119.00")))
        self.assertIn("storniert – falsch gebucht", g.historie)
        with self.assertRaisesMessage(ValueError, "schon storniert"):
            services.gutschrift_zahlung_stornieren(z, "nochmal")

    # ---- Bankabgleich --------------------------------------------------------------------------
    def csv(self, betrag, name, zweck, datum=HEUTE):
        kopf = "Buchungstag;Betrag;Name Zahlungsbeteiligter;Verwendungszweck\n"
        return (kopf + f"{datum:%d.%m.%Y};{betrag};{name};{zweck}\n").encode()

    def test_bank_ordnet_eingang_mit_gutschriftnummer_automatisch_zu(self):
        g = self.gutschrift("G-4711", D("119.00"))
        erg = bank.importiere(self.m, "a.csv", self.csv("119,00", "Techdata GmbH", "Erstattung Gutschrift G-4711"))
        u = Bankumsatz.objects.get()
        self.assertEqual((erg["automatisch"], u.status), (1, Bankumsatz.ZUGEORDNET))
        z = LieferantengutschriftZahlung.objects.get()
        self.assertEqual((z.gutschrift, z.betrag, z.datum, z.bankumsatz), (g, D("119.00"), HEUTE, u))
        self.assertEqual(Lieferantengutschrift.objects.get(pk=g.pk).restguthaben, D("0.00"))

    def test_bank_nummer_muss_ganzes_wort_sein_und_betrag_passen(self):
        self.gutschrift("G1", D("119.00"))
        bank.importiere(self.m, "a.csv", self.csv("119,00", "Irgendwer", "Rechnung G12 und G1X"))  # G1 nur als Teilwort
        bank.importiere(self.m, "b.csv", self.csv("100,00", "Irgendwer", "Gutschrift G1"))  # Betrag ≠ Restguthaben
        self.assertFalse(LieferantengutschriftZahlung.objects.exists())
        self.assertEqual(Bankumsatz.objects.filter(status=Bankumsatz.OFFEN).count(), 2)

    def test_bank_nur_name_bleibt_vorschlag_und_wird_von_hand_gebucht(self):
        g = self.gutschrift("G1", D("119.00"))
        erg = bank.importiere(self.m, "a.csv", self.csv("119,00", "TECHDATA GmbH", "Überweisung"))
        u = Bankumsatz.objects.get()
        self.assertEqual((erg["automatisch"], u.status), (0, Bankumsatz.OFFEN))
        self.assertFalse(LieferantengutschriftZahlung.objects.exists())
        self.anmelden()
        seite = self.client.get(reverse("finanzen:bank"))
        self.assertContains(seite, "Auf Lieferanten-Gutschrift buchen")
        self.assertContains(seite, "(Vorschlag)")
        antwort = self.client.post(reverse("finanzen:bank_zuordnen", args=[u.pk]), {"gutschrift": g.pk}, follow=True)
        self.assertIn("Gebucht", meldungen(antwort))
        self.assertEqual(LieferantengutschriftZahlung.objects.get().bankumsatz_id, u.pk)
        self.assertContains(self.client.get(reverse("finanzen:bank"), {"status": "zugeordnet"}), "Lieferanten-Gutschrift")
        antwort = self.client.post(reverse("finanzen:bank_zuordnen", args=[u.pk]), {"gutschrift": g.pk}, follow=True)  # doppelt
        self.assertIn("bereits zugeordnet", meldungen(antwort))
        self.assertEqual(LieferantengutschriftZahlung.objects.count(), 1)

    def test_bank_teilbetrag_von_hand_und_ueberzahlung_abgewiesen(self):
        g = self.gutschrift("G1", D("119.00"))
        bank.importiere(self.m, "a.csv", self.csv("50,00", "Irgendwer", "Teilzahlung"))
        bank.importiere(self.m, "b.csv", self.csv("200,00", "Irgendwer", "zu viel"))
        klein, gross = Bankumsatz.objects.get(betrag=D("50.00")), Bankumsatz.objects.get(betrag=D("200.00"))
        self.anmelden()
        antwort = self.client.post(reverse("finanzen:bank_zuordnen", args=[gross.pk]), {"gutschrift": g.pk}, follow=True)
        self.assertIn("Höchstens 119.00 €", meldungen(antwort))
        self.client.post(reverse("finanzen:bank_zuordnen", args=[klein.pk]), {"gutschrift": g.pk})
        self.assertEqual(Lieferantengutschrift.objects.get(pk=g.pk).restguthaben, D("69.00"))
        self.assertEqual(Bankumsatz.objects.get(pk=gross.pk).status, Bankumsatz.OFFEN)

    def test_bank_abbuchung_und_fremder_mandant(self):
        self.gutschrift("G1", D("119.00"), mandant=self.m2)
        bank.importiere(self.m, "a.csv", self.csv("119,00", "Techdata GmbH", "Gutschrift G1"))  # Gutschrift gehört zum anderen Mandanten
        self.assertFalse(LieferantengutschriftZahlung.objects.exists())
        g = self.gutschrift("G2", D("119.00"))
        bank.importiere(self.m, "b.csv", self.csv("-119,00", "Techdata GmbH", "Gutschrift G2"))  # Abbuchung, kein Eingang
        self.assertFalse(LieferantengutschriftZahlung.objects.exists())
        u = Bankumsatz.objects.get(betrag=D("-119.00"))
        with self.assertRaisesMessage(ValueError, "Zahlungseingang"):
            services.gutschrift_zahlung_buchen(g, None, None, bankumsatz=u)
        self.anmelden()
        fremd = Lieferantengutschrift.objects.get(mandant=self.m2)
        eingang = Bankumsatz.objects.get(betrag=D("119.00"))
        antwort = self.client.post(reverse("finanzen:bank_zuordnen", args=[eingang.pk]), {"gutschrift": fremd.pk}, follow=True)
        self.assertIn("nicht gefunden", meldungen(antwort))

    def test_bank_storno_gibt_umsatz_wieder_frei(self):
        g = self.gutschrift("G-1", D("119.00"))
        bank.importiere(self.m, "a.csv", self.csv("119,00", "Techdata GmbH", "Gutschrift G-1"))
        z = LieferantengutschriftZahlung.objects.get()
        services.gutschrift_zahlung_stornieren(z, "falsch zugeordnet")
        u = Bankumsatz.objects.get()
        self.assertEqual(u.status, Bankumsatz.OFFEN)
        self.assertEqual(Lieferantengutschrift.objects.get(pk=g.pk).restguthaben, D("119.00"))
        services.gutschrift_zahlung_buchen(g, None, None, bankumsatz=u)  # erneut zuordenbar
        self.assertEqual(Lieferantengutschrift.objects.get(pk=g.pk).restguthaben, D("0.00"))

    # ---- Seiten --------------------------------------------------------------------------------
    def test_ruecksendung_formular_und_ablauf(self):
        r = self.ruecksendung()
        self.anmelden()
        seite = self.client.get(reverse("einkauf:ruecksendung", args=[r.pk]))
        for feld in ("nummer", "datum", "netto", "steuer", "brutto", "notiz"):
            self.assertContains(seite, f'name="{feld}"')
        self.assertContains(seite, 'name="netto" size="9" inputmode="decimal" value="45.00"')  # Vorschlag: Wert der Rücksendung
        antwort = self.client.post(reverse("einkauf:ruecksendung", args=[r.pk]), {
            "aktion": "gutschrift", "nummer": " ", "datum": HEUTE.isoformat(), "netto": "45,00", "steuer": "8,55", "brutto": "53,55"}, follow=True)
        self.assertIn("Gutschrift-Nr.", meldungen(antwort))
        self.assertEqual(Ruecksendung.objects.get(pk=r.pk).status, RuecksendungStatus.ANGELEGT)
        antwort = self.client.post(reverse("einkauf:ruecksendung", args=[r.pk]), {
            "aktion": "gutschrift", "nummer": "G-5", "datum": "", "netto": "45,00", "steuer": "8,55", "brutto": "53,55"}, follow=True)
        self.assertIn("Gutschriftdatum", meldungen(antwort))  # Datum ist Pflicht, kein stilles „heute“
        antwort = self.client.post(reverse("einkauf:ruecksendung", args=[r.pk]), {
            "aktion": "gutschrift", "nummer": "G-5", "datum": HEUTE.isoformat(), "netto": "45,00", "steuer": "8,55", "brutto": "53,00"}, follow=True)
        self.assertIn("Netto plus USt", meldungen(antwort))
        antwort = self.client.post(reverse("einkauf:ruecksendung", args=[r.pk]), {
            "aktion": "gutschrift", "nummer": "G-5", "datum": HEUTE.isoformat(), "netto": "45,00", "steuer": "8,55", "brutto": "53,55"}, follow=True)
        g = Lieferantengutschrift.objects.get()
        self.assertEqual(antwort.redirect_chain[-1][0], reverse("einkauf:gutschrift", args=[g.pk]))
        self.assertContains(antwort, "G-5")
        self.assertContains(self.client.get(reverse("einkauf:ruecksendung", args=[r.pk])), reverse("einkauf:gutschrift", args=[g.pk]))

    def test_verrechnen_ueber_die_seite(self):
        r, g = self.rechnung("R1", D("100.00")), self.gutschrift("G1", D("60.00"))
        self.rechnung("R-FREMD", lieferant=self.lf2)
        self.anmelden()
        seite = self.client.get(reverse("einkauf:gutschrift", args=[g.pk]))
        self.assertContains(seite, "Mit Rechnung verrechnen")
        self.assertContains(seite, "R1 · offen 100,00")
        self.assertNotContains(seite, "R-FREMD")
        self.assertContains(seite, 'name="betrag" size="9" inputmode="decimal" value="60,00"')  # Vorschlag: min(Rest, offen)
        antwort = self.client.post(reverse("einkauf:gutschrift_verrechnen", args=[g.pk]), {"rechnung": r.pk, "betrag": "25,50", "datum": HEUTE.isoformat()}, follow=True)
        self.assertIn("25,50 € mit Rechnung R1 verrechnet", meldungen(antwort))
        self.assertContains(self.client.get(reverse("einkauf:rechnung", args=[r.pk])), "Verrechnet mit Gutschriften")
        self.assertEqual(Lieferantenrechnung.objects.get(pk=r.pk).zahlbetrag(), D("74.50"))

    def test_verrechnen_fehler_ueber_die_seite(self):
        g = self.gutschrift("G1", D("60.00"))
        fremd, eigene = self.rechnung("R-FREMD", lieferant=self.lf2), self.rechnung("R1", D("100.00"))
        self.anmelden()
        url = reverse("einkauf:gutschrift_verrechnen", args=[g.pk])
        self.assertIn("offene Rechnung dieses Lieferanten", meldungen(self.client.post(url, {"rechnung": fremd.pk, "betrag": "10"}, follow=True)))
        self.assertIn("offene Rechnung dieses Lieferanten", meldungen(self.client.post(url, {"rechnung": "", "betrag": "10"}, follow=True)))
        self.assertIn("Restguthaben", meldungen(self.client.post(url, {"rechnung": eigene.pk, "betrag": "60,01"}, follow=True)))
        self.assertIn("Ungültige Zahl", meldungen(self.client.post(url, {"rechnung": eigene.pk, "betrag": "abc"}, follow=True)))
        self.assertFalse(LieferantengutschriftVerrechnung.objects.exists())

    def test_fremder_mandant_sieht_gutschrift_nicht(self):
        g = self.gutschrift("G1", D("60.00"), mandant=self.m2)
        self.anmelden()
        for name, args in (("gutschrift", [g.pk]),):
            self.assertEqual(self.client.get(reverse(f"einkauf:{name}", args=args)).status_code, 404)
        for name in ("gutschrift_verrechnen", "gutschrift_zahlung"):
            self.assertEqual(self.client.post(reverse(f"einkauf:{name}", args=[g.pk]), {"betrag": "1"}).status_code, 404)
        self.assertFalse(LieferantengutschriftZahlung.objects.exists())

    def test_rueckgaengig_und_storno_nur_fuer_admins(self):
        r, g = self.rechnung("R1", D("119.00")), self.gutschrift("G1", D("119.00"))
        v = services.gutschrift_verrechnen(g, r, D("19.00"))
        g2 = self.gutschrift("G2", D("10.00"))
        z = services.gutschrift_zahlung_buchen(g2, D("10.00"), HEUTE)
        self.anmelden(self.normal)
        self.client.post(reverse("einkauf:gutschrift_verrechnung_aufheben", args=[g.pk, v.pk]), {"grund": "x"})
        self.client.post(reverse("einkauf:gutschrift_zahlung_storno", args=[g2.pk, z.pk]), {"grund": "x"})
        self.assertFalse(LieferantengutschriftVerrechnung.objects.get(pk=v.pk).aufgehoben)
        self.assertFalse(LieferantengutschriftZahlung.objects.get(pk=z.pk).storniert)
        self.anmelden(self.admin)
        antwort = self.client.post(reverse("einkauf:gutschrift_verrechnung_aufheben", args=[g.pk, v.pk]), {"grund": ""}, follow=True)
        self.assertIn("Grund", meldungen(antwort))
        self.client.post(reverse("einkauf:gutschrift_verrechnung_aufheben", args=[g.pk, v.pk]), {"grund": "Irrtum"})
        self.client.post(reverse("einkauf:gutschrift_zahlung_storno", args=[g2.pk, z.pk]), {"grund": "Irrtum"})
        self.assertTrue(LieferantengutschriftVerrechnung.objects.get(pk=v.pk).aufgehoben)
        self.assertTrue(LieferantengutschriftZahlung.objects.get(pk=z.pk).storniert)

    def test_storno_direkt_aus_zahlungen_lieferanten(self):
        """Wunsch des Nutzers 19.11.2026: die Gutschriftzahlung muss storniert werden können – auch in der Liste."""
        g = self.gutschrift("G1", D("50.00"))
        z = services.gutschrift_zahlung_buchen(g, D("50.00"), HEUTE)
        self.anmelden()
        liste = self.client.get(reverse("finanzen:zahlungen_lieferanten"))
        self.assertContains(liste, reverse("einkauf:gutschrift_zahlung_storno", args=[g.pk, z.pk]))
        r = self.client.post(reverse("einkauf:gutschrift_zahlung_storno", args=[g.pk, z.pk]), {"grund": "doppelt", "zurueck": "zahlungen"})
        self.assertRedirects(r, reverse("finanzen:zahlungen_lieferanten"), fetch_redirect_response=False)
        z.refresh_from_db()
        self.assertTrue(z.storniert)
        self.assertEqual(Lieferantengutschrift.objects.get(pk=g.pk).restguthaben, D("50.00"))
        # nach dem Storno wieder offen: Liste „Mit Restguthaben“, Offene Posten, Zahlung neu buchbar
        self.assertIn(g, services.offene_gutschriften(self.m))
        self.assertContains(self.client.get(reverse("einkauf:gutschriften")), reverse("einkauf:gutschrift", args=[g.pk]))
        self.assertContains(self.client.get(reverse("finanzen:op_lieferanten")), "G1")
        self.assertContains(self.client.get(reverse("einkauf:gutschrift", args=[g.pk])), "Zahlung erhalten buchen")
        self.assertNotContains(self.client.get(reverse("finanzen:zahlungen_lieferanten")),
                               reverse("einkauf:gutschrift_zahlung_storno", args=[g.pk, z.pk]))

    def test_zahlung_ueber_die_seite_und_zahlungen_lieferanten(self):
        g = self.gutschrift("G1", D("119.00"))
        self.anmelden()
        seite = self.client.get(reverse("einkauf:gutschrift", args=[g.pk]))
        self.assertContains(seite, "Zahlung erhalten buchen")
        self.assertContains(seite, 'name="betrag" size="9" inputmode="decimal" value="119,00"')  # Vorschlag: Restguthaben
        antwort = self.client.post(reverse("einkauf:gutschrift_zahlung", args=[g.pk]), {"datum": HEUTE.isoformat(), "betrag": ""}, follow=True)
        self.assertIn("Zahlung des Lieferanten über 119,00 €", meldungen(antwort))
        self.assertEqual(Lieferantengutschrift.objects.get(pk=g.pk).restguthaben, D("0.00"))
        zahlungen = self.client.get(reverse("finanzen:zahlungen_lieferanten"))
        self.assertContains(zahlungen, "Zahlungseingänge von Lieferanten")
        self.assertContains(zahlungen, "G1")
        antwort = self.client.post(reverse("einkauf:gutschrift_zahlung", args=[g.pk]), {"betrag": "1"}, follow=True)  # nichts mehr übrig
        self.assertIn("Höchstens 0.00 €", meldungen(antwort))

    def test_offene_posten_lieferanten_zeigen_guthaben(self):
        g = self.gutschrift("G1", D("119.00"))
        fremd = self.gutschrift("G9", D("5.00"), mandant=self.m2)
        voll = self.gutschrift("G2", D("10.00"))
        services.gutschrift_zahlung_buchen(voll, D("10.00"), HEUTE)
        self.anmelden()
        seite = self.client.get(reverse("finanzen:op_lieferanten"))
        self.assertContains(seite, "Guthaben bei Lieferanten")
        self.assertContains(seite, reverse("einkauf:gutschrift", args=[g.pk]))
        self.assertNotContains(seite, reverse("einkauf:gutschrift", args=[fremd.pk]))  # anderer Mandant
        self.assertNotContains(seite, reverse("einkauf:gutschrift", args=[voll.pk]))  # kein Restguthaben mehr
        self.assertNotContains(self.client.get(reverse("einkauf:rechnungen")), "Guthaben bei Lieferanten")

    def test_rechnungsliste_zeigt_gekuerzten_zahlbetrag(self):
        r, g = self.rechnung("R1", D("119.00")), self.gutschrift("G1", D("19.00"))
        services.gutschrift_verrechnen(g, r, D("19.00"))
        self.anmelden()
        seite = self.client.get(reverse("einkauf:rechnungen"))
        self.assertContains(seite, "100,00 <small>(nach Gutschrift)</small>")
