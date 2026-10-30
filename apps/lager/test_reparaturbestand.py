"""Gutschrift: Spalte B entscheidet, ob die Ware in den Reparatur- oder den Lagerbestand geht."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.kern.models import Mandant
from apps.stammdaten.models import Artikel, Kunde, Steuersatz
from apps.verkauf.models import Beleg, Belegart, BelegArbeitsstand, Position, Ruecknahme

from .models import Bewegungsart, Lagerbewegung, Reparaturart, Reparaturbewegung, Seriennummer, bestand, reparaturbestand
from .services import (BestandReichtNicht, buche_manuell, fehlbestand, reparatur_ausbuchen, reparatur_umbuchen,
                       retoure_aus_gutschrift, retoure_offen)


@override_settings(SECURE_SSL_REDIRECT=False)
class ReparaturbestandTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.m2 = Mandant.objects.create(kurzname="ZONE", firmenname="ZONE Handel")
        self.kunde = Kunde.objects.create(nummer="1", name1="Kunde GmbH")
        self.st = Steuersatz.objects.create(bezeichnung="19", prozent=Decimal("19"))
        self.a = Artikel.objects.create(nummer="A1", bezeichnung="Notebook", steuersatz=self.st)
        self.b = Artikel.objects.create(nummer="A2", bezeichnung="Maus", steuersatz=self.st)
        self.l = Artikel.objects.create(nummer="L1", bezeichnung="Arbeitszeit", typ="L", steuersatz=self.st)
        buche_manuell(self.m, self.a, Bewegungsart.WARENEINGANG, Decimal("5"))
        buche_manuell(self.m, self.b, Bewegungsart.WARENEINGANG, Decimal("5"))
        self.user = get_user_model().objects.create_user("t", password="x" * 14)
        self.client.force_login(self.user)
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    def rechnung(self, *artikel):
        re = Beleg.objects.create(mandant=self.m, art=Belegart.RECHNUNG, kunde=self.kunde)
        for a in artikel:
            Position.objects.create(beleg=re, artikel=a, typ=a.typ, bezeichnung=a.bezeichnung, menge=Decimal("2"), einzelpreis=Decimal("10"))
        re.abschliessen()
        re.refresh_from_db()
        return re

    def gutschrift(self, re, wert):
        self.client.post(reverse("verkauf:beleg_gutschrift", args=[re.pk]), {"ruecknahme": wert})
        return re.folgebelege.get(art=Belegart.GUTSCHRIFT)

    def test_reparatur_zaehlt_nicht_als_lagerbestand(self):
        re = self.rechnung(self.a)
        gs = self.gutschrift(re, "R")
        self.assertEqual(gs.positionen.get().ruecknahme, Ruecknahme.REPARATUR)
        self.assertEqual(reparaturbestand(self.m, self.a), Decimal("0"))  # gebucht wird erst beim Abschließen
        gs.abschliessen()
        self.assertEqual(reparaturbestand(self.m, self.a), Decimal("2"))
        self.assertEqual(bestand(self.m, self.a), Decimal("3"))
        ls = Beleg.objects.create(mandant=self.m, art=Belegart.LIEFERSCHEIN, kunde=self.kunde)
        Position.objects.create(beleg=ls, artikel=self.a, bezeichnung="Notebook", menge=Decimal("4"), einzelpreis=Decimal("10"))
        self.assertTrue(fehlbestand(ls))  # der Reparaturbestand ist nicht verkaufbar
        self.assertEqual(reparaturbestand(self.m2, self.a), Decimal("0"))  # Mandanten bleiben getrennt

    def test_lagerbestand_und_keine_ware(self):
        re = self.rechnung(self.a, self.b)
        gs = self.gutschrift(re, "B")
        erste, zweite = gs.positionen.all()
        Position.objects.filter(pk=zweite.pk).update(ruecknahme=Ruecknahme.KEINE)
        gs.abschliessen()
        self.assertEqual(bestand(self.m, self.a), Decimal("5"))
        self.assertEqual(bestand(self.m, self.b), Decimal("3"))
        self.assertFalse(Reparaturbewegung.objects.exists())
        self.assertEqual(Lagerbewegung.objects.get(beleg=gs).art, Bewegungsart.RETOURE)
        self.assertFalse(retoure_offen(gs))
        with self.assertRaises(ValueError):
            retoure_aus_gutschrift(gs)  # kein zweites Mal von Hand
        self.assertNotContains(self.client.get(reverse("verkauf:beleg", args=[gs.pk])), "Ware ins Lager zurückbuchen")

    def test_abschliessen_bucht_nur_einmal_und_summiert_gleiche_artikel(self):
        re = self.rechnung(self.a, self.a)
        gs = self.gutschrift(re, "R")
        gs.abschliessen()
        Beleg.objects.get(pk=gs.pk).abschliessen()
        self.assertEqual(Reparaturbewegung.objects.get().menge, Decimal("4"))

    def test_abfrage_ist_pflicht_bei_lagerartikeln(self):
        re = self.rechnung(self.a)
        for daten in ({}, {"ruecknahme": ""}, {"ruecknahme": "X"}):
            self.client.post(reverse("verkauf:beleg_gutschrift", args=[re.pk]), daten)
        self.assertFalse(re.folgebelege.exists())

    def test_abfrage_nur_bei_lagerartikeln(self):
        html = self.client.get(reverse("verkauf:beleg", args=[self.rechnung(self.a).pk])).content.decode()
        self.assertIn('id="gutschriftform"', html)
        nur_leistung = self.rechnung(self.l)
        self.assertNotContains(self.client.get(reverse("verkauf:beleg", args=[nur_leistung.pk])), 'id="gutschriftform"')
        gs = self.gutschrift(nur_leistung, "")
        self.assertEqual(gs.positionen.get().ruecknahme, Ruecknahme.KEINE)

    def test_klick_wechselt_r_b_keine_und_laesst_sich_verwerfen(self):
        gs = self.gutschrift(self.rechnung(self.a), "R")
        p = gs.positionen.get()
        url = reverse("verkauf:position_ruecknahme", args=[gs.pk, p.pk])
        for erwartet in ("B", "K", "R", "B"):
            self.client.post(url)
            p.refresh_from_db()
            self.assertEqual(p.ruecknahme, erwartet)
        self.assertTrue(BelegArbeitsstand.objects.filter(beleg=gs).exists())
        gs.arbeitsstand_verwerfen()
        p.refresh_from_db()
        self.assertEqual(p.ruecknahme, "R")

    def test_klick_nur_bei_offener_gutschrift_und_lagerartikel(self):
        re = self.rechnung(self.a, self.l)
        gs = self.gutschrift(re, "R")
        leistung = gs.positionen.get(artikel=self.l)
        self.assertEqual(self.client.post(reverse("verkauf:position_ruecknahme", args=[gs.pk, leistung.pk])).status_code, 404)
        self.assertFalse(BelegArbeitsstand.objects.filter(beleg=gs).exists())
        p = gs.positionen.get(artikel=self.a)
        gs.abschliessen()
        self.client.post(reverse("verkauf:position_ruecknahme", args=[gs.pk, p.pk]))
        p.refresh_from_db()
        self.assertEqual(p.ruecknahme, "R")
        auftrag = Beleg.objects.create(mandant=self.m, art=Belegart.AUFTRAG, kunde=self.kunde)
        fremd = Position.objects.create(beleg=auftrag, artikel=self.a, bezeichnung="x", einzelpreis=Decimal("1"))
        self.client.post(reverse("verkauf:position_ruecknahme", args=[auftrag.pk, fremd.pk]))
        fremd.refresh_from_db()
        self.assertEqual(fremd.ruecknahme, "")

    def test_neue_position_folgt_der_gutschrift(self):
        gs = self.gutschrift(self.rechnung(self.a), "B")
        self.assertEqual(Position.objects.create(beleg=gs, artikel=self.b, bezeichnung="x", einzelpreis=Decimal("1")).ruecknahme, "B")
        frei = Beleg.objects.create(mandant=self.m, art=Belegart.GUTSCHRIFT, kunde=self.kunde)
        self.assertEqual(Position.objects.create(beleg=frei, artikel=self.b, bezeichnung="x", einzelpreis=Decimal("1")).ruecknahme, "R")

    def test_aeltere_gutschrift_bleibt_von_hand_buchbar(self):
        re = self.rechnung(self.a)
        gs = re.gutschrift_erzeugen()
        gs.abschliessen()
        self.assertEqual(Lagerbewegung.objects.filter(beleg=gs).count(), 0)
        self.assertTrue(retoure_offen(gs))
        self.assertContains(self.client.get(reverse("verkauf:beleg", args=[gs.pk])), "Ware ins Lager zurückbuchen")
        self.assertEqual(retoure_aus_gutschrift(gs), 1)
        self.assertEqual(bestand(self.m, self.a), Decimal("5"))
        self.assertFalse(retoure_offen(gs))

    def test_umbuchen_und_ausbuchen(self):
        gs = self.gutschrift(self.rechnung(self.a), "R")
        gs.abschliessen()
        reparatur_umbuchen(self.m, self.a, Decimal("1"), "repariert")
        self.assertEqual((reparaturbestand(self.m, self.a), bestand(self.m, self.a)), (Decimal("1"), Decimal("4")))
        self.assertEqual(Lagerbewegung.objects.filter(art=Bewegungsart.REPARATUR).count(), 1)
        with self.assertRaises(BestandReichtNicht):
            reparatur_umbuchen(self.m, self.a, Decimal("2"))
        self.assertEqual(bestand(self.m, self.a), Decimal("4"))  # kein halber Vorgang
        with self.assertRaises(ValueError):
            reparatur_ausbuchen(self.m, self.a, Decimal("1"), "")  # Bemerkung ist Pflicht
        reparatur_ausbuchen(self.m, self.a, Decimal("1"), "Schrott")
        self.assertEqual((reparaturbestand(self.m, self.a), bestand(self.m, self.a)), (Decimal("0"), Decimal("4")))
        self.assertEqual(Reparaturbewegung.objects.first().art, Reparaturart.AUSBUCHUNG)
        with self.assertRaises(BestandReichtNicht):
            reparatur_ausbuchen(self.m2, self.a, Decimal("1"), "Schrott")

    def test_mehr_als_verkauft_laesst_sich_nicht_abschliessen(self):
        gs = self.gutschrift(self.rechnung(self.a), "B")
        Position.objects.filter(beleg=gs).update(menge=Decimal("8"), einzelpreis=Decimal("1"))
        with self.assertRaisesMessage(ValueError, "Zurückgebucht würden 8, auf der Rechnung"):
            Beleg.objects.get(pk=gs.pk).abschliessen()
        self.assertFalse(Beleg.objects.get(pk=gs.pk).abgeschlossen)
        self.assertFalse(Lagerbewegung.objects.filter(beleg=gs).exists())
        Position.objects.filter(beleg=gs).update(ruecknahme=Ruecknahme.KEINE)  # ein reiner Preisnachlass bleibt möglich
        Beleg.objects.get(pk=gs.pk).abschliessen()
        self.assertTrue(Beleg.objects.get(pk=gs.pk).abgeschlossen)

    def test_zweite_gutschrift_zaehlt_die_erste_mit(self):
        re = self.rechnung(self.a, self.b)
        erste = self.gutschrift(re, "R")
        Position.objects.filter(beleg=erste, artikel=self.b).update(ruecknahme=Ruecknahme.KEINE, einzelpreis=Decimal("0"))
        erste.abschliessen()
        zweite = re.gutschrift_erzeugen(Ruecknahme.REPARATUR)
        Position.objects.filter(beleg=zweite, artikel=self.b).update(einzelpreis=Decimal("0"))
        with self.assertRaises(ValueError):
            Beleg.objects.get(pk=zweite.pk).abschliessen()  # A wäre doppelt zurück
        self.assertEqual(reparaturbestand(self.m, self.a), Decimal("2"))
        Position.objects.filter(beleg=zweite, artikel=self.a).update(ruecknahme=Ruecknahme.KEINE)
        Beleg.objects.get(pk=zweite.pk).abschliessen()
        self.assertEqual(reparaturbestand(self.m, self.b), Decimal("2"))

    def test_artikel_nicht_auf_der_rechnung_wird_nicht_gebucht(self):
        gs = self.gutschrift(self.rechnung(self.a), "B")
        fremd = Position.objects.create(beleg=gs, artikel=self.b, bezeichnung="x", einzelpreis=Decimal("0"))
        with self.assertRaises(ValueError):
            Beleg.objects.get(pk=gs.pk).abschliessen()
        Position.objects.filter(pk=fremd.pk).update(ruecknahme=Ruecknahme.KEINE)
        Beleg.objects.get(pk=gs.pk).abschliessen()

    def test_freie_gutschrift_hat_keine_mengengrenze(self):
        frei = Beleg.objects.create(mandant=self.m, art=Belegart.GUTSCHRIFT, kunde=self.kunde)
        Position.objects.create(beleg=frei, artikel=self.a, bezeichnung="x", menge=Decimal("9"), einzelpreis=Decimal("1"))
        frei.abschliessen()
        self.assertEqual(reparaturbestand(self.m, self.a), Decimal("9"))

    def gruppenrechnung(self, fest):
        re = Beleg.objects.create(mandant=self.m, art=Belegart.RECHNUNG, kunde=self.kunde)
        kopf = Position.objects.create(beleg=re, typ="G", bezeichnung="Paket", menge=Decimal("2"), einzelpreis=Decimal("30"), preis_fest=fest)
        Position.objects.create(beleg=re, gruppe=kopf, artikel=self.a, bezeichnung="Notebook", menge=Decimal("2"), einzelpreis=Decimal("30"), pos=kopf.pos)
        re.abschliessen()
        re.refresh_from_db()
        return re

    def test_gruppenmenge_aendert_die_unterpositionen_mit(self):
        for fest in (True, False):
            with self.subTest(fest=fest):
                gs = self.gutschrift(self.gruppenrechnung(fest), "R")
                kopf = gs.positionen.get(typ="G", gruppe__isnull=True)
                vorher = reparaturbestand(self.m, self.a)
                self.client.post(reverse("verkauf:position_aendern", args=[gs.pk, kopf.pk]), {"menge": "1", "preis": "30.00", "rabatt": "0"})
                self.assertEqual(gs.positionen.get(gruppe=kopf).menge, Decimal("1"))
                gs.refresh_from_db()
                self.assertEqual(gs.summe_netto, Decimal("30.00"))
                Beleg.objects.get(pk=gs.pk).abschliessen()
                self.assertEqual(reparaturbestand(self.m, self.a) - vorher, Decimal("1"))

    def test_gruppenmenge_die_nicht_aufgeht_wird_abgelehnt(self):
        gs = self.gutschrift(self.gruppenrechnung(True), "R")
        kopf = gs.positionen.get(typ="G", gruppe__isnull=True)
        Position.objects.filter(pk=kopf.pk).update(menge=Decimal("3"))
        self.client.post(reverse("verkauf:position_aendern", args=[gs.pk, kopf.pk]), {"menge": "1", "preis": "30.00", "rabatt": "0"})
        kopf.refresh_from_db()
        self.assertEqual(kopf.menge, Decimal("3"))
        self.assertEqual(gs.positionen.get(gruppe=kopf).menge, Decimal("2"))
        self.assertFalse(BelegArbeitsstand.objects.filter(beleg=gs).exists())

    def test_aelterer_entwurf_wird_beim_ersten_klick_nicht_gemischt(self):
        gs = self.rechnung(self.a, self.b).gutschrift_erzeugen()
        erste = gs.positionen.first()
        self.client.post(reverse("verkauf:position_ruecknahme", args=[gs.pk, erste.pk]))
        self.assertEqual(set(gs.positionen.values_list("ruecknahme", flat=True)), {"R"})

    def test_seriennummer_wieder_einlagern_ohne_bestandsbuchung(self):
        from .services import seriennummern_einlagern, seriennummern_zuordnen
        seriennummern_einlagern(self.m, self.a, "SN1")
        entwurf = Beleg.objects.create(mandant=self.m, art=Belegart.RECHNUNG, kunde=self.kunde)
        p = Position.objects.create(beleg=entwurf, artikel=self.a, bezeichnung="Notebook", einzelpreis=Decimal("10"))
        seriennummern_zuordnen(p, "SN1")
        sn = Seriennummer.objects.get(nummer="SN1")
        url = reverse("lager:seriennummer_einlagern", args=[sn.pk])
        self.client.post(url)
        sn.refresh_from_db()
        self.assertEqual(sn.status, Seriennummer.VERKAUFT)  # der Beleg ist noch offen
        self.assertNotContains(self.client.get(reverse("lager:artikel", args=[self.a.pk])), "Wieder einlagern")
        entwurf.abschliessen()
        self.assertContains(self.client.get(reverse("lager:artikel", args=[self.a.pk])), "Wieder einlagern")
        vorher = bestand(self.m, self.a)
        self.client.post(url)
        sn.refresh_from_db()
        self.assertEqual((sn.status, sn.position), (Seriennummer.IM_LAGER, None))
        self.assertEqual(bestand(self.m, self.a), vorher)
        self.client.post(url)  # kein zweites Mal
        self.assertEqual(Seriennummer.objects.get(pk=sn.pk).status, Seriennummer.IM_LAGER)

    def test_seriennummer_eines_anderen_mandanten_ist_tabu(self):
        fremd = Seriennummer.objects.create(mandant=self.m2, artikel=self.a, nummer="X", status=Seriennummer.VERKAUFT)
        self.assertEqual(self.client.post(reverse("lager:seriennummer_einlagern", args=[fremd.pk])).status_code, 404)

    def test_bewegungen_sind_unveraenderlich(self):
        gs = self.gutschrift(self.rechnung(self.a), "R")
        gs.abschliessen()
        z = Reparaturbewegung.objects.get()
        with self.assertRaises(PermissionError):
            z.save()
        with self.assertRaises(PermissionError):
            z.delete()

    def test_masken(self):
        gs = self.gutschrift(self.rechnung(self.a), "R")
        self.assertContains(self.client.get(reverse("verkauf:beleg", args=[gs.pk])), "position/%d/bestand/" % gs.positionen.get().pk)
        url = reverse("lager:artikel", args=[self.a.pk])
        self.assertNotContains(self.client.get(url), "In Lagerbestand umbuchen")
        gs.abschliessen()
        self.assertContains(self.client.get(url), "In Lagerbestand umbuchen")
        buchen = reverse("lager:reparatur", args=[self.a.pk])
        self.client.post(buchen, {"aktion": "lager", "menge": "1,5", "bemerkung": "repariert"})
        self.assertEqual(reparaturbestand(self.m, self.a), Decimal("0.5"))
        self.client.post(buchen, {"aktion": "ausbuchen", "menge": "0,5", "bemerkung": ""})
        self.assertEqual(reparaturbestand(self.m, self.a), Decimal("0.5"))
        self.client.post(buchen, {"aktion": "anders", "menge": "0,5", "bemerkung": "x"})
        self.client.post(buchen, {"aktion": "lager", "menge": "NaN"})
        self.client.post(buchen, {"aktion": "lager", "menge": "-1"})
        self.assertEqual(reparaturbestand(self.m, self.a), Decimal("0.5"))
        self.assertContains(self.client.get(url), "Historie Reparaturbestand")
        kein_lager = Artikel.objects.create(nummer="A3", bezeichnung="Nur in Reparatur", steuersatz=self.st)
        Reparaturbewegung.objects.create(mandant=self.m, artikel=kein_lager, art=Reparaturart.ZUGANG, menge=Decimal("1"))
        self.assertContains(self.client.get(reverse("lager:uebersicht") + "?bestand=1"), "A3")
