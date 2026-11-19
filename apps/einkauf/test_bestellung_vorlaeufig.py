"""Wunsch des Nutzers 19.11.2026: Lieferantenbestellungen werden wie die Kundenbestellung im Verkauf abgewickelt. „+ Bestellung“ und
„In Lieferantenbestellung kopieren“ legen eine vorläufige Bestellung ohne Nummer an; die Nummer vergibt erst „Speichern: abschließen &
Nummer vergeben“, beim Verlassen fragt ein Fenster Abschließen / Verwerfen / Zurück, ohne Lebenszeichen wird verworfen."""
from datetime import timedelta
from decimal import Decimal as D

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.kern.models import Mandant
from apps.stammdaten.models import Artikel, ArtikelLieferant, Kunde, Lieferant, Steuersatz
from apps.verkauf.models import VERLASSEN_SEKUNDEN, VORLAEUFIG_MINUTEN, Beleg, Belegart, Nummernkreis, Position

from . import services, vergleich
from .models import BestellPosition, BestellStatus, Bestellung, Wareneingang


@override_settings(SECURE_SSL_REDIRECT=False)
class BestellungVorlaeufigTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.m2 = Mandant.objects.create(kurzname="ZONE", firmenname="ZONE Handel")
        self.lf = Lieferant.objects.create(nummer="L1", name1="Techdata GmbH", kurzname="TD")
        self.lf2 = Lieferant.objects.create(nummer="L2", name1="Wave AG", kurzname="WAVE")
        st = Steuersatz.objects.create(bezeichnung="19", prozent=D("19"))
        self.maus = Artikel.objects.create(nummer="M1", bezeichnung="Maus", steuersatz=st, vk1=D("15"))
        self.tast = Artikel.objects.create(nummer="T1", bezeichnung="Tastatur", steuersatz=st, vk1=D("25"))
        ArtikelLieferant.objects.create(artikel=self.maus, lieferant=self.lf, bestellnummer="TD-1", ek=D("5"), bestand=10)
        self.user = get_user_model().objects.create_superuser("chef", password="x" * 14)
        self.client.force_login(self.user)
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    # ---- Hilfen ------------------------------------------------------------------------------------

    def neu(self, lieferant=None, mandant=None):
        return services.bestellung_anlegen(mandant or self.m, lieferant or self.lf, vorlaeufig=True)

    def mit_position(self, lieferant=None):
        b = self.neu(lieferant)
        services.bestellposition(b, self.maus, D("2"), ek=D("5"))
        return b

    def seite(self, b):
        return self.client.get(reverse("einkauf:bestellung", args=[b.pk]))

    def alt(self, b, minuten=VORLAEUFIG_MINUTEN + 5):
        Bestellung.objects.filter(pk=b.pk).update(zuletzt_aktiv=timezone.now() - timedelta(minutes=minuten))

    # ---- Anlegen -----------------------------------------------------------------------------------

    def test_plus_bestellung_legt_vorlaeufig_ohne_nummer_an(self):
        r = self.client.post(reverse("einkauf:bestellung_neu"), {"lieferant": self.lf.pk})
        b = Bestellung.objects.get()
        self.assertRedirects(r, reverse("einkauf:bestellung", args=[b.pk]))
        self.assertTrue(b.vorlaeufig)
        self.assertIsNone(b.nummer)
        self.assertIsNotNone(b.zuletzt_aktiv)
        self.assertEqual(b.status, BestellStatus.ENTWURF)
        self.assertEqual(b.angelegt_von, self.user)
        self.assertFalse(Nummernkreis.objects.filter(mandant=self.m, art="BE").exists())  # nichts verbraucht
        self.assertEqual(str(b), "Bestellung (nicht gespeichert)")

    def test_seite_zeigt_nicht_gespeichert_und_symbol(self):
        b = self.neu()
        html = self.seite(b).content.decode()
        self.assertIn("Bestellung (nicht gespeichert)", html)
        self.assertIn("nicht gespeichert</span>", html)
        self.assertIn('id="abschlussform"', html)
        self.assertNotIn("None", html.split("<h1>")[1].split("</h1>")[0])

    def test_gesperrter_lieferant_legt_nichts_an(self):
        self.lf.gesperrt = True
        self.lf.save()
        self.client.post(reverse("einkauf:bestellung_neu"), {"lieferant": self.lf.pk})
        self.assertFalse(Bestellung.objects.exists())

    # ---- Abschließen -------------------------------------------------------------------------------

    def test_abschliessen_vergibt_nummer_lueckenlos(self):
        a, b = self.mit_position(), self.mit_position()
        self.assertTrue(services.bestellung_abschliessen(b))  # die später angelegte zuerst: Nummern folgen dem Abschluss
        self.assertTrue(services.bestellung_abschliessen(a))
        a.refresh_from_db()
        b.refresh_from_db()
        jahr = timezone.localdate().year
        self.assertEqual((b.nummer, a.nummer), (f"BE{jahr}-00001", f"BE{jahr}-00002"))
        self.assertEqual((a.vorlaeufig, a.zuletzt_aktiv, a.status), (False, None, BestellStatus.ENTWURF))
        self.assertEqual(Nummernkreis.objects.get(mandant=self.m, art="BE").letzte_nummer, 2)

    def test_doppelt_abgeschlossen_nummer_nur_einmal(self):
        b = self.mit_position()
        zweiter_tab = Bestellung.objects.get(pk=b.pk)  # veralteter Stand: noch vorläufig
        self.assertTrue(services.bestellung_abschliessen(b))
        nummer = b.nummer
        self.assertFalse(services.bestellung_abschliessen(zweiter_tab))
        self.assertEqual(zweiter_tab.nummer, nummer)  # der zweite Aufruf liest den Zustand neu
        self.assertEqual(Nummernkreis.objects.get(mandant=self.m, art="BE").letzte_nummer, 1)
        r = self.client.post(reverse("einkauf:bestellung_abschliessen", args=[b.pk]))  # Doppelklick über die Oberfläche
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Nummernkreis.objects.get(mandant=self.m, art="BE").letzte_nummer, 1)

    def test_ohne_position_kein_abschluss(self):
        b = self.neu()
        with self.assertRaisesMessage(ValueError, "Ohne Position"):
            services.bestellung_abschliessen(b)
        r = self.client.post(reverse("einkauf:bestellung_abschliessen", args=[b.pk]), follow=True)
        self.assertContains(r, "Ohne Position")
        b.refresh_from_db()
        self.assertTrue(b.vorlaeufig)
        self.assertIsNone(b.nummer)
        self.assertFalse(Nummernkreis.objects.filter(mandant=self.m, art="BE").exists())

    def test_abschliessen_ueber_die_oberflaeche(self):
        b = self.mit_position()
        r = self.client.post(reverse("einkauf:bestellung_abschliessen", args=[b.pk]), follow=True)
        b.refresh_from_db()
        self.assertFalse(b.vorlaeufig)
        self.assertContains(r, b.nummer)
        self.assertNotContains(r, 'id="vorlaeufigskript"')

    def test_abgeschlossene_bestellung_wird_nicht_mehr_verworfen(self):
        b = self.mit_position()
        services.bestellung_abschliessen(b)
        self.assertFalse(services.bestellung_verwerfen(b))
        self.assertEqual(Bestellung.objects.filter(pk=b.pk).count(), 1)
        r = self.client.post(reverse("einkauf:bestellung_verwerfen", args=[b.pk]), headers={"X-Wwok-Stand": "1"})
        self.assertFalse(r.json()["ok"])
        self.assertEqual(Bestellung.objects.filter(pk=b.pk).count(), 1)

    # ---- Verwerfen und Lebenszeichen --------------------------------------------------------------

    def test_verwerfen_loescht_mit_positionen(self):
        b = self.mit_position()
        r = self.client.post(reverse("einkauf:bestellung_verwerfen", args=[b.pk]), headers={"X-Wwok-Stand": "1"})
        self.assertEqual(r.json(), {"ok": True, "weiter": reverse("einkauf:lieferant", args=[self.lf.pk])})
        self.assertFalse(Bestellung.objects.exists())
        self.assertFalse(BestellPosition.objects.exists())
        self.assertFalse(Nummernkreis.objects.filter(mandant=self.m, art="BE").exists())  # keine Lücke im Kreis
        r = self.client.post(reverse("einkauf:bestellung_verwerfen", args=[b.pk]), headers={"X-Wwok-Stand": "1"})  # zweiter Klick
        self.assertTrue(r.json()["ok"])

    def test_verwerfen_ohne_json_mit_meldung(self):
        b = self.neu()
        r = self.client.post(reverse("einkauf:bestellung_verwerfen", args=[b.pk]), follow=True)
        self.assertContains(r, "verworfen")
        self.assertFalse(Bestellung.objects.exists())

    def test_loeschen_einer_vorlaeufigen_bestellung_verwirft(self):
        b = self.neu()
        self.client.post(reverse("einkauf:bestellung_loeschen", args=[b.pk]))
        self.assertFalse(Bestellung.objects.exists())

    def test_verwerfen_mit_wareneingang_wird_abgelehnt(self):
        b = self.mit_position()
        Wareneingang.objects.create(mandant=self.m, lieferant=self.lf, bestellung=b, nummer="WE-X")  # kann nicht vorkommen: abgesichert
        with self.assertRaisesMessage(ValueError, "Wareneingänge"):
            services.bestellung_verwerfen(b)
        r = self.client.post(reverse("einkauf:bestellung_verwerfen", args=[b.pk]), headers={"X-Wwok-Stand": "1"})
        self.assertEqual(r.status_code, 409)
        self.assertTrue(Bestellung.objects.filter(pk=b.pk).exists())

    def test_ohne_lebenszeichen_wird_verworfen_mit_nicht(self):
        alt, frisch, gespeichert = self.mit_position(), self.mit_position(), self.mit_position()
        services.bestellung_abschliessen(gespeichert)
        self.alt(alt)
        self.alt(gespeichert)  # gespeichert hat zuletzt_aktiv leer: bleibt auch ohne Lebenszeichen
        Bestellung.objects.filter(pk=gespeichert.pk).update(zuletzt_aktiv=timezone.now() - timedelta(hours=9))
        self.assertEqual(services.vorlaeufige_bestellungen_verwerfen(self.m), 1)
        self.assertEqual(set(Bestellung.objects.values_list("pk", flat=True)), {frisch.pk, gespeichert.pk})
        self.assertEqual(services.vorlaeufige_bestellungen_verwerfen(self.m), 0)

    def test_puls_haelt_die_bestellung_am_leben(self):
        b = self.mit_position()
        self.alt(b)
        r = self.client.post(reverse("einkauf:bestellung_aktiv", args=[b.pk]))
        self.assertEqual(r.json(), {"ok": True})
        self.assertEqual(services.vorlaeufige_bestellungen_verwerfen(self.m), 0)
        self.assertTrue(Bestellung.objects.filter(pk=b.pk).exists())

    def test_puls_meldet_verworfene_und_gespeicherte(self):
        b = self.mit_position()
        services.bestellung_abschliessen(b)
        r = self.client.post(reverse("einkauf:bestellung_aktiv", args=[b.pk])).json()
        self.assertEqual((r["ok"], r["weiter"]), (False, reverse("einkauf:bestellung", args=[b.pk])))
        weg = self.neu()
        weg_pk = weg.pk
        weg.delete()
        r = self.client.post(reverse("einkauf:bestellung_aktiv", args=[weg_pk])).json()
        self.assertEqual((r["ok"], r["weiter"]), (False, reverse("einkauf:bestellungen")))

    def test_seitenaufruf_und_aenderung_zaehlen_als_aktivitaet(self):
        b = self.neu()
        self.alt(b)
        self.seite(b)
        self.assertEqual(services.vorlaeufige_bestellungen_verwerfen(self.m), 0)
        self.alt(b)
        self.client.post(reverse("einkauf:bestellung_position", args=[b.pk]), {"artikel": "M1", "menge": "1"})
        self.assertEqual(services.vorlaeufige_bestellungen_verwerfen(self.m), 0)
        self.alt(b)
        self.client.post(reverse("einkauf:bestellung_kopf", args=[b.pk]), {"versandart": "DHL", "tab": "versand"})
        b.refresh_from_db()
        self.assertEqual(b.versandart, "DHL")  # Tab „Versand & Konditionen“ funktioniert bei vorläufigen Bestellungen
        self.assertGreater(b.zuletzt_aktiv, timezone.now() - timedelta(minutes=1))

    def test_verlassen_signal_verwirft_bei_der_naechsten_bereinigung(self):
        b = self.mit_position()
        r = self.client.post(reverse("einkauf:bestellung_verlassen", args=[b.pk]))
        self.assertEqual(r.status_code, 204)
        b.refresh_from_db()
        self.assertTrue(b.vorlaeufig)  # erst nach VERLASSEN_SEKUNDEN
        self.assertEqual(services.vorlaeufige_bestellungen_verwerfen(self.m), 0)
        spaeter = timezone.now() + timedelta(seconds=VERLASSEN_SEKUNDEN + 5)
        self.assertEqual(services.vorlaeufige_bestellungen_verwerfen(self.m, jetzt=spaeter), 1)
        self.assertFalse(Bestellung.objects.exists())

    def test_neuladen_nach_verlassen_haelt_die_bestellung(self):
        b = self.mit_position()
        self.client.post(reverse("einkauf:bestellung_verlassen", args=[b.pk]))
        self.seite(b)  # F5: der Aufruf zählt wieder als Lebenszeichen
        spaeter = timezone.now() + timedelta(seconds=VERLASSEN_SEKUNDEN + 5)
        self.assertEqual(services.vorlaeufige_bestellungen_verwerfen(self.m, jetzt=spaeter), 0)

    def test_oeffnen_der_listen_raeumt_auf(self):
        a, b = self.mit_position(), self.mit_position(self.lf2)
        self.alt(a)
        self.alt(b)
        self.client.get(reverse("einkauf:lieferant", args=[self.lf.pk]))  # nur dieser Lieferant
        self.assertEqual(list(Bestellung.objects.values_list("pk", flat=True)), [b.pk])
        self.client.get(reverse("einkauf:bestellungen"))
        self.assertFalse(Bestellung.objects.exists())

    def test_leere_vorlaeufige_wird_nicht_nach_zwei_minuten_entfernt(self):
        b = self.neu()
        Bestellung.objects.filter(pk=b.pk).update(geaendert_am=timezone.now() - timedelta(minutes=10))
        self.assertEqual(services.leere_bestellungen_entfernen(self.m), 0)  # gilt das Lebenszeichen, nicht die 2 Minuten
        self.assertTrue(Bestellung.objects.filter(pk=b.pk).exists())

    def test_fremder_mandant_wird_nicht_verworfen(self):
        fremd = self.neu(mandant=self.m2)
        self.alt(fremd)
        self.assertEqual(services.vorlaeufige_bestellungen_verwerfen(self.m), 0)
        self.assertTrue(Bestellung.objects.filter(pk=fremd.pk).exists())

    # ---- Vom Programm in einem Rutsch angelegte Bestellungen bleiben mit Nummer -----------------

    def test_vorschlag_legt_weiter_mit_nummer_an(self):
        liste = services.bestellungen_aus_vorschlag(self.m, [(self.maus.pk, D("3"))])
        self.assertEqual(len(liste), 1)
        self.assertTrue(liste[0].nummer.startswith("BE"))
        self.assertFalse(liste[0].vorlaeufig)

    def test_preisvergleich_legt_weiter_mit_nummer_an(self):
        b = vergleich.bestellen(self.m, self.lf.pk, D("1"), artikel=self.maus)
        self.assertTrue(b.nummer.startswith("BE"))
        self.assertFalse(b.vorlaeufig)

    def test_vergleich_nimmt_keine_fremde_vorlaeufige_bestellung(self):
        offen = self.neu()  # in einem anderen Tab geöffnet
        b = vergleich.bestellen(self.m, self.lf.pk, D("1"), artikel=self.maus)
        self.assertNotEqual(b.pk, offen.pk)
        self.assertEqual(offen.positionen.count(), 0)
        b2 = vergleich.bestellen(self.m, self.lf.pk, D("1"), artikel=self.maus, bevorzugt=offen)  # die bearbeitete: ja
        self.assertEqual(b2.pk, offen.pk)

    # ---- aus Kundenbeleg ---------------------------------------------------------------------------

    def kundenbeleg(self, *artikel):
        ab = Beleg.objects.create(mandant=self.m, art=Belegart.AUFTRAG, kunde=Kunde.objects.create(nummer="1", name1="K"))
        for a in artikel:
            Position.objects.create(beleg=ab, artikel=a, bezeichnung=a.bezeichnung, menge=D("2"), einzelpreis=D("10"))
        return ab

    def test_aus_beleg_ein_lieferant_ist_vorlaeufig(self):
        ab = self.kundenbeleg(self.maus)
        r = self.client.post(reverse("einkauf:aus_beleg", args=[ab.pk]), follow=True)
        b = Bestellung.objects.get()
        self.assertTrue(b.vorlaeufig)
        self.assertIsNone(b.nummer)
        self.assertEqual((b.aus_beleg, b.positionen.count()), (ab, 1))
        self.assertRedirects(r, reverse("einkauf:bestellung", args=[b.pk]))
        self.assertContains(r, "Bestellung (nicht gespeichert)")
        self.assertContains(self.client.get(reverse("verkauf:beleg", args=[ab.pk])), "(nicht gespeichert)")  # Anzeige am Kundenbeleg

    def test_aus_beleg_mehrere_lieferanten_alle_mit_nummer(self):
        ArtikelLieferant.objects.create(artikel=self.tast, lieferant=self.lf2, bestellnummer="W-1", ek=D("7"), bestand=5)
        ab = self.kundenbeleg(self.maus, self.tast)
        liste = services.bestellungen_aus_beleg(ab, vorlaeufig_bei_einer=True, wahl={p.pk: (self.lf if p.artikel == self.maus else self.lf2).pk
                                                                                       for p in ab.positionen.all()})
        self.assertEqual(len(liste), 2)
        for b in liste:
            self.assertFalse(b.vorlaeufig)
            self.assertTrue(b.nummer.startswith("BE"))

    def test_service_ohne_schalter_bleibt_mit_nummer(self):
        liste = services.bestellungen_aus_beleg(self.kundenbeleg(self.maus))
        self.assertTrue(liste[0].nummer and not liste[0].vorlaeufig)

    # ---- Mail, bestellt, PDF, Wareneingang verlangen den Abschluss -------------------------------

    def test_service_bestellen_und_wareneingang_verlangen_abschluss(self):
        b = self.mit_position()
        with self.assertRaisesMessage(ValueError, "noch nicht gespeichert"):
            services.bestellen(b)
        with self.assertRaisesMessage(ValueError, "noch nicht gespeichert"):
            services.wareneingang_anlegen(self.m, self.lf, b)
        self.assertEqual(b.status, BestellStatus.ENTWURF)

    def test_mail_pdf_bestellt_wareneingang_fragen_erst(self):
        b = self.mit_position()
        for url in (reverse("einkauf:bestellung_mail", args=[b.pk]), reverse("einkauf:bestellung_pdf", args=[b.pk])):
            r = self.client.get(url)
            self.assertContains(r, "Erst abschließen")
            self.assertEqual(r.status_code, 200)
        r = self.client.post(reverse("einkauf:bestellung_bestellen", args=[b.pk]))
        self.assertContains(r, "Erst abschließen")
        r = self.client.post(reverse("einkauf:wareneingang_neu"), {"bestellung": b.pk})
        self.assertContains(r, "Erst abschließen")
        b.refresh_from_db()
        self.assertEqual((b.vorlaeufig, b.status, b.nummer), (True, BestellStatus.ENTWURF, None))
        self.assertFalse(Wareneingang.objects.exists())

    def test_bestaetigen_schliesst_ab_und_fuehrt_aktion_aus(self):
        url = lambda b: reverse("einkauf:bestellung_abschliessen", args=[b.pk])
        b = self.mit_position()
        self.client.post(url(b), {"danach": "bestellen"})
        b.refresh_from_db()
        self.assertEqual((b.vorlaeufig, b.status), (False, BestellStatus.BESTELLT))
        self.assertTrue(b.nummer)
        b = self.mit_position()
        r = self.client.post(url(b), {"danach": "mail"})
        self.assertRedirects(r, reverse("einkauf:bestellung_mail", args=[b.pk]), fetch_redirect_response=False)
        b.refresh_from_db()
        self.assertTrue(b.nummer)
        self.assertEqual(self.client.get(reverse("einkauf:bestellung_mail", args=[b.pk])).status_code, 200)  # jetzt ohne Rückfrage
        b = self.mit_position()
        r = self.client.post(url(b), {"danach": "pdf"})
        self.assertRedirects(r, reverse("einkauf:bestellung_pdf", args=[b.pk]), fetch_redirect_response=False)
        self.assertEqual(self.client.get(reverse("einkauf:bestellung_pdf", args=[b.pk]))["Content-Type"], "application/pdf")
        b = self.mit_position()
        self.client.post(url(b), {"danach": "wareneingang"})
        b.refresh_from_db()
        self.assertEqual((b.vorlaeufig, b.status), (False, BestellStatus.ENTWURF))  # Status bleibt, bis bestellt wird
        we = Wareneingang.objects.get()
        self.assertEqual(we.bestellung, b)

    def test_bestaetigen_ohne_position_bricht_ab(self):
        b = self.neu()
        self.client.post(reverse("einkauf:bestellung_abschliessen", args=[b.pk]), {"danach": "bestellen"})
        b.refresh_from_db()
        self.assertEqual((b.vorlaeufig, b.status, b.nummer), (True, BestellStatus.ENTWURF, None))

    def test_unbekannte_aktion_schliesst_nur_ab(self):
        b = self.mit_position()
        self.client.post(reverse("einkauf:bestellung_abschliessen", args=[b.pk]), {"danach": "unfug"})
        b.refresh_from_db()
        self.assertEqual((b.vorlaeufig, b.status), (False, BestellStatus.ENTWURF))

    # ---- Anzeige in Listen ------------------------------------------------------------------------

    def test_listen_stuerzen_nicht_ab_und_zeigen_nicht_gespeichert(self):
        b = self.mit_position()
        self.assertContains(self.client.get(reverse("einkauf:bestellungen")), "nicht gespeichert")
        self.assertContains(self.client.get(reverse("einkauf:bestellungen"), {"status": "alle"}), "nicht gespeichert")
        r = self.client.get(reverse("einkauf:lieferant", args=[self.lf.pk]), {"art": "alle"})
        self.assertContains(r, "nicht gespeichert")
        self.assertContains(r, reverse("einkauf:bestellung", args=[b.pk]))
        self.assertContains(self.client.get(reverse("einkauf:lieferanten")), "nicht gespeichert")  # offene Belege aller Lieferanten
        self.assertEqual(self.client.get(reverse("einkauf:wareneingaenge")).status_code, 200)
        self.assertEqual(self.client.get(reverse("einkauf:vorschlag")).status_code, 200)

    def test_lieferantenkonto_zeile_ohne_nummer(self):
        from . import lieferantenkonto
        self.mit_position()
        zeilen = lieferantenkonto.belege(self.m, self.lf, nur_offen=False)
        self.assertEqual([(z["art"], z["nummer"], z["vorlaeufig"]) for z in zeilen], [("BE", None, True)])
        self.mit_position()  # Sortierung mit mehreren nummernlosen Bestellungen
        self.assertEqual(len(lieferantenkonto.belege(self.m)), 2)

    def test_gemischte_liste_mit_nummerierten_und_vorlaeufigen(self):
        alt = services.bestellung_anlegen(self.m, self.lf)
        services.bestellposition(alt, self.maus, D("1"), ek=D("5"))
        self.mit_position()
        r = self.client.get(reverse("einkauf:bestellungen"))
        self.assertContains(r, alt.nummer)
        self.assertContains(r, "nicht gespeichert")

    def test_schnellsuche_findet_vorlaeufige_ueber_den_lieferanten(self):
        from apps.kern import schnellsuche
        self.mit_position()
        treffer = [t for t in schnellsuche.suchen("Techdata", self.m) if t["gruppe"] == "Bestellung"]
        self.assertEqual([t["titel"] for t in treffer], ["(nicht gespeichert)"])

    def test_rueckfrage_nach_beleg_zeigt_vorlaeufige_ohne_none(self):
        ab = self.kundenbeleg(self.maus)
        b = services.bestellungen_aus_beleg(ab, vorlaeufig_bei_einer=True)[0]
        services.bestellposition(b, self.maus, D("1"))
        # eine bestellte (nummerierte) Bestellung löst die Rückfrage aus; die vorläufige daneben darf sie nicht stören
        bestellt = services.bestellung_anlegen(self.m, self.lf, aus_beleg=ab)
        services.bestellposition(bestellt, self.maus, D("1"))
        services.bestellen(bestellt)
        r = self.client.post(reverse("einkauf:aus_beleg", args=[ab.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, ">None<")

    # ---- Verlassen-Fenster -------------------------------------------------------------------------

    def test_seite_enthaelt_verlassen_skript_mit_drei_knoepfen(self):
        html = self.seite(self.mit_position()).content.decode()
        self.assertIn('id="vorlaeufigskript"', html)
        for text in ("text: 'Abschließen'", "text: 'Verwerfen'", "text: 'Zurück'"):
            self.assertIn(text, html)
        self.assertIn("haupt: true", html)
        self.assertIn("sendBeacon", html)
        self.assertIn(reverse("einkauf:bestellung_aktiv", args=[Bestellung.objects.get().pk]), html)
        self.assertIn(reverse("einkauf:bestellung_verlassen", args=[Bestellung.objects.get().pk]), html)

    def test_gespeicherte_bestellung_hat_kein_verlassen_skript(self):
        b = self.mit_position()
        services.bestellung_abschliessen(b)
        html = self.seite(b).content.decode()
        self.assertNotIn("vorlaeufigskript", html)
        self.assertNotIn('id="abschlussform"', html)
        self.assertIn(reverse("einkauf:bestellung_pdf", args=[b.pk]), html)

    def test_aktionsleiste_vorlaeufig_mit_rueckfrage_formularen(self):
        html = self.seite(self.mit_position()).content.decode()
        self.assertEqual(html.count("data-bleibt data-erst>"), 4)  # E-Mail, bestellt, Wareneingang, PDF
        self.assertIn('name="danach" value="mail"', html)
        self.assertIn("Erst abschließen & Nummer vergeben?", html)

    # ---- Mandanten ---------------------------------------------------------------------------------

    def test_fremder_mandant_404(self):
        fremd = self.neu(mandant=self.m2)
        services.bestellposition(fremd, self.maus, D("1"), ek=D("5"))
        self.assertEqual(self.seite(fremd).status_code, 404)
        self.assertEqual(self.client.post(reverse("einkauf:bestellung_abschliessen", args=[fremd.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("einkauf:bestellung_mail", args=[fremd.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("einkauf:bestellung_pdf", args=[fremd.pk])).status_code, 404)
        # Puls, Verlassen und Verwerfen eines fremden Mandanten ändern nichts
        self.assertFalse(self.client.post(reverse("einkauf:bestellung_aktiv", args=[fremd.pk])).json()["ok"])
        self.client.post(reverse("einkauf:bestellung_verlassen", args=[fremd.pk]))
        self.client.post(reverse("einkauf:bestellung_verwerfen", args=[fremd.pk]), headers={"X-Wwok-Stand": "1"})
        fremd.refresh_from_db()
        self.assertTrue(fremd.vorlaeufig)
        self.assertGreater(fremd.zuletzt_aktiv, timezone.now() - timedelta(minutes=1))
        self.assertEqual(self.client.post(reverse("einkauf:wareneingang_neu"), {"bestellung": fremd.pk}).status_code, 404)

    def test_nummer_bleibt_je_mandant_eindeutig_mehrere_ohne_nummer_erlaubt(self):
        a, b = self.neu(), self.neu()  # NULL ist im Unique-Index (Mandant, Nummer) mehrfach erlaubt
        self.assertEqual(Bestellung.objects.filter(nummer__isnull=True).count(), 2)
        self.assertNotEqual(a.pk, b.pk)
