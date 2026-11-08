"""Übernehmen wandelt um: Angebot -> Bestellung und Bestellung -> Rechnung ändern den Beleg selbst (neue Art, neue Nummer),
die alten Nummern stehen im Verlauf (BelegUmwandlung). Teilübernahmen und Lieferschein-Schritte bleiben neue Belege."""
import io
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from pypdf import PdfReader

from apps.finanzen.models import Anzahlungsverrechnung, Zahlung
from apps.kern.models import AenderungsProtokoll, Mandant
from apps.lager.models import Bewegungsart, Lagerbewegung, bestand, reserviert
from apps.lager.services import buche_manuell
from apps.stammdaten.models import Artikel, Kunde, Lieferant, Preislistenartikel, Steuersatz

from .models import Beleg, Belegart, BelegUmwandlung, Nummernkreis, Position
from .pdf import beleg_pdf


@override_settings(SECURE_SSL_REDIRECT=False)
class UmwandelnTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.kunde = Kunde.objects.create(nummer="1", name1="Kunde GmbH")
        st = Steuersatz.objects.get(nummer=1)
        self.maus = Artikel.objects.create(nummer="M1", bezeichnung="Maus", steuersatz=st, vk1=Decimal("20"))
        self.tasta = Artikel.objects.create(nummer="T1", bezeichnung="Tastatur", steuersatz=st, vk1=Decimal("30"))
        buche_manuell(self.m, self.maus, Bewegungsart.WARENEINGANG, Decimal("10"))
        buche_manuell(self.m, self.tasta, Bewegungsart.WARENEINGANG, Decimal("10"))
        api = Lieferant.objects.create(nummer="70272", name1="API GmbH", kurzname="API")
        self.pl = Preislistenartikel.objects.create(
            lieferant=api, bestellnummer="500", herstellernummer="PSAZ-650", bezeichnung="Netzteil", ek=Decimal("40.0000"), bestand=5)
        self.benutzer = get_user_model().objects.create_user("t", password="x" * 14)
        self.client.force_login(self.benutzer)
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    def beleg(self, art, menge=2, mit_p=False, zweite=False):
        b = Beleg.objects.create(mandant=self.m, art=art, kunde=self.kunde, gueltig_bis=timezone.localdate() + timedelta(days=14),
                                 liefertermin=timezone.localdate() + timedelta(days=3), belegdatum=timezone.localdate() - timedelta(days=5))
        Position.objects.create(beleg=b, artikel=self.maus, bezeichnung="Maus", menge=menge, einzelpreis=Decimal("20"))
        if zweite:
            Position.objects.create(beleg=b, artikel=self.tasta, bezeichnung="Tastatur", menge=3, einzelpreis=Decimal("30"))
        if mit_p:
            Position.objects.create(beleg=b, typ="P", preisliste_artikel=self.pl, bezeichnung="Netzteil", menge=1, einzelpreis=Decimal("50"))
        b.berechne()
        return b

    def meldungen(self, antwort):
        return [str(m) for m in antwort.context["messages"]]

    # --- Angebot -> Bestellung ---------------------------------------------------------------------------------

    def test_angebot_wird_bestellung_umgewandelt(self):
        an = self.beleg(Belegart.ANGEBOT)
        alt, summe = an.nummer, an.summe_brutto
        anzahl = Beleg.objects.count()
        ab = an.folgebeleg_erzeugen()
        an.refresh_from_db()
        self.assertEqual(ab.pk, an.pk)
        self.assertEqual(Beleg.objects.count(), anzahl)
        self.assertEqual(an.art, Belegart.AUFTRAG)
        self.assertTrue(an.nummer.startswith("AB") and an.nummer != alt)
        self.assertEqual((an.belegdatum, an.abgeschlossen, an.gueltig_bis, an.liefertermin), (timezone.localdate(), False, None, None))
        self.assertEqual((an.positionen.count(), an.summe_brutto), (1, summe))
        u = an.umwandlungen.get()
        self.assertEqual((u.von_art, u.von_nummer, u.nach_art), ("AN", alt, "AB"))
        self.assertEqual(u.von_belegdatum, timezone.localdate() - timedelta(days=5))
        self.assertEqual(an.vormals, f"Angebot {alt}")
        self.assertEqual(an.bezug_text, f"Angebot {alt}")

    def test_benutzer_im_verlauf(self):
        an = self.beleg(Belegart.ANGEBOT)
        self.client.post(reverse("verkauf:beleg_folgebeleg", args=[an.pk]), {"von_art": "AN"})
        self.assertEqual(an.umwandlungen.get().benutzer, self.benutzer)

    def test_angebot_mit_frueherer_kopie_bleibt_kopie(self):
        an = self.beleg(Belegart.ANGEBOT)
        an.kopieren_als(Belegart.AUFTRAG)  # frühere Kopie: deren Positionen zählen sonst als übernommen
        ab = an.folgebeleg_erzeugen()
        an.refresh_from_db()
        self.assertNotEqual(ab.pk, an.pk)
        self.assertEqual((an.art, ab.art, ab.vorgaenger_id), (Belegart.ANGEBOT, Belegart.AUFTRAG, an.pk))
        self.assertFalse(an.umwandlungen.exists())

    def test_angebot_mit_p_artikel_bleibt_teiluebernahme(self):
        an = self.beleg(Belegart.ANGEBOT, mit_p=True)
        ab = an.folgebeleg_erzeugen()
        an.refresh_from_db()
        self.assertNotEqual(ab.pk, an.pk)
        self.assertEqual(an.art, Belegart.ANGEBOT)
        self.assertEqual(an.positionen.count(), 2)  # P bleibt im Angebot
        self.assertEqual(ab.positionen.count(), 1)
        self.assertFalse(BelegUmwandlung.objects.exists())

    def test_doppelklick_wandelt_nicht_zweimal_um(self):
        an = self.beleg(Belegart.ANGEBOT)
        zweiter_tab = Beleg.objects.get(pk=an.pk)
        an.folgebeleg_erzeugen()
        with self.assertRaisesMessage(ValueError, "inzwischen umgewandelt"):
            zweiter_tab.folgebeleg_erzeugen()
        self.assertEqual(BelegUmwandlung.objects.count(), 1)

    def test_angebot_ohne_position_wird_nicht_umgewandelt(self):
        an = Beleg.objects.create(mandant=self.m, art=Belegart.ANGEBOT, kunde=self.kunde)
        with self.assertRaisesMessage(ValueError, "keine Position"):
            an.folgebeleg_erzeugen()
        an.refresh_from_db()
        self.assertEqual(an.art, Belegart.ANGEBOT)

    def test_angebot_nicht_direkt_als_rechnung(self):
        an = self.beleg(Belegart.ANGEBOT)
        with self.assertRaisesMessage(ValueError, "kann kein Rechnung übernommen werden"):
            an.folgebeleg_erzeugen(art=Belegart.RECHNUNG)

    def test_kopieren_als_bleibt_echte_kopie(self):
        an = self.beleg(Belegart.ANGEBOT)
        kopie = an.kopieren_als(Belegart.AUFTRAG)
        an.refresh_from_db()
        self.assertNotEqual(kopie.pk, an.pk)
        self.assertEqual(an.art, Belegart.ANGEBOT)
        ab = self.beleg(Belegart.AUFTRAG)
        re = ab.kopieren_als(Belegart.RECHNUNG)
        ab.refresh_from_db()
        self.assertNotEqual(re.pk, ab.pk)
        self.assertEqual(ab.art, Belegart.AUFTRAG)
        self.assertFalse(BelegUmwandlung.objects.exists())

    # --- Bestellung -> Rechnung --------------------------------------------------------------------------------

    def test_bestellung_wird_rechnung_umgewandelt_und_abgeschlossen(self):
        an = self.beleg(Belegart.ANGEBOT)
        an_nummer = an.nummer
        ab = an.folgebeleg_erzeugen()
        ab_nummer = ab.nummer
        self.assertEqual(reserviert(self.m, self.maus), Decimal("2"))
        re = ab.folgebeleg_erzeugen(art=Belegart.RECHNUNG)
        self.assertEqual(re.pk, an.pk)
        re.refresh_from_db()
        self.assertEqual((re.art, re.nummer, re.abgeschlossen), (Belegart.RECHNUNG, None, False))
        self.assertEqual([(u.von_art, u.von_nummer) for u in re.umwandlungen.all()], [("AN", an_nummer), ("AB", ab_nummer)])
        self.assertEqual(re.vormals, f"Angebot {an_nummer}, Bestellung {ab_nummer}")
        self.assertEqual(re.bezug_text, f"Bestellung {ab_nummer}")
        self.assertEqual(reserviert(self.m, self.maus), Decimal("0"))  # keine Bestellung mehr, nichts mehr reserviert
        re.abschliessen()
        re.refresh_from_db()
        self.assertTrue(re.nummer.startswith("RE") and re.abgeschlossen)
        self.assertEqual(bestand(self.m, self.maus), Decimal("8"))
        self.assertEqual(Lagerbewegung.objects.filter(beleg=re, art=Bewegungsart.VERKAUF).count(), 1)
        self.assertEqual(Nummernkreis.objects.get(mandant=self.m, art="RE").letzte_nummer, 1)  # erst beim Abschluss vergeben

    def test_rechnung_nummer_erst_beim_abschluss_lueckenlos(self):
        ab = self.beleg(Belegart.AUFTRAG)
        ab.folgebeleg_erzeugen(art=Belegart.RECHNUNG)
        self.assertFalse(Nummernkreis.objects.filter(mandant=self.m, art="RE").exists())

    def test_bestellung_mit_engpass_und_teilmenge_wird_kopiert(self):
        ab = self.beleg(Belegart.AUFTRAG, menge=15)  # nur 10 auf Lager
        re = ab.folgebeleg_erzeugen(entscheidungen={ab.positionen.get().pk: Decimal("4")}, art=Belegart.RECHNUNG)
        ab.refresh_from_db()
        self.assertNotEqual(re.pk, ab.pk)
        self.assertEqual((ab.art, re.art, re.vorgaenger_id), (Belegart.AUFTRAG, Belegart.RECHNUNG, ab.pk))
        self.assertEqual(re.positionen.get().menge, Decimal("4"))
        self.assertEqual(ab.offene_mengen()[ab.positionen.get().pk], Decimal("11"))
        self.assertFalse(BelegUmwandlung.objects.exists())

    def test_bestellung_mit_frueherem_lieferschein_wird_kopiert(self):
        ab = self.beleg(Belegart.AUFTRAG, zweite=True)
        ls = Beleg.objects.create(mandant=self.m, art=Belegart.LIEFERSCHEIN, kunde=self.kunde, vorgaenger=ab)
        maus = ab.positionen.get(artikel=self.maus)
        Position.objects.create(beleg=ls, artikel=self.maus, bezeichnung="Maus", menge=1, einzelpreis=Decimal("20"), quellposition=maus)
        re = ab.folgebeleg_erzeugen(art=Belegart.RECHNUNG)
        ab.refresh_from_db()
        self.assertNotEqual(re.pk, ab.pk)
        self.assertEqual(ab.art, Belegart.AUFTRAG)
        self.assertEqual(sorted(p.menge for p in re.positionen.all()), [Decimal("1"), Decimal("3")])

    def test_bestellung_mit_stornierter_proforma_wird_kopiert(self):
        ab = self.beleg(Belegart.AUFTRAG)
        pf = ab.kopieren_als(Belegart.PROFORMA)
        pf.abschliessen()
        Beleg.objects.create(mandant=self.m, art=Belegart.PROFORMA_STORNO, kunde=self.kunde, vorgaenger=pf)
        re = ab.folgebeleg_erzeugen(art=Belegart.RECHNUNG)
        self.assertNotEqual(re.pk, ab.pk)
        ab.refresh_from_db()
        self.assertEqual(ab.art, Belegart.AUFTRAG)

    def test_aktive_proforma_sperrt_die_uebernahme(self):
        ab = self.beleg(Belegart.AUFTRAG)
        ab.kopieren_als(Belegart.PROFORMA)
        with self.assertRaisesMessage(ValueError, "Vorkasse"):
            ab.folgebeleg_erzeugen(art=Belegart.RECHNUNG)
        ab.refresh_from_db()
        self.assertEqual(ab.art, Belegart.AUFTRAG)

    def test_bestellung_mit_zahlung_wird_kopiert(self):
        ab = self.beleg(Belegart.AUFTRAG)
        Zahlung.objects.create(mandant=self.m, beleg=ab, betrag=Decimal("10"))
        re = ab.folgebeleg_erzeugen(art=Belegart.RECHNUNG)
        ab.refresh_from_db()
        self.assertNotEqual(re.pk, ab.pk)
        self.assertEqual(ab.art, Belegart.AUFTRAG)
        self.assertEqual(ab.zahlungen.count(), 1)

    def test_bestellung_mit_anzahlungsverrechnung_wird_kopiert(self):
        ab = self.beleg(Belegart.AUFTRAG)
        andere = Beleg.objects.create(mandant=self.m, art=Belegart.RECHNUNG, kunde=self.kunde)
        Anzahlungsverrechnung.objects.create(mandant=self.m, quelle=ab, rechnung=andere, betrag=Decimal("5"))
        re = ab.folgebeleg_erzeugen(art=Belegart.RECHNUNG)
        self.assertNotEqual(re.pk, ab.pk)

    def test_bestellung_mit_p_artikel_wird_kopiert_und_p_wandert_ins_angebot(self):
        ab = self.beleg(Belegart.AUFTRAG, mit_p=True)
        re = ab.folgebeleg_erzeugen(art=Belegart.RECHNUNG)
        self.assertNotEqual(re.pk, ab.pk)
        self.assertEqual(Beleg.objects.filter(art=Belegart.ANGEBOT, vorgaenger=ab).count(), 1)
        self.assertFalse(BelegUmwandlung.objects.exists())

    def test_bestellung_zum_lieferschein_bleibt_neuer_beleg(self):
        ab = self.beleg(Belegart.AUFTRAG)
        ls = ab.folgebeleg_erzeugen()
        self.assertNotEqual(ls.pk, ab.pk)
        self.assertEqual(ls.art, Belegart.LIEFERSCHEIN)
        ls.abschliessen()
        re = ls.folgebeleg_erzeugen()
        self.assertNotEqual(re.pk, ls.pk)
        self.assertEqual(re.art, Belegart.RECHNUNG)
        self.assertFalse(BelegUmwandlung.objects.exists())

    def test_rechnung_aus_bestellung_darf_nicht_weiter_uebernommen_werden(self):
        ab = self.beleg(Belegart.AUFTRAG)
        re = ab.folgebeleg_erzeugen(art=Belegart.RECHNUNG)
        with self.assertRaisesMessage(ValueError, "keinen Folgebeleg"):
            re.folgebeleg_erzeugen(art=Belegart.RECHNUNG)

    # --- Nummern ------------------------------------------------------------------------------------------------

    def test_alte_nummern_bleiben_vergeben(self):
        an = self.beleg(Belegart.ANGEBOT)
        alt = an.nummer
        an.folgebeleg_erzeugen()
        self.assertIn(alt, Nummernkreis.vergebene(self.m, "AN"))
        Nummernkreis.freigeben(self.m, "AN", alt)  # wäre die letzte Nummer, bleibt aber vergeben
        self.assertEqual(Nummernkreis.objects.get(mandant=self.m, art="AN").letzte_nummer, 1)
        with self.assertRaises(ValueError):
            Nummernkreis.naechste_setzen(self.m, "AN", 1)
        neu = self.beleg(Belegart.ANGEBOT)
        self.assertNotEqual(neu.nummer, alt)
        self.assertTrue(neu.nummer.endswith("00002"))

    def test_bestellnummer_bleibt_nach_rechnung_vergeben(self):
        ab = self.beleg(Belegart.AUFTRAG)
        alt = ab.nummer
        ab.folgebeleg_erzeugen(art=Belegart.RECHNUNG)
        self.assertIn(alt, Nummernkreis.vergebene(self.m, "AB"))
        with self.assertRaises(ValueError):
            Nummernkreis.naechste_setzen(self.m, "AB", 1)

    def test_bestellnummer_kommt_aus_dem_kreis(self):
        vorhanden = self.beleg(Belegart.AUFTRAG)
        an = self.beleg(Belegart.ANGEBOT)
        ab = an.folgebeleg_erzeugen()
        self.assertEqual(int(ab.nummer[-5:]), int(vorhanden.nummer[-5:]) + 1)

    # --- Suche, Anzeige, Protokoll -------------------------------------------------------------------------------

    def test_alte_nummer_fuehrt_zum_umgewandelten_beleg(self):
        an = self.beleg(Belegart.ANGEBOT)
        alt = an.nummer
        an.folgebeleg_erzeugen()
        r = self.client.get(reverse("verkauf:suche"), {"q": alt})
        self.assertRedirects(r, reverse("verkauf:beleg", args=[an.pk]), fetch_redirect_response=False)
        r = self.client.get(reverse("verkauf:belege"), {"q": alt, "status": ""})
        self.assertContains(r, f"vormals Angebot {alt}")
        self.assertEqual([b.pk for b in r.context["seite"].object_list], [an.pk])

    def test_belegkopf_zeigt_vormals_und_verlauf(self):
        an = self.beleg(Belegart.ANGEBOT)
        alt = an.nummer
        an.folgebeleg_erzeugen()
        r = self.client.get(reverse("verkauf:beleg", args=[an.pk]))
        self.assertContains(r, f"vormals Angebot {alt}")
        self.assertContains(r, "Verlauf (1 Umwandlung)")
        self.assertContains(r, "Als Rechnung übernehmen")
        r = self.client.get(reverse("verkauf:kunde", args=[self.kunde.pk]))
        self.assertContains(r, f"vormals Angebot {alt}")

    def test_pdf_bezug_nennt_letzte_fruehere_nummer(self):
        ab = self.beleg(Belegart.AUFTRAG)
        alt = ab.nummer
        re = ab.folgebeleg_erzeugen(art=Belegart.RECHNUNG)
        text = " ".join(PdfReader(io.BytesIO(beleg_pdf(re))).pages[0].extract_text().split())
        self.assertIn(f"Bezug Bestellung {alt}", text)

    def test_aenderungsprotokoll_haelt_artwechsel_fest(self):
        an = self.beleg(Belegart.ANGEBOT)
        an.folgebeleg_erzeugen()
        eintrag = AenderungsProtokoll.objects.filter(tabelle="verkauf.Beleg", datensatz_id=str(an.pk), aktion="aendern").latest("pk")
        self.assertEqual((eintrag.vorher["art"], eintrag.nachher["art"]), ("AN", "AB"))
        self.assertNotEqual(eintrag.vorher["nummer"], eintrag.nachher["nummer"])

    # --- Löschen ------------------------------------------------------------------------------------------------

    def test_umgewandelte_rechnung_nicht_loeschbar(self):
        ab = self.beleg(Belegart.AUFTRAG)
        alt = ab.nummer
        re = ab.folgebeleg_erzeugen(art=Belegart.RECHNUNG)
        self.assertIn(f"Bestellung {alt}", re.loeschhindernis())
        r = self.client.post(reverse("verkauf:beleg_loeschen", args=[re.pk]), follow=True)
        self.assertTrue(Beleg.objects.filter(pk=re.pk).exists())
        self.assertIn("umgewandelt", " ".join(self.meldungen(r)))

    def test_leerer_umgewandelter_entwurf_wird_nicht_entfernt(self):
        ab = self.beleg(Belegart.AUFTRAG)
        re = ab.folgebeleg_erzeugen(art=Belegart.RECHNUNG)
        re.positionen.all().delete()
        Beleg.objects.filter(pk=re.pk).update(angelegt_am=timezone.now() - timedelta(hours=1))
        self.assertEqual(Beleg.leere_entwuerfe_entfernen(self.m), 0)
        self.assertTrue(Beleg.objects.filter(pk=re.pk).exists())

    def test_umgewandelte_bestellung_bleibt_loeschbar(self):
        an = self.beleg(Belegart.ANGEBOT)
        ab = an.folgebeleg_erzeugen()
        self.assertIsNone(ab.loeschhindernis())
        ab.delete()
        self.assertFalse(BelegUmwandlung.objects.exists())

    # --- Ansichten ----------------------------------------------------------------------------------------------

    def test_knopf_als_rechnung_uebernehmen_meldung(self):
        ab = self.beleg(Belegart.AUFTRAG)
        alt = ab.nummer
        r = self.client.post(reverse("verkauf:beleg_folgebeleg", args=[ab.pk]), {"von_art": "AB", "art": "RE"}, follow=True)
        self.assertRedirects(r, reverse("verkauf:beleg", args=[ab.pk]))
        text = " ".join(self.meldungen(r))
        self.assertIn(f"Bestellung {alt} in Rechnung (Entwurf) umgewandelt", text)
        self.assertNotIn("Rückstand", text)
        self.assertContains(r, f"vormals Bestellung {alt}")

    def test_meldung_angebot_in_bestellung_umgewandelt(self):
        an = self.beleg(Belegart.ANGEBOT)
        alt = an.nummer
        r = self.client.post(reverse("verkauf:beleg_folgebeleg", args=[an.pk]), {"von_art": "AN"}, follow=True)
        an.refresh_from_db()
        self.assertIn(f"Angebot {alt} in Bestellung {an.nummer} umgewandelt", " ".join(self.meldungen(r)))
        self.assertNotIn("Rückstand", " ".join(self.meldungen(r)))

    def test_meldung_bei_zahlung_neue_rechnung(self):
        ab = self.beleg(Belegart.AUFTRAG)
        Zahlung.objects.create(mandant=self.m, beleg=ab, betrag=Decimal("10"))
        r = self.client.post(reverse("verkauf:beleg_folgebeleg", args=[ab.pk]), {"von_art": "AB", "art": "RE"}, follow=True)
        self.assertIn("gibt es Zahlungen", " ".join(self.meldungen(r)))
        ab.refresh_from_db()
        self.assertEqual(ab.art, Belegart.AUFTRAG)

    def test_engpass_rueckfrage_bei_rechnung_behaelt_art(self):
        ab = self.beleg(Belegart.AUFTRAG, menge=15)
        r = self.client.post(reverse("verkauf:beleg_folgebeleg", args=[ab.pk]), {"von_art": "AB", "art": "RE"})
        self.assertContains(r, 'name="art" value="RE"')
        self.assertContains(r, "Bestand reicht nicht")
        ab.refresh_from_db()
        self.assertEqual(ab.art, Belegart.AUFTRAG)
        pos = ab.positionen.get().pk
        r = self.client.post(reverse("verkauf:beleg_folgebeleg", args=[ab.pk]), {"von_art": "AB", "art": "RE", f"wahl_{pos}": "B", f"menge_{pos}": "10"}, follow=True)
        neu = Beleg.objects.get(art=Belegart.RECHNUNG)
        self.assertNotEqual(neu.pk, ab.pk)  # Teilmenge: neue Rechnung, Rückstand bleibt in der Bestellung
        self.assertIn("Rückstand", " ".join(self.meldungen(r)))

    def test_ungueltige_art_im_post_wird_abgelehnt(self):
        ab = self.beleg(Belegart.AUFTRAG)
        r = self.client.post(reverse("verkauf:beleg_folgebeleg", args=[ab.pk]), {"von_art": "AB", "art": "GS"}, follow=True)
        ab.refresh_from_db()
        self.assertEqual(ab.art, Belegart.AUFTRAG)
        self.assertTrue(self.meldungen(r))

    # --- Review-Funde -------------------------------------------------------------------------------------------

    def test_zweiter_post_aus_altem_tab_legt_keinen_lieferschein_an(self):
        an = self.beleg(Belegart.ANGEBOT)
        url = reverse("verkauf:beleg_folgebeleg", args=[an.pk])
        self.client.post(url, {"von_art": "AN"})
        r = self.client.post(url, {"von_art": "AN"}, follow=True)  # veralteter Tab: Beleg ist inzwischen AB
        self.assertFalse(Beleg.objects.filter(art=Belegart.LIEFERSCHEIN).exists())
        self.assertIn("inzwischen umgewandelt", " ".join(self.meldungen(r)))
        self.assertEqual(Beleg.objects.get(pk=an.pk).art, Belegart.AUFTRAG)

    def test_post_ohne_von_art_wird_abgelehnt(self):
        an = self.beleg(Belegart.ANGEBOT)
        self.client.post(reverse("verkauf:beleg_folgebeleg", args=[an.pk]))
        self.assertEqual(Beleg.objects.get(pk=an.pk).art, Belegart.ANGEBOT)

    def test_bezug_rechnung_aus_bestellung_kopie_nennt_bestellung(self):
        an = self.beleg(Belegart.ANGEBOT, mit_p=True)  # P-Artikel: Teilübernahme, neue Bestellung mit Vorgänger Angebot
        ab = an.folgebeleg_erzeugen()
        self.assertNotEqual(ab.pk, an.pk)
        self.assertEqual(ab.vorgaenger_id, an.pk)
        ab_nummer = ab.nummer
        re = ab.folgebeleg_erzeugen(art=Belegart.RECHNUNG)
        self.assertEqual(re.pk, ab.pk)  # umgewandelt
        self.assertEqual(re.bezug_text, f"Bestellung {ab_nummer}")

    def test_umwandeln_bricht_ab_wenn_inzwischen_zahlung_da_ist(self):
        ab = self.beleg(Belegart.AUFTRAG)
        veraltet = Beleg.objects.get(pk=ab.pk)
        Zahlung.objects.create(mandant=self.m, beleg=ab, betrag=Decimal("10"))
        with self.assertRaisesMessage(ValueError, "Zahlungen"):
            veraltet._umwandeln(Belegart.RECHNUNG)
        self.assertEqual(Beleg.objects.get(pk=ab.pk).art, Belegart.AUFTRAG)
        self.assertEqual(BelegUmwandlung.objects.count(), 0)

    def test_umwandeln_bricht_ab_wenn_inzwischen_folgebeleg_da_ist(self):
        ab = self.beleg(Belegart.AUFTRAG)
        veraltet = Beleg.objects.get(pk=ab.pk)
        Beleg.objects.create(mandant=self.m, art=Belegart.LIEFERSCHEIN, kunde=self.kunde, vorgaenger=ab)
        with self.assertRaisesMessage(ValueError, "Folgebelege"):
            veraltet._umwandeln(Belegart.RECHNUNG)
        self.assertEqual(Beleg.objects.get(pk=ab.pk).art, Belegart.AUFTRAG)
