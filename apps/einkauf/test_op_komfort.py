"""Komfort wie in Mention (Wunsch des Nutzers 24.11.2026, Screenshots OP Lieferanten/Kunden und WE → Rechnung):
Summenblock und Zahlsumme der Auswahl in Offene Posten Lieferanten, Filter „fällig bis“, Gutschrift direkt in der Liste
verrechnen, Lieferantenrechnung direkt im gebuchten Wareneingang erfassen, Summen in Offene Posten Kunden."""
from datetime import date, timedelta
from decimal import Decimal as D

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.kern.models import Mandant
from apps.stammdaten.models import Artikel, Kunde, Lieferant, Steuersatz, Zahlungsbedingung
from apps.verkauf.models import Beleg, Belegart, Position

from . import services
from .models import Lieferantenrechnung, RechnungStatus
from .views import rechnung_summen

HEUTE = date.today()


@override_settings(SECURE_SSL_REDIRECT=False)
class OpLieferantenKomfortTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer", iban="DE02120300000000202051")
        self.lf = Lieferant.objects.create(nummer="L1", name1="Kosatec GmbH", kurzname="KOS", iban="DE89370400440532013000")
        self.maus = Artikel.objects.create(nummer="M1", bezeichnung="Maus", steuersatz=Steuersatz.objects.get(nummer=1), vk1=D("15"))
        self.chef = get_user_model().objects.create_superuser("chef", password="x" * 14)
        self.client.force_login(self.chef)
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    def rechnung(self, nummer, brutto, faellig, **zusatz):
        netto = (brutto / D("1.19")).quantize(D("0.01"))
        return Lieferantenrechnung.objects.create(mandant=self.m, lieferant=self.lf, rechnungsnummer=nummer, datum=HEUTE - timedelta(days=5),
                                                  faellig=faellig, netto=netto, steuer=brutto - netto, brutto=brutto, **zusatz)

    def gebuchter_we(self, menge=D("2"), ek=D("60.50")):
        we = services.wareneingang_anlegen(self.m, self.lf)
        services.wareneingang_erfassen(we, self.maus, menge, ek)
        services.wareneingang_buchen(we, bestaetigt=True)
        we.refresh_from_db()
        return we

    # ---- Summen, Zahlsumme, fällig bis -----------------------------------------------------------
    def test_summen_ueber_alle_offenen_rechnungen(self):
        self.rechnung("R1", D("119.00"), HEUTE - timedelta(days=1))
        self.rechnung("R2", D("238.00"), HEUTE + timedelta(days=10))
        self.rechnung("R3", D("100.00"), HEUTE + timedelta(days=20), skonto_prozent=D("2"), skonto_bis=HEUTE + timedelta(days=3))
        self.rechnung("R4", D("500.00"), HEUTE - timedelta(days=9), status=RechnungStatus.BEZAHLT)
        s = rechnung_summen(Lieferantenrechnung.objects.filter(mandant=self.m), HEUTE, D("0"))
        self.assertEqual((s["faellig"], s["faellig_anzahl"]), (D("119.00"), 1))
        self.assertEqual(s["nicht_faellig"], D("238.00") + D("98.00"))  # Skonto schon abgezogen
        self.assertEqual((s["skonto"], s["skonto_anzahl"], s["anzahl"]), (D("2.00"), 1, 3))
        self.assertEqual(s["gesamt"], D("455.00"))

    def test_liste_zeigt_summen_und_markierhilfen(self):
        self.rechnung("R1", D("119.00"), HEUTE)
        self.rechnung("R2", D("238.00"), HEUTE + timedelta(days=10))
        antwort = self.client.get(reverse("finanzen:zahlungen_lieferanten"))
        self.assertContains(antwort, 'id="op-summen"')
        self.assertContains(antwort, "Gesamt zu zahlen")
        self.assertContains(antwort, 'data-betrag="119.00"')
        self.assertContains(antwort, "data-faellig", count=2)  # Knopf + genau eine fällige Rechnung
        self.assertContains(antwort, 'id="op-zahlsumme"')
        self.assertContains(antwort, "Fällige markieren")

    def test_faellig_bis_filtert_und_falsches_datum_schadet_nicht(self):
        self.rechnung("R-FRUEH", D("119.00"), HEUTE + timedelta(days=2))
        self.rechnung("R-SPAET", D("238.00"), HEUTE + timedelta(days=30))
        antwort = self.client.get(reverse("finanzen:op_lieferanten"), {"bis": (HEUTE + timedelta(days=7)).isoformat()})
        self.assertContains(antwort, "R-FRUEH")
        self.assertNotContains(antwort, "R-SPAET")
        antwort = self.client.get(reverse("finanzen:op_lieferanten"), {"bis": "kein-datum"})
        self.assertContains(antwort, "R-SPAET")

    # ---- Gutschrift direkt in der Liste verrechnen -----------------------------------------------
    def test_gutschrift_in_der_liste_verrechnen(self):
        r = self.rechnung("R1", D("246.07"), HEUTE + timedelta(days=14))
        g = services.gutschrift_erfassen(self.m, self.lf, "G150187421", HEUTE, D("15.02"), D("2.85"), D("17.87"), "", None, self.chef)
        liste = reverse("finanzen:op_lieferanten")
        antwort = self.client.get(liste)
        self.assertContains(antwort, "Guthaben 17,87 € verrechenbar")
        self.assertContains(antwort, f'action="{reverse("einkauf:gutschrift_verrechnen", args=[g.pk])}"')
        antwort = self.client.post(reverse("einkauf:gutschrift_verrechnen", args=[g.pk]),
                                   {"rechnung": r.pk, "betrag": "", "zurueck": liste}, follow=True)
        self.assertEqual(antwort.redirect_chain[-1][0], liste)
        self.assertContains(antwort, "17,87 € mit Rechnung R1 verrechnet")
        r.refresh_from_db()
        self.assertEqual(r.zahlbetrag(), D("228.20"))
        self.assertEqual(g.__class__.objects.get(pk=g.pk).restguthaben, D("0.00"))

    def test_verrechnen_ohne_zurueck_bleibt_wie_bisher_und_fremdes_ziel_ignoriert(self):
        r = self.rechnung("R1", D("119.00"), HEUTE + timedelta(days=14))
        g = services.gutschrift_erfassen(self.m, self.lf, "G1", HEUTE, D("10.00"), D("1.90"), D("11.90"), "", None, self.chef)
        antwort = self.client.post(reverse("einkauf:gutschrift_verrechnen", args=[g.pk]),
                                   {"rechnung": r.pk, "betrag": "5,00", "zurueck": "//boese.example"})
        self.assertRedirects(antwort, reverse("einkauf:gutschrift", args=[g.pk]), fetch_redirect_response=False)
        antwort = self.client.post(reverse("einkauf:gutschrift_verrechnen", args=[g.pk]), {"rechnung": r.pk, "betrag": ""}, follow=True)
        self.assertContains(antwort, "größer als 0")  # ohne Liste bleibt der Betrag Pflicht

    # ---- Rechnung direkt im Wareneingang ---------------------------------------------------------
    def test_wareneingang_bietet_rechnung_vorbelegt_an(self):
        we = self.gebuchter_we()
        antwort = self.client.get(reverse("einkauf:wareneingang", args=[we.pk]))
        self.assertContains(antwort, f"Lieferantenrechnung zu {we.nummer} erfassen")
        self.assertContains(antwort, 'name="netto" size="10" inputmode="decimal" data-ust="netto" value="121,00"')
        self.assertContains(antwort, f'name="wareneingang" value="{we.pk}"')

    def test_rechnung_aus_wareneingang_erfassen_verknuepft(self):
        we = self.gebuchter_we()
        seite = reverse("einkauf:wareneingang", args=[we.pk])
        antwort = self.client.post(reverse("einkauf:rechnung_neu"), {
            "zurueck": seite, "lieferant": self.lf.pk, "wareneingang": we.pk, "rechnungsnummer": "RG-4711",
            "datum": HEUTE.isoformat(), "netto": "121,00", "brutto": "143,99"}, follow=True)
        r = Lieferantenrechnung.objects.get(rechnungsnummer="RG-4711")
        self.assertEqual(list(r.wareneingaenge.all()), [we])
        self.assertTrue(r.abgleich()["ok"])
        self.assertNotContains(self.client.get(seite), "Lieferantenrechnung zu")  # danach nur noch der Verweis
        self.assertContains(self.client.get(seite), "RG-4711")

    def test_fehler_im_wareneingang_behaelt_eingaben(self):
        we = self.gebuchter_we()
        seite = reverse("einkauf:wareneingang", args=[we.pk])
        self.rechnung("RG-DOPPELT", D("10.00"), HEUTE)
        antwort = self.client.post(reverse("einkauf:rechnung_neu"), {
            "zurueck": seite, "lieferant": self.lf.pk, "wareneingang": we.pk, "rechnungsnummer": "RG-DOPPELT",
            "datum": HEUTE.isoformat(), "netto": "121,00", "brutto": "143,99"}, follow=True)
        self.assertContains(antwort, "Nicht gespeichert")
        self.assertContains(antwort, 'value="RG-DOPPELT"')
        self.assertContains(antwort, 'value="143,99"')

    def test_kein_angebot_wenn_schon_rechnung_oder_nicht_gebucht(self):
        offen = services.wareneingang_anlegen(self.m, self.lf)
        services.wareneingang_erfassen(offen, self.maus, D("1"), D("5"))
        self.assertNotContains(self.client.get(reverse("einkauf:wareneingang", args=[offen.pk])), "Lieferantenrechnung zu")

    def test_weitere_freie_wareneingaenge_mit_vorwahl_des_lieferanten(self):
        erster, zweiter = self.gebuchter_we(), self.gebuchter_we(D("1"), D("10"))
        antwort = self.client.get(reverse("einkauf:wareneingang", args=[zweiter.pk]))
        self.assertContains(antwort, f"{erster.nummer} ist auch noch ohne Rechnung")
        antwort = self.client.get(reverse("einkauf:rechnungen"), {"lieferant": self.lf.pk})
        self.assertContains(antwort, f'name="lieferant" id="lieferantid" value="{self.lf.pk}"')
        self.assertContains(antwort, "KOS – Kosatec GmbH (L1)")


@override_settings(SECURE_SSL_REDIRECT=False)
class OpKundenSummenTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.kunde = Kunde.objects.create(nummer="1", name1="Kunde GmbH")
        self.zb = Zahlungsbedingung.objects.create(bezeichnung="10 Tage", tage_netto=10, tage_mahnung=11)
        self.client.force_login(get_user_model().objects.create_superuser("chef", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    def rechnung(self, tage_alt=0):
        b = Beleg.objects.create(mandant=self.m, art=Belegart.RECHNUNG, kunde=self.kunde, zahlungsbedingung=self.zb)
        Position.objects.create(beleg=b, bezeichnung="Dienst", einzelpreis=D("100"))
        b.abschliessen()
        Beleg.objects.filter(pk=b.pk).update(belegdatum=date.today() - timedelta(days=tage_alt))
        b.refresh_from_db()
        return b

    def test_summen_ueberfaellig_nicht_faellig_gutschriften(self):
        self.rechnung(tage_alt=30)
        neu = self.rechnung()
        g = neu.gutschrift_erzeugen()
        g.abschliessen()
        s = self.client.get(reverse("finanzen:offene_posten")).context["summen"]
        self.assertEqual((s["ueberfaellig"], s["nicht_faellig"], s["gutschriften"], s["gesamt"]),
                         (D("119.00"), D("119.00"), D("-119.00"), D("119.00")))
