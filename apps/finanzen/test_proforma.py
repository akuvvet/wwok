"""Proforma-Rechnung als Vorkasse (Issue #57, README Abschnitt 18): Vorkasse, Zahlstatus, Umwandlung, Verrechnung, Storno."""
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.kern import dashboard
from apps.kern.models import Mandant
from apps.lager.models import Bewegungsart, Lagerbewegung, bestand, reserviert
from apps.lager.services import buche_manuell
from apps.stammdaten.models import Artikel, Kunde, Steuersatz, Zahlungsbedingung
from apps.verkauf.models import Beleg, BelegGesperrt, Belegart, Position
from apps.verkauf.pdf import beleg_pdf
from apps.verkauf.zugferd import xml_erzeugen

from . import anzahlung, bank, datev, services
from .models import Anzahlungsverrechnung, Bankumsatz, Kassenbuch, Zahlart

E = Decimal


@override_settings(SECURE_SSL_REDIRECT=False)
class ProformaBasis(TestCase):
    """Gemeinsame Daten und Hilfen."""
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer", ust_id="DE123456789",
                                       strasse="Hauptstr. 1", plz="42651", ort="Solingen", iban="DE02120300000000202051")
        self.ziel = Zahlungsbedingung.objects.create(bezeichnung="30 Tage netto", tage_netto=30)
        self.kunde = Kunde.objects.create(nummer="1", name1="Kunde GmbH", zahlungsbedingung=self.ziel,
                                          strasse="Weg 2", plz="40210", ort="Düsseldorf")
        st = Steuersatz.objects.create(bezeichnung="19", prozent=E("19"))
        self.a = Artikel.objects.create(nummer="A1", bezeichnung="Notebook", steuersatz=st, vk1=E("100"))
        buche_manuell(self.m, self.a, Bewegungsart.WARENEINGANG, E("10"))
        self.user = get_user_model().objects.create_user("t", password="x" * 14, is_staff=True, is_superuser=True)
        self.client.force_login(self.user)
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    # Hilfen
    def bestellung(self, menge="2", preis="100"):
        ab = Beleg.objects.create(mandant=self.m, art=Belegart.AUFTRAG, kunde=self.kunde, zahlungsbedingung=self.ziel)
        Position.objects.create(beleg=ab, artikel=self.a, bezeichnung="Notebook", menge=E(menge), einzelpreis=E(preis),
                                steuer_prozent=E("19"))
        ab.berechne()
        return ab

    def proforma(self, abschliessen=True):
        ab = self.bestellung()
        pf = ab.kopieren_als(Belegart.PROFORMA)
        if abschliessen:
            pf.abschliessen()
        pf.refresh_from_db()
        return ab, pf  # 2 × 100 netto = 238,00 brutto

    def zahlen(self, beleg, betrag, zahlart=Zahlart.UEBERWEISUNG):
        return services.zahlung_buchen(beleg, E(betrag), date.today(), zahlart, benutzer=self.user)


class ProformaTest(ProformaBasis):
    # 1. Vorkasse
    def test_proforma_hat_immer_vorkasse_auch_bei_anderer_kundenbedingung(self):
        _, pf = self.proforma()
        self.assertTrue(pf.zahlungsbedingung.vorkasse)
        self.assertEqual(pf.zahlungsbedingung.bezeichnung, "Vorkasse")
        r = self.client.post(reverse("verkauf:beleg_kopf", args=[pf.pk]), {"zahlungsbedingung": self.ziel.pk})
        self.assertEqual(r.status_code, 302)
        pf.refresh_from_db()
        self.assertTrue(pf.zahlungsbedingung.vorkasse)  # Auswahl in der Maske wird ignoriert

    def test_vorhandene_vorkasse_bedingung_wird_genutzt(self):
        eigene = Zahlungsbedingung.objects.create(bezeichnung="Vorauskasse", vorkasse=True)
        _, pf = self.proforma()
        self.assertEqual(pf.zahlungsbedingung, eigene)

    def test_proforma_nur_aus_bestellung(self):
        an = Beleg.objects.create(mandant=self.m, art=Belegart.ANGEBOT, kunde=self.kunde)
        Position.objects.create(beleg=an, bezeichnung="X", menge=1, einzelpreis=E("10"))
        with self.assertRaises(ValueError):
            an.kopieren_als(Belegart.PROFORMA)
        r = self.client.post(reverse("verkauf:beleg_neu", args=[self.kunde.pk]), {"art": "PF"})
        self.assertEqual(r.status_code, 302)
        self.assertFalse(Beleg.objects.filter(art=Belegart.PROFORMA).exists())

    def test_keine_zweite_proforma_und_kein_lieferschein_neben_der_proforma(self):
        ab, pf = self.proforma()
        with self.assertRaises(ValueError):
            ab.kopieren_als(Belegart.PROFORMA)
        with self.assertRaises(ValueError):
            ab.folgebeleg_erzeugen()  # Lieferschein: die Rechnung entsteht über die Proforma
        with self.assertRaises(ValueError):
            ab.kopieren_als(Belegart.RECHNUNG)

    # 2. Zahlung und Status
    def test_status_offen_teilbezahlt_bezahlt_und_anzeige(self):
        _, pf = self.proforma()
        self.assertEqual(anzahlung.proforma_status(pf)["code"], "offen")
        self.assertEqual(services.offen(pf), E("238.00"))
        self.zahlen(pf, "100")
        self.assertEqual(anzahlung.proforma_status(pf)["code"], "teilbezahlt")
        self.assertContains(self.client.get(reverse("verkauf:beleg", args=[pf.pk])), "teilbezahlt")
        self.zahlen(pf, "138")
        self.assertEqual(anzahlung.proforma_status(pf)["code"], "bezahlt")
        self.assertContains(self.client.get(reverse("verkauf:beleg", args=[pf.pk])), "In Rechnung umwandeln</strong>")
        self.assertContains(self.client.get(reverse("verkauf:kunde", args=[self.kunde.pk])), "bezahlt")
        liste = self.client.get(reverse("verkauf:belege"), {"zahlung": "bezahlt"})
        self.assertContains(liste, pf.nummer)
        self.assertNotContains(self.client.get(reverse("verkauf:belege"), {"zahlung": "offen"}), pf.nummer)
        self.assertEqual(self.client.get(reverse("finanzen:beleg", args=[pf.pk])).status_code, 200)

    def test_zahlung_auf_entwurf_nicht_moeglich(self):
        _, pf = self.proforma(abschliessen=False)
        with self.assertRaises(ValueError):
            self.zahlen(pf, "10")

    # 3. Umwandlung
    def test_umwandlung_nur_bei_voller_zahlung_und_nur_einmal(self):
        _, pf = self.proforma()
        with self.assertRaises(ValueError):
            anzahlung.proforma_umwandeln(pf)
        self.zahlen(pf, "200")
        with self.assertRaises(ValueError):  # Teilzahlung reicht nicht
            anzahlung.proforma_umwandeln(pf)
        with self.assertRaises(ValueError):  # auch nicht über „Kopieren als Rechnung“
            pf.kopieren_als(Belegart.RECHNUNG)
        self.zahlen(pf, "38")
        re = anzahlung.proforma_umwandeln(pf, self.user)
        self.assertEqual((re.art, re.abgeschlossen, re.vorgaenger_id, re.summe_brutto), (Belegart.RECHNUNG, True, pf.pk, E("238.00")))
        with self.assertRaises(ValueError):
            anzahlung.proforma_umwandeln(pf)
        r = self.client.post(reverse("verkauf:beleg_umwandeln", args=[pf.pk]))  # Doppelklick: Meldung, keine 2. Rechnung
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Beleg.objects.filter(art=Belegart.RECHNUNG).count(), 1)
        self.assertEqual(anzahlung.proforma_status(pf)["code"], "umgewandelt")

    def test_umwandlung_ueber_die_seite(self):
        _, pf = self.proforma()
        self.zahlen(pf, "238")
        r = self.client.post(reverse("verkauf:beleg_umwandeln", args=[pf.pk]))
        re = Beleg.objects.get(art=Belegart.RECHNUNG)
        self.assertRedirects(r, reverse("verkauf:beleg", args=[re.pk]), fetch_redirect_response=False)

    # 4. Rechnung ist bezahlt und nicht offen
    def test_rechnung_dankend_erhalten_und_nicht_offen(self):
        _, pf = self.proforma()
        self.zahlen(pf, "238")
        re = anzahlung.proforma_umwandeln(pf)
        self.assertEqual(re.zahlungsbedingung.bezeichnung, "Betrag dankend erhalten")
        self.assertEqual(services.offen(re), E("0"))
        self.assertEqual(services.offene_rechnungen(self.m), [])
        self.assertEqual(services.mahnvorschlag(self.m, heute=date(2030, 1, 1)), [])
        self.assertEqual(services.kunde_offen(self.m, self.kunde), E("0"))
        self.assertEqual(dashboard.offene_posten(self.m, date.today())["anzahl"], 0)
        v = Anzahlungsverrechnung.objects.get()
        self.assertEqual((v.quelle, v.rechnung, v.betrag), (pf, re, E("238.00")))
        self.assertNotContains(self.client.get(reverse("finanzen:lastschrift")), re.nummer)
        pdf = beleg_pdf(re)
        self.assertTrue(pdf.startswith(b"%PDF"))
        xml = xml_erzeugen(re).decode()
        self.assertIn("<ram:TotalPrepaidAmount>238.00</ram:TotalPrepaidAmount>", xml)
        self.assertIn("<ram:DuePayableAmount>0.00</ram:DuePayableAmount>", xml)

    def test_rechnung_bucht_lager_ab_und_reservierung_wandert(self):
        ab, pf = self.proforma()
        self.assertEqual(reserviert(self.m, self.a), E("2"))  # die Proforma hält die Reservierung der Bestellung
        self.zahlen(pf, "238")
        anzahlung.proforma_umwandeln(pf)
        self.assertEqual(bestand(self.m, self.a), E("8"))
        self.assertEqual(reserviert(self.m, self.a), E("0"))
        self.assertNotIn(ab.pk, list(dashboard.kundenbestellungen_offen(self.m).values_list("pk", flat=True)))

    # 5. Bar: kein zweiter Kassenbucheintrag, DATEV ohne doppelte Zahlung
    def test_barzahlung_nur_einmal_in_kasse_und_datev(self):
        _, pf = self.proforma()
        self.zahlen(pf, "238", Zahlart.BAR)
        re = anzahlung.proforma_umwandeln(pf)
        self.assertEqual(Kassenbuch.objects.count(), 1)
        inhalt = datev.export(self.m, date(2000, 1, 1), date(2100, 1, 1)).decode("cp1252", errors="replace")
        self.assertEqual(inhalt.count("Zahlung Kunde GmbH"), 1)  # die Zahlung nur einmal, mit Proforma-Nummer und Zahldatum
        self.assertEqual(inhalt.count("Rechnung Kunde GmbH"), 1)  # Belegzeile nur für die Rechnung, nicht für die Proforma
        zahlzeile = [z for z in inhalt.splitlines() if "Zahlung" in z][0]
        self.assertIn(pf.nummer, zahlzeile)
        self.assertIn(re.nummer, inhalt)

    # 6. Sperren und Löschen
    def test_proforma_mit_zahlung_fest_und_nicht_loeschbar(self):
        _, pf = self.proforma()
        self.assertIsNone(pf.loeschhindernis())
        self.zahlen(pf, "50")
        pf.refresh_from_db()
        self.assertTrue(pf.positionen_gesperrt)
        p = pf.positionen.first()
        p.menge = E("5")
        with self.assertRaises(BelegGesperrt):
            p.save()
        with self.assertRaises(BelegGesperrt):
            Position.objects.create(beleg=pf, bezeichnung="mehr", menge=1, einzelpreis=E("1"))
        self.assertIsNotNone(pf.loeschhindernis())
        self.zahlen(pf, "188")
        anzahlung.proforma_umwandeln(pf)
        self.assertIn("Rechnung", pf.loeschhindernis())

    # 7. Bankabgleich
    def test_bankabgleich_ordnet_proforma_zu(self):
        _, pf = self.proforma()
        self.assertEqual(bank.finde_rechnung(self.m, f"Vorkasse {pf.nummer} Danke"), pf)
        u = Bankumsatz.objects.create(mandant=self.m, buchungsdatum=date.today(), betrag=E("238.00"),
                                      zweck=f"{pf.nummer}", pruefsumme="x1")
        self.assertTrue(bank.zuordnen_automatisch(u))
        self.assertEqual(anzahlung.proforma_status(pf)["code"], "bezahlt")
        u2 = Bankumsatz.objects.create(mandant=self.m, buchungsdatum=date.today(), betrag=E("5"), zweck="", pruefsumme="x2")
        self.client.post(reverse("finanzen:bank_zuordnen", args=[u2.pk]), {"rechnung": pf.nummer})
        u2.refresh_from_db()
        self.assertEqual(u2.status, Bankumsatz.ZUGEORDNET)

    # 8. Anzahlung auf Bestellung
    def test_anzahlung_auf_bestellung_wird_mit_rechnung_verrechnet(self):
        ab = self.bestellung()
        self.zahlen(ab, "100")  # z. B. Shop/PayPal
        ls = ab.folgebeleg_erzeugen()
        ls.abschliessen()
        re = ls.folgebeleg_erzeugen()
        re.abschliessen()
        self.assertEqual(services.angerechnet(re), E("100"))
        self.assertEqual(services.offen(re), E("138.00"))
        self.assertEqual(anzahlung.guthaben(ab), E("0"))
        xml = xml_erzeugen(re).decode()
        self.assertIn("<ram:DuePayableAmount>138.00</ram:DuePayableAmount>", xml)

    # Überzahlung: Guthaben zurückzahlen oder mit anderer Rechnung verrechnen
    def test_ueberzahlung_guthaben_verrechnen(self):
        _, pf = self.proforma()
        self.zahlen(pf, "300")
        self.assertEqual(anzahlung.proforma_status(pf)["code"], "ueberzahlt")
        re = anzahlung.proforma_umwandeln(pf)
        self.assertEqual(services.offen(re), E("0"))
        self.assertEqual(anzahlung.guthaben(pf), E("62.00"))
        self.assertEqual(anzahlung.proforma_status(pf)["code"], "guthaben")
        andere = Beleg.objects.create(mandant=self.m, art=Belegart.RECHNUNG, kunde=self.kunde)
        Position.objects.create(beleg=andere, bezeichnung="Service", menge=1, einzelpreis=E("50"), steuer_prozent=E("19"))
        andere.abschliessen()  # 59,50 brutto
        with self.assertRaises(ValueError):  # nicht mehr als offen
            anzahlung.anzahlung_verrechnen(pf, andere, E("62"))
        r = self.client.post(reverse("finanzen:anzahlung_verrechnen", args=[pf.pk]),
                             {"rechnung": andere.pk, "betrag": "59,50", "guthaben_bisher": "62.00"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(services.offen(andere), E("0"))
        self.assertEqual(anzahlung.guthaben(pf), E("2.50"))
        self.zahlen(pf, "-2.50")  # Rest zurücküberweisen
        self.assertEqual(anzahlung.proforma_status(pf)["code"], "umgewandelt")
        fremd = Kunde.objects.create(nummer="2", name1="Andere")
        x = Beleg.objects.create(mandant=self.m, art=Belegart.RECHNUNG, kunde=fremd)
        with self.assertRaises(ValueError):
            anzahlung.anzahlung_verrechnen(pf, x, E("1"))

    def test_offene_posten_zeigen_proformas_getrennt(self):
        _, pf = self.proforma()
        self.zahlen(pf, "100")
        r = self.client.get(reverse("finanzen:offene_posten"))
        self.assertContains(r, "Offene Proformas (Vorkasse)")
        self.assertContains(r, pf.nummer)
        self.assertEqual(r.context["summe"], E("0"))  # keine Forderung

    # Kunde kauft nicht: zurückzahlen, dann stornieren
    def test_storno_erst_nach_rueckzahlung_mit_eigenem_beleg(self):
        ab, pf = self.proforma()
        self.zahlen(pf, "238", Zahlart.BAR)
        with self.assertRaises(ValueError):
            anzahlung.proforma_stornieren(pf, "Kunde verzichtet")
        with self.assertRaises(ValueError):
            anzahlung.proforma_stornieren(pf, "")
        self.zahlen(pf, "-238", Zahlart.BAR)  # Rückzahlung aus der Kasse
        self.assertEqual(anzahlung.proforma_status(pf)["code"], "erstattet")
        r = self.client.post(reverse("verkauf:beleg_proforma_storno", args=[pf.pk]), {"grund": "Kunde verzichtet"})
        sp = Beleg.objects.get(art=Belegart.PROFORMA_STORNO)
        self.assertRedirects(r, reverse("verkauf:beleg", args=[sp.pk]), fetch_redirect_response=False)
        self.assertTrue(sp.abgeschlossen and sp.gesperrt and sp.nummer.startswith("SP"))
        self.assertEqual((sp.vorgaenger_id, sp.summe_brutto), (pf.pk, pf.summe_brutto))
        self.assertIn("Kunde verzichtet", sp.bemerkung)
        self.assertTrue(beleg_pdf(sp).startswith(b"%PDF"))
        self.assertEqual(anzahlung.proforma_status(pf)["code"], "storniert")
        self.assertEqual(Kassenbuch.objects.count(), 2)  # Einnahme und Ausgabe, nachvollziehbar
        with self.assertRaises(ValueError):
            self.zahlen(pf, "10")
        with self.assertRaises(ValueError):
            anzahlung.proforma_umwandeln(pf)
        with self.assertRaises(ValueError):
            anzahlung.proforma_stornieren(pf, "nochmal")
        self.assertIsNotNone(pf.loeschhindernis())
        self.assertIsNotNone(sp.loeschhindernis())
        # Die Bestellung ist wieder offen und reserviert
        self.assertTrue(ab.offene_mengen())
        self.assertEqual(reserviert(self.m, self.a), E("2"))
        self.assertEqual(services.kunde_offen(self.m, self.kunde), E("0"))
        self.assertEqual(dashboard.umsatz(self.m, date(2000, 1, 1), date(2100, 1, 1))["netto"], E("0"))

    # Optionaler Lieferschein zur Rechnung
    def test_lieferschein_zur_rechnung_ohne_lagerbuchung(self):
        _, pf = self.proforma()
        self.zahlen(pf, "238")
        re = anzahlung.proforma_umwandeln(pf)
        vorher = Lagerbewegung.objects.count()
        r = self.client.get(reverse("verkauf:beleg_lieferschein_pdf", args=[re.pk]))
        self.assertEqual((r.status_code, r["Content-Type"]), (200, "application/pdf"))
        self.assertEqual(Lagerbewegung.objects.count(), vorher)
        self.assertFalse(Beleg.objects.filter(art=Belegart.LIEFERSCHEIN).exists())
        self.assertEqual(bestand(self.m, self.a), E("8"))
        self.assertEqual(self.client.get(reverse("verkauf:beleg_lieferschein_pdf", args=[pf.pk])).status_code, 302)

    def test_aufhebung_der_verrechnung_macht_rechnung_wieder_offen(self):
        _, pf = self.proforma()
        self.zahlen(pf, "238")
        re = anzahlung.proforma_umwandeln(pf)
        v = Anzahlungsverrechnung.objects.get()
        with self.assertRaises(PermissionError):
            v.delete()
        anzahlung.anzahlung_aufheben(v, "Fehler")
        self.assertEqual(services.offen(re), E("238.00"))
        self.assertEqual(anzahlung.guthaben(pf), E("238.00"))
        with self.assertRaises(ValueError):
            anzahlung.anzahlung_aufheben(v, "doppelt")

    # Review: verrechnete Zahlung darf nicht zurückgezahlt/storniert werden
    def test_verrechnete_zahlung_nicht_rueckzahlbar_oder_stornierbar(self):
        _, pf = self.proforma()
        z = self.zahlen(pf, "238")
        re = anzahlung.proforma_umwandeln(pf)
        with self.assertRaises(ValueError):
            self.zahlen(pf, "-238")
        with self.assertRaises(ValueError):
            services.zahlung_stornieren(z, "Rücklastschrift")
        self.assertEqual(services.offen(re), E("0"))
        anzahlung.anzahlung_aufheben(Anzahlungsverrechnung.objects.get(), "Rücklastschrift")
        services.zahlung_stornieren(z, "Rücklastschrift")  # jetzt erlaubt; die Rechnung ist wieder offen
        self.assertEqual(services.offen(re), E("238.00"))

    # Review: Anzahlung auf der Bestellung zählt für die Vollzahlung der Proforma
    def test_anzahlung_der_bestellung_zaehlt_fuer_proforma(self):
        ab = self.bestellung()
        self.zahlen(ab, "38")  # Shop/PayPal
        pf = ab.kopieren_als(Belegart.PROFORMA)
        pf.abschliessen()
        self.assertEqual(services.offen(pf), E("200.00"))
        self.zahlen(pf, "200")
        self.assertEqual(anzahlung.proforma_status(pf)["code"], "bezahlt")
        re = anzahlung.proforma_umwandeln(pf)
        self.assertEqual(services.offen(re), E("0"))
        self.assertEqual(sorted(re.anzahlungen.values_list("betrag", flat=True)), [E("38.00"), E("200.00")])
        self.assertEqual(anzahlung.guthaben(pf), E("0"))

    # Review: spätere Verrechnung ändert den ausgestellten Rechnungsdruck nicht (GoBD)
    def test_spaetere_verrechnung_aendert_druck_nicht(self):
        re = Beleg.objects.create(mandant=self.m, art=Belegart.RECHNUNG, kunde=self.kunde, zahlungsbedingung=self.ziel)
        Position.objects.create(beleg=re, bezeichnung="Service", menge=1, einzelpreis=E("100"), steuer_prozent=E("19"))
        re.abschliessen()
        vorher = xml_erzeugen(re).decode()
        _, pf = self.proforma()
        self.zahlen(pf, "300")
        anzahlung.proforma_umwandeln(pf)
        anzahlung.anzahlung_verrechnen(pf, re, E("62"))
        self.assertEqual(services.offen(re), E("57.00"))
        self.assertEqual(xml_erzeugen(re).decode(), vorher)
        self.assertNotIn("TotalPrepaidAmount", vorher)


class UeberzahlungTest(ProformaBasis):
    """Überzahlte Rechnung: Guthaben beim Zahlung buchen anderer Rechnungen vorschlagen und nachvollziehbar verrechnen."""

    def rechnung(self, netto="100", kunde=None):
        re = Beleg.objects.create(mandant=self.m, art=Belegart.RECHNUNG, kunde=kunde or self.kunde, zahlungsbedingung=self.ziel)
        Position.objects.create(beleg=re, bezeichnung="Service", menge=1, einzelpreis=E(netto), steuer_prozent=E("19"))
        re.abschliessen()
        return re  # 119,00 brutto bei 100 netto

    def test_ueberzahlung_wird_vorgeschlagen_und_verrechnet(self):
        alt = self.rechnung()
        self.zahlen(alt, "150")  # 31,00 zu viel
        self.assertEqual(anzahlung.guthaben(alt), E("31.00"))
        neu = self.rechnung("50")  # 59,50
        seite = self.client.get(reverse("finanzen:beleg", args=[neu.pk]))
        self.assertContains(seite, "Guthaben des Kunden verrechnen")
        self.assertEqual([(g["beleg"], g["vorschlag"]) for g in seite.context["kunden_guthaben"]], [(alt, E("31.00"))])
        r = self.client.post(reverse("finanzen:anzahlung_verrechnen", args=[alt.pk]),
                             {"rechnung": neu.pk, "betrag": "31,00", "guthaben_bisher": "31.00", "zurueck": "rechnung"})
        self.assertRedirects(r, reverse("finanzen:beleg", args=[neu.pk]), fetch_redirect_response=False)
        self.assertEqual((services.offen(alt), services.offen(neu)), (E("0"), E("28.50")))
        self.assertEqual([z["beleg"] for z in services.offene_rechnungen(self.m)], [neu])
        self.assertEqual(dashboard.offene_posten(self.m, date.today())["betrag"], E("28.50"))
        self.assertEqual(services.kunde_offen(self.m, self.kunde), E("28.50"))
        self.assertEqual(self.client.get(reverse("finanzen:beleg", args=[neu.pk])).context["kunden_guthaben"], [])
        # nachvollziehbar: auf beiden Belegen und im Kontoauszug, Saldo unverändert richtig
        self.assertContains(self.client.get(reverse("finanzen:beleg", args=[alt.pk])), "Weiterverrechnet an andere Rechnungen")
        self.assertContains(self.client.get(reverse("finanzen:beleg", args=[neu.pk])), "Erhalten aus")
        konto = self.client.get(reverse("finanzen:kontoauszug", args=[self.kunde.pk]))
        self.assertContains(konto, f"Verrechnet: Guthaben {alt} → {neu}")
        self.assertEqual(konto.context["saldo"], E("28.50"))
        self.assertContains(self.client.get(reverse("verkauf:kunde", args=[self.kunde.pk])), "119,00")
        # druckt nichts nach (GoBD): die nachträgliche Verrechnung steht nicht in der E-Rechnung
        self.assertNotIn("TotalPrepaidAmount", xml_erzeugen(neu).decode())

    def test_rueckzahlung_nur_vom_freien_guthaben_und_aufheben(self):
        alt = self.rechnung()
        self.zahlen(alt, "150")
        neu = self.rechnung("10")  # 11,90
        anzahlung.anzahlung_verrechnen(alt, neu, E("11.90"))
        self.assertEqual(anzahlung.guthaben(alt), E("19.10"))
        with self.assertRaises(ValueError):
            self.zahlen(alt, "-31")  # 11,90 sind verrechnet
        self.zahlen(alt, "-19.10")  # Rest zurücküberweisen
        self.assertEqual(services.offen(alt), E("0"))
        v = Anzahlungsverrechnung.objects.get()
        anzahlung.anzahlung_aufheben(v, "falsche Rechnung")
        self.assertEqual((services.offen(alt), services.offen(neu)), (E("-11.90"), E("11.90")))
        self.assertContains(self.client.get(reverse("finanzen:kontoauszug", args=[self.kunde.pk])), "Verrechnung aufgehoben")

    def test_nicht_auf_sich_selbst_und_nicht_fremder_kunde(self):
        alt = self.rechnung()
        self.zahlen(alt, "150")
        with self.assertRaises(ValueError):
            anzahlung.anzahlung_verrechnen(alt, alt, E("1"))
        fremd = self.rechnung(kunde=Kunde.objects.create(nummer="9", name1="Fremd"))
        with self.assertRaises(ValueError):
            anzahlung.anzahlung_verrechnen(alt, fremd, E("1"))
        self.assertEqual(self.client.get(reverse("finanzen:beleg", args=[fremd.pk])).context["kunden_guthaben"], [])

    def test_anzahlung_offener_bestellung_wird_nicht_vorgeschlagen(self):
        ab = self.bestellung()
        self.zahlen(ab, "50")
        neu = self.rechnung()
        self.assertEqual(anzahlung.kunden_guthaben(self.m, self.kunde, ausser=neu), [])
