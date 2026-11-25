"""Entscheidung des Nutzers 19.11.2026: Gutschrift auch ohne Rechnung, unter der Kundenadresse angelegt.
Positionen frei wie bei der Rechnung, Spalte B wie bisher (Standard: keine Ware zurück), Grund ist Pflicht und wird gedruckt,
Bezug frei unter „Bezugnahme“. Nach dem Abschluss Guthaben des Kunden in den Offenen Posten, verrechenbar wie jede Gutschrift."""
import io
from decimal import Decimal as D

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from pypdf import PdfReader

from apps.finanzen.services import gutschrift_rest, gutschrift_verrechnen, kunde_offen, offen
from apps.kern.models import Mandant
from apps.lager.models import Bewegungsart, bestand, reparaturbestand
from apps.lager.services import buche_manuell
from apps.stammdaten.models import Artikel, Kunde, Steuersatz

from .models import Beleg, Belegart, GutschriftGrundFehlt, Position, Ruecknahme
from .pdf import beleg_pdf


@override_settings(SECURE_SSL_REDIRECT=False)
class FreieGutschriftTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.kunde = Kunde.objects.create(nummer="1000", name1="Muster GmbH")
        self.maus = Artikel.objects.create(nummer="M1", bezeichnung="Maus", steuersatz=Steuersatz.objects.get(nummer=1), vk1=D("10"))
        self.client.force_login(get_user_model().objects.create_superuser("chef", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    def neu(self):
        r = self.client.post(reverse("verkauf:beleg_neu", args=[self.kunde.pk]), {"art": "GS"})
        gs = Beleg.objects.get(art=Belegart.GUTSCHRIFT)
        self.assertRedirects(r, reverse("verkauf:beleg", args=[gs.pk]), fetch_redirect_response=False)
        return gs

    def pos(self, gs, menge="2", preis="10"):
        return Position.objects.create(beleg=gs, artikel=self.maus, bezeichnung="Maus", menge=D(menge), einzelpreis=D(preis))

    def test_knopf_unter_der_kundenadresse_und_anlegen(self):
        r = self.client.get(reverse("verkauf:kunde", args=[self.kunde.pk]))
        self.assertContains(r, 'name="art" value="GS" type="submit"')
        self.assertContains(r, "+ Gutschrift</button>")
        gs = self.neu()
        self.assertTrue(gs.freie_gutschrift)
        self.assertIsNone(gs.vorgaenger_id)
        self.assertTrue(gs.vorlaeufig)  # wie jeder neue Beleg: Nummer erst mit dem Speichern
        self.assertContains(self.client.get(reverse("verkauf:beleg", args=[gs.pk])), "Gutschrift ohne Rechnung")

    def test_abschluss_nur_mit_grund_der_gedruckt_wird(self):
        gs = self.neu()
        self.pos(gs)
        gs.berechne()
        url = reverse("verkauf:beleg_abschliessen", args=[gs.pk])
        r = self.client.post(url)
        self.assertContains(r, "Grund der Gutschrift")
        self.assertContains(r, 'name="gutschrift_grund"')
        gs.refresh_from_db()
        self.assertFalse(gs.abgeschlossen)
        r = self.client.post(url, {"gutschrift_grund": "   "})
        self.assertContains(r, "Bitte einen Grund eintragen.")
        self.client.post(url, {"gutschrift_grund": "  Kulanz   wegen Lieferverzug <b> "})
        gs.refresh_from_db()
        self.assertTrue(gs.abgeschlossen and gs.nummer)
        self.assertEqual(gs.gutschrift_grund, "Kulanz wegen Lieferverzug <b>")
        text = " ".join(PdfReader(io.BytesIO(beleg_pdf(gs))).pages[0].extract_text().split())
        self.assertIn("Grund der Gutschrift: Kulanz wegen Lieferverzug <b>", text)
        self.assertContains(self.client.get(reverse("verkauf:beleg", args=[gs.pk])), "Grund: Kulanz wegen Lieferverzug &lt;b&gt;")

    def test_modell_verlangt_grund(self):
        gs = Beleg.objects.create(mandant=self.m, art=Belegart.GUTSCHRIFT, kunde=self.kunde)
        self.pos(gs)
        with self.assertRaises(GutschriftGrundFehlt):
            gs.abschliessen()
        gs.abschliessen(gutschrift_grund="Preisnachlass")
        self.assertEqual(Beleg.objects.get(pk=gs.pk).gutschrift_grund, "Preisnachlass")

    def test_gutschrift_zur_rechnung_braucht_keinen_grund(self):
        re = Beleg.objects.create(mandant=self.m, art=Belegart.RECHNUNG, kunde=self.kunde)
        Position.objects.create(beleg=re, bezeichnung="Dienst", einzelpreis=D("10"))
        re.abschliessen()
        gs = re.gutschrift_erzeugen(ruecknahme=Ruecknahme.KEINE)
        self.assertFalse(gs.freie_gutschrift)
        gs.abschliessen()
        self.assertTrue(Beleg.objects.get(pk=gs.pk).abgeschlossen)
        self.assertEqual(Beleg.objects.get(pk=gs.pk).gutschrift_grund, "")

    def test_spalte_b_standard_keine_ware_zurueck_und_umschaltbar(self):
        gs = self.neu()
        p = self.pos(gs)
        self.assertEqual(p.ruecknahme, Ruecknahme.KEINE)
        self.assertEqual(self.pos(gs).ruecknahme, Ruecknahme.KEINE)  # weitere Positionen folgen der ersten
        gs.abschliessen(gutschrift_grund="Kulanz")
        self.assertEqual((bestand(self.m, self.maus), reparaturbestand(self.m, self.maus)), (D("0"), D("0")))

    def test_ware_zurueck_ins_lager_ohne_rechnung(self):
        gs = self.neu()
        p = self.pos(gs, menge="3")
        Position.objects.filter(pk=p.pk).update(ruecknahme=Ruecknahme.LAGER)
        gs.abschliessen(gutschrift_grund="Rückgabe ohne Rechnung")
        self.assertEqual(bestand(self.m, self.maus), D("3"))

    def test_guthaben_in_offenen_posten_und_verrechnung(self):
        gs = self.neu()
        self.pos(gs, menge="1", preis="10")
        gs.abschliessen(gutschrift_grund="Kulanz")
        gs.refresh_from_db()
        self.assertEqual(gutschrift_rest(gs), D("11.90"))
        self.assertEqual(kunde_offen(self.m, self.kunde), D("-11.90"))
        re = Beleg.objects.create(mandant=self.m, art=Belegart.RECHNUNG, kunde=self.kunde)
        Position.objects.create(beleg=re, bezeichnung="Dienst", einzelpreis=D("20"))
        re.abschliessen()
        gutschrift_verrechnen(gs, re, D("11.90"))
        self.assertEqual((offen(re), gutschrift_rest(gs)), (D("11.90"), D("0")))
        self.assertContains(self.client.get(reverse("finanzen:beleg", args=[gs.pk])), gs.nummer)

    def test_gesperrter_kunde_bekommt_keine_gutschrift(self):
        self.kunde.gesperrt = True
        self.kunde.save()
        self.client.post(reverse("verkauf:beleg_neu", args=[self.kunde.pk]), {"art": "GS"})
        self.assertFalse(Beleg.objects.exists())

    def test_lager_vorher_vorhanden_bleibt_unberuehrt_bei_keine(self):
        buche_manuell(self.m, self.maus, Bewegungsart.WARENEINGANG, D("5"))
        gs = self.neu()
        self.pos(gs)
        gs.abschliessen(gutschrift_grund="Preisnachlass")
        self.assertEqual(bestand(self.m, self.maus), D("5"))

    def test_entwurf_bleibt_beim_filter_ohne_storno_sichtbar(self):
        gs = self.neu()
        self.pos(gs)
        r = self.client.get(reverse("verkauf:kunde", args=[self.kunde.pk]), {"ohne_storno": "1"})
        self.assertIn(gs.pk, [z["beleg"].pk for z in r.context["zeilen"]])
        re = Beleg.objects.create(mandant=self.m, art=Belegart.RECHNUNG, kunde=self.kunde)
        Position.objects.create(beleg=re, bezeichnung="Dienst", einzelpreis=D("10"))
        re.abschliessen()
        zur_rechnung = re.gutschrift_erzeugen(ruecknahme=Ruecknahme.KEINE)  # Entwurf zur Rechnung: weiter ausgeblendet
        r = self.client.get(reverse("verkauf:kunde", args=[self.kunde.pk]), {"ohne_storno": "1"})
        self.assertNotIn(zur_rechnung.pk, [z["beleg"].pk for z in r.context["zeilen"]])


@override_settings(SECURE_SSL_REDIRECT=False)
class GutschriftAuszahlenTest(TestCase):
    """Entscheidung des Nutzers 19.11.2026: Der freie Betrag einer Gutschrift lässt sich direkt an den Kunden auszahlen
    (negative Zahlung auf der Gutschrift, höchstens bis zum freien Betrag), auch wenn der Kunde keine Rechnung hat."""

    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.kunde = Kunde.objects.create(nummer="1000", name1="Muster GmbH")
        self.client.force_login(get_user_model().objects.create_superuser("chef", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()
        self.gs = Beleg.objects.create(mandant=self.m, art=Belegart.GUTSCHRIFT, kunde=self.kunde)
        Position.objects.create(beleg=self.gs, bezeichnung="Kulanz", einzelpreis=D("100"))
        self.gs.abschliessen(gutschrift_grund="Kulanz")
        self.gs.refresh_from_db()  # 119,00 brutto
        self.url = reverse("finanzen:gutschrift_auszahlen", args=[self.gs.pk])

    def auszahlen(self, betrag, frei_bisher=None, zahlart="ue"):
        daten = {"betrag": betrag, "datum": "2026-11-19", "zahlart": zahlart}
        if frei_bisher is not None:
            daten["frei_bisher"] = frei_bisher
        return self.client.post(self.url, daten, follow=True)

    def test_auszahlung_ohne_rechnung_mindert_den_freien_betrag(self):
        from apps.finanzen.models import Zahlung
        seite = self.client.get(reverse("finanzen:beleg", args=[self.gs.pk]))
        self.assertContains(seite, "Auszahlung an den Kunden")
        self.assertContains(seite, 'name="frei_bisher" value="119,00"')
        self.assertNotContains(seite, '<option value="sk">')
        r = self.auszahlen("50,00", "119,00")
        self.assertContains(r, "ausgezahlt")
        self.assertEqual(Zahlung.objects.get().betrag, D("-50.00"))
        self.assertEqual((gutschrift_rest(self.gs), kunde_offen(self.m, self.kunde)), (D("69.00"), D("-69.00")))
        seite = self.client.get(reverse("finanzen:beleg", args=[self.gs.pk]))
        self.assertContains(seite, "Auszahlung aus Gutschrift")
        self.assertContains(seite, 'name="frei_bisher" value="69,00"')
        kunde = self.client.get(reverse("verkauf:kunde", args=[self.kunde.pk]))
        self.assertEqual(next(z for z in kunde.context["zeilen"] if z["beleg"].pk == self.gs.pk)["offen"], D("-69.00"))

    def test_nicht_mehr_als_frei_und_doppelklick(self):
        from apps.finanzen.models import Zahlung
        self.assertContains(self.auszahlen("119,01"), "nur noch 119,00 € frei")
        self.auszahlen("119,00", "119,00")
        self.assertContains(self.auszahlen("119,00", "119,00"), "hat sich inzwischen geändert")
        self.assertEqual(Zahlung.objects.count(), 1)
        self.assertEqual(gutschrift_rest(self.gs), D("0"))
        self.assertContains(self.client.get(reverse("finanzen:beleg", args=[self.gs.pk])), "Nichts mehr frei")
        from apps.finanzen.services import offene_gutschriften
        self.assertNotIn(self.gs.pk, [z["beleg"].pk for z in offene_gutschriften(self.m)])

    def test_kein_zahlungseingang_und_null_auf_gutschrift(self):
        from apps.finanzen.services import zahlung_buchen
        from django.utils import timezone
        with self.assertRaisesMessage(ValueError, "nur ausgezahlt"):
            zahlung_buchen(self.gs, D("10"), timezone.localdate(), "ue")
        self.assertContains(self.auszahlen("0"), "größer als 0")

    def test_storno_der_auszahlung_gibt_den_betrag_wieder_frei(self):
        from apps.finanzen.models import Zahlung
        from apps.finanzen.services import zahlung_stornieren
        self.auszahlen("119,00")
        zahlung_stornieren(Zahlung.objects.get(), "falsches Konto")
        self.assertEqual(gutschrift_rest(self.gs), D("119.00"))

    def test_verrechnen_nach_auszahlung_nur_noch_der_rest(self):
        self.auszahlen("100,00")
        re = Beleg.objects.create(mandant=self.m, art=Belegart.RECHNUNG, kunde=self.kunde)
        Position.objects.create(beleg=re, bezeichnung="Dienst", einzelpreis=D("100"))
        re.abschliessen()
        with self.assertRaisesMessage(ValueError, "nur noch 19,00"):
            gutschrift_verrechnen(self.gs, re, D("50"))
        gutschrift_verrechnen(self.gs, re, D("19"))
        self.assertEqual(gutschrift_rest(self.gs), D("0"))

    def test_bar_auszahlung_laeuft_ins_kassenbuch_und_datev(self):
        from datetime import date
        from apps.finanzen import datev
        from apps.finanzen.models import Kassenbuch
        from django.utils import timezone
        from apps.finanzen.services import kasse_buchen
        self.assertContains(self.auszahlen("20,00", zahlart="ba"), "Kasse darf nicht ins Minus")  # leere Kasse: nichts gebucht
        self.assertEqual(gutschrift_rest(self.gs), D("119.00"))
        kasse_buchen(self.m, Kassenbuch.EINNAHME, D("50"), "Einlage", timezone.localdate())
        heute = timezone.localdate().isoformat()
        self.client.post(self.url, {"betrag": "20,00", "datum": heute, "zahlart": "ba"})
        self.assertEqual(Kassenbuch.objects.order_by("-nummer").first().art, Kassenbuch.AUSGABE)
        self.m.datev_zahlungen = True  # Zahlungen im Export eingeschaltet (seit 25.11.2026 aus als Standard)
        text = datev.export(self.m, date(2026, 1, 1), date(2026, 12, 31)).decode("cp1252")
        self.assertIn(f"20,00;H;EUR;;;;", text)

    def test_fremder_mandant_und_rechnung_nicht_ueber_diesen_weg(self):
        re = Beleg.objects.create(mandant=self.m, art=Belegart.RECHNUNG, kunde=self.kunde)
        self.assertEqual(self.client.post(reverse("finanzen:gutschrift_auszahlen", args=[re.pk]), {"betrag": "1"}).status_code, 404)
        s = self.client.session
        s["mandant_id"] = Mandant.objects.create(kurzname="ZONE", firmenname="ZONE Handel").pk
        s.save()
        self.assertEqual(self.client.post(self.url, {"betrag": "1"}).status_code, 404)

    def test_skonto_ist_keine_auszahlung(self):
        from apps.finanzen.services import zahlung_buchen
        from django.utils import timezone
        self.assertContains(self.auszahlen("10,00", zahlart="sk"), "Zahlart für die Auszahlung")
        with self.assertRaisesMessage(ValueError, "Skonto ist hier nicht möglich"):
            zahlung_buchen(self.gs, D("-10"), timezone.localdate(), "sk")
        self.assertEqual(gutschrift_rest(self.gs), D("119.00"))

    def test_dashboard_zaehlt_ausgezahlte_gutschrift_nicht_mehr(self):
        from django.utils import timezone
        from apps.kern import dashboard
        self.assertEqual(dashboard.offene_posten(self.m, timezone.localdate())["betrag"], D("-119.00"))
        self.auszahlen("119,00")
        self.assertEqual(dashboard.offene_posten(self.m, timezone.localdate())["betrag"], D("0"))

    def test_kopf_der_gutschrift_zeigt_nur_ihre_eigenen_werte(self):
        re = Beleg.objects.create(mandant=self.m, art=Belegart.RECHNUNG, kunde=self.kunde)
        Position.objects.create(beleg=re, bezeichnung="Dienst", einzelpreis=D("200"))
        re.abschliessen()
        gs1 = re.gutschrift_erzeugen(ruecknahme=Ruecknahme.KEINE)
        gs1.positionen.update(einzelpreis=D("50"))  # Teilgutschrift, damit eine zweite möglich ist
        gs1.berechne()
        gs1.abschliessen()
        gs2 = re.gutschrift_erzeugen(ruecknahme=Ruecknahme.KEINE)
        gs2.positionen.update(einzelpreis=D("10"))
        gs2.berechne()
        gs2.abschliessen()
        self.client.post(reverse("finanzen:gutschrift_auszahlen", args=[gs2.pk]), {"betrag": "5,00", "datum": "2026-11-19", "zahlart": "ue"})
        r = self.client.get(reverse("finanzen:beleg", args=[gs1.pk]))
        self.assertIsNone(r.context["gs_ausgezahlt"] or None)  # die Auszahlung von gs2 gehört nicht in den Kopf von gs1
        self.assertEqual(self.client.get(reverse("finanzen:beleg", args=[gs2.pk])).context["gs_ausgezahlt"], D("5.00"))


@override_settings(SECURE_SSL_REDIRECT=False)
class BelegListeNurNummerTest(TestCase):
    """Wunsch des Nutzers 19.11.2026: In Belegtabellen steht nur die Nummer (RE…, AB…), die Belegart nur bei Belegen ohne Nummer."""

    def test_kunde_und_alle_belege(self):
        m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        kunde = Kunde.objects.create(nummer="1000", name1="Muster GmbH")
        self.client.force_login(get_user_model().objects.create_superuser("chef", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = m.pk
        s.save()
        re = Beleg.objects.create(mandant=m, art=Belegart.RECHNUNG, kunde=kunde)
        Position.objects.create(beleg=re, bezeichnung="Dienst", einzelpreis=D("10"))
        re.abschliessen()
        re.refresh_from_db()
        entwurf = Beleg.objects.create(mandant=m, art=Belegart.RECHNUNG, kunde=kunde)  # ohne Nummer bis zum Abschluss
        Position.objects.create(beleg=entwurf, bezeichnung="Dienst", einzelpreis=D("10"))
        for url in (reverse("verkauf:kunde", args=[kunde.pk]), reverse("verkauf:belege") + "?status=alle"):
            r = self.client.get(url)
            self.assertContains(r, f'title="Rechnung">{re.nummer}</a>')
            self.assertNotContains(r, f"Rechnung {re.nummer}</a>")
            self.assertContains(r, 'title="Rechnung">Rechnung (nicht abgeschlossen)</a>')


@override_settings(SECURE_SSL_REDIRECT=False)
class GutschriftVerrechnenSeiteTest(TestCase):
    """Wunsch des Nutzers 19.11.2026: Auf der Zahlungsseite der Gutschrift mit einer (anderen) Rechnung verrechnen, auch teilweise.
    RE 100 / GS 50 → Rechnung bleibt 50 offen; GS 100 / RE 50 → Gutschrift bleibt 50 frei (auszahlen oder weiter verrechnen)."""

    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.kunde = Kunde.objects.create(nummer="1000", name1="Muster GmbH")
        self.client.force_login(get_user_model().objects.create_superuser("chef", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    def beleg(self, art, netto, **extra):
        b = Beleg.objects.create(mandant=self.m, art=art, kunde=self.kunde)
        Position.objects.create(beleg=b, bezeichnung="Dienst", einzelpreis=D(netto), steuer_prozent=D("0"))
        b.abschliessen(**extra)
        b.refresh_from_db()
        return b

    def verrechnen(self, gs, re, betrag):
        return self.client.post(reverse("finanzen:gutschrift_verrechnen", args=[re.pk]),
                                {"gutschrift": gs.pk, "zurueck": "gutschrift", "offen_bisher": f"{offen(re):.2f}".replace(".", ","),
                                 "betrag": betrag}, follow=True)

    def test_rechnung_100_gutschrift_50(self):
        re = self.beleg(Belegart.RECHNUNG, "100")
        gs = self.beleg(Belegart.GUTSCHRIFT, "50", gutschrift_grund="Kulanz")
        seite = self.client.get(reverse("finanzen:beleg", args=[gs.pk]))
        self.assertContains(seite, "Mit Rechnung verrechnen")
        self.assertContains(seite, 'name="betrag" value="50,00"')  # Vorschlag: höchstens der freie Betrag
        r = self.verrechnen(gs, re, "50,00")
        self.assertEqual(r.redirect_chain[-1][0], reverse("finanzen:beleg", args=[gs.pk]))
        self.assertEqual((offen(re), gutschrift_rest(gs)), (D("50.00"), D("0.00")))

    def test_gutschrift_100_rechnung_50_rest_weiter_verrechnen_oder_auszahlen(self):
        gs = self.beleg(Belegart.GUTSCHRIFT, "100", gutschrift_grund="Kulanz")
        re1 = self.beleg(Belegart.RECHNUNG, "50")
        re2 = self.beleg(Belegart.RECHNUNG, "30")
        seite = self.client.get(reverse("finanzen:beleg", args=[gs.pk]))
        self.assertContains(seite, re1.nummer)
        self.assertContains(seite, re2.nummer)
        self.verrechnen(gs, re1, "50,00")
        self.assertEqual((offen(re1), gutschrift_rest(gs)), (D("0.00"), D("50.00")))
        self.verrechnen(gs, re2, "30,00")  # Rest mit einer weiteren Rechnung
        self.assertEqual(gutschrift_rest(gs), D("20.00"))
        self.client.post(reverse("finanzen:gutschrift_auszahlen", args=[gs.pk]),
                         {"betrag": "20,00", "datum": "2026-11-19", "zahlart": "ue"})  # Rest überweisen
        self.assertEqual(gutschrift_rest(gs), D("0.00"))
        self.assertNotContains(self.client.get(reverse("finanzen:beleg", args=[gs.pk])), "Mit Rechnung verrechnen")

    def test_ohne_offene_rechnung_hinweis_auf_auszahlung(self):
        gs = self.beleg(Belegart.GUTSCHRIFT, "10", gutschrift_grund="Kulanz")
        self.assertContains(self.client.get(reverse("finanzen:beleg", args=[gs.pk])), "keine offene Rechnung")
