"""Teilübernahme aus einer Bestellung: die übernommene Menge WANDERT aus der Bestellung in den Folgebeleg (10 bestellt,
7 geliefert -> die Bestellung zeigt nur noch 3). Verlauf, Rückbuchen/Mitziehen, Gruppen, Seriennummern, Altdaten-Befehl."""
from decimal import Decimal
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.finanzen.anzahlung import proforma_stornieren
from apps.kern.models import AenderungsProtokoll, Mandant
from apps.lager.models import Bewegungsart, Seriennummer, reserviert
from apps.lager.services import buche_manuell
from apps.stammdaten.models import Artikel, Kunde, Steuersatz

from . import uebernahme
from .models import Beleg, BelegUebernahme, Belegart, Position
from .pdf import beleg_pdf

D = Decimal


@override_settings(SECURE_SSL_REDIRECT=False)
class TeiluebernahmeBasis(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.kunde = Kunde.objects.create(nummer="1", name1="Kunde GmbH")
        st = Steuersatz.objects.create(bezeichnung="19", prozent=D("19"))
        self.maus = Artikel.objects.create(nummer="M1", bezeichnung="Maus", steuersatz=st, vk1=D("10"))
        self.ram = Artikel.objects.create(nummer="R1", bezeichnung="RAM", steuersatz=st, vk1=D("5"))
        buche_manuell(self.m, self.maus, Bewegungsart.WARENEINGANG, D("50"))
        buche_manuell(self.m, self.ram, Bewegungsart.WARENEINGANG, D("100"))
        self.user = get_user_model().objects.create_user("t", password="x" * 14)
        self.client.force_login(self.user)
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()
        self.ab = Beleg.objects.create(mandant=self.m, art=Belegart.AUFTRAG, kunde=self.kunde)
        self.pos = Position.objects.create(beleg=self.ab, artikel=self.maus, bezeichnung="Maus", menge=D("10"), einzelpreis=D("10"))
        self.ab.berechne()

    def uebernehmen(self, mengen, art=Belegart.LIEFERSCHEIN, ab=None):
        ab = ab or self.ab
        neu = Beleg.objects.create(mandant=self.m, art=art, kunde=self.kunde, vorgaenger=ab)
        neu.uebernahmen_neu = uebernahme.menge_verschieben(ab, neu, mengen)
        neu.berechne()
        return neu

    def rest(self, p=None):
        p = p or self.pos
        p.refresh_from_db()
        return p.menge

    def gruppe(self, menge="10", ram_menge="20"):
        """Gruppe „PC“: menge Stück, je Stück 2 RAM (Unterposition trägt die Gesamtmenge) und 1 Maus."""
        kopf = Position.objects.create(beleg=self.ab, typ="G", bezeichnung="PC", menge=D(menge), einzelpreis=D("0"))
        r = Position.objects.create(beleg=self.ab, gruppe=kopf, artikel=self.ram, bezeichnung="RAM", menge=D(ram_menge), einzelpreis=D("5"))
        mm = Position.objects.create(beleg=self.ab, gruppe=kopf, artikel=self.maus, bezeichnung="Maus im PC", menge=D(menge), einzelpreis=D("10"))
        self.ab.berechne()
        return kopf, r, mm


class UebernahmeKernTest(TeiluebernahmeBasis):
    def test_teilmenge_wandert_aus_der_bestellung(self):
        ls = self.uebernehmen({self.pos.pk: D("7")})
        self.assertEqual(self.rest(), D("3"))  # die Bestellung behält nur den Rest
        p = ls.positionen.get()
        self.assertEqual((p.menge, p.verschoben, p.quellposition_id), (D("7"), True, self.pos.pk))
        self.assertEqual(self.pos.offen_menge, D("3"))
        self.ab.refresh_from_db()
        self.assertEqual(self.ab.summe_netto, D("30.00"))
        self.assertEqual(ls.summe_netto, D("70.00"))
        self.assertFalse(self.ab.abgeschlossen)
        e = BelegUebernahme.objects.get()
        self.assertEqual((e.menge, e.richtung, e.quelle, e.ziel, e.zielposition, e.quellposition),
                         (D("7"), BelegUebernahme.UEBERNOMMEN, self.ab, ls, p, self.pos))
        self.assertEqual((e.artikelnummer, e.bezeichnung, e.quelle_nummer, e.ziel_art), ("M1", "Maus", self.ab.nummer, "LS"))

    def test_reservierung_ist_der_rest(self):
        self.assertEqual(reserviert(self.m, self.maus), D("10"))
        self.uebernehmen({self.pos.pk: D("7")})
        self.assertEqual(reserviert(self.m, self.maus), D("3"))  # nur noch die Bestellung reserviert, der Lieferschein nicht
        self.uebernehmen({self.pos.pk: D("3")}, art=Belegart.RECHNUNG)
        self.assertEqual(reserviert(self.m, self.maus), D("0"))

    def test_volle_menge_haengt_die_position_um_und_erledigt_die_bestellung(self):
        ls = self.uebernehmen({self.pos.pk: D("10")})
        self.pos.refresh_from_db()
        self.assertEqual((self.pos.beleg_id, self.pos.verschoben, self.pos.menge), (ls.pk, True, D("10")))  # dieselbe Position
        self.ab.refresh_from_db()
        self.assertTrue(self.ab.abgeschlossen)  # leer gewordene Bestellung = erledigt, ohne abschliessen()
        self.assertEqual(self.ab.positionen.count(), 0)
        self.assertEqual(self.ab.summe_netto, D("0.00"))
        self.assertEqual(reserviert(self.m, self.maus), D("0"))
        protokoll = AenderungsProtokoll.objects.filter(tabelle="verkauf.Position", datensatz_id=str(self.pos.pk), aktion="aendern")
        self.assertTrue(any(z.nachher["beleg"] == ls.pk and z.vorher["beleg"] == self.ab.pk for z in protokoll))  # kein stilles update()

    def test_positionsnummern_des_rests_lueckenlos(self):
        zweite = Position.objects.create(beleg=self.ab, artikel=self.ram, bezeichnung="RAM", menge=D("4"), einzelpreis=D("5"))
        dritte = Position.objects.create(beleg=self.ab, artikel=self.maus, bezeichnung="Maus 2", menge=D("2"), einzelpreis=D("10"))
        self.ab.berechne()
        ls = self.uebernehmen({zweite.pk: D("4"), dritte.pk: D("1")})  # Position 2 ganz, Position 3 teilweise
        self.assertEqual([(p.pos, p.bezeichnung, p.menge) for p in self.ab.positionen.all()], [(1, "Maus", D("10")), (2, "Maus 2", D("1"))])
        self.assertEqual([p.pos for p in ls.positionen.all()], [1, 2])

    def test_zweite_zu_grosse_uebernahme_wird_abgelehnt_nichts_doppelt(self):
        """Doppelklick/zweiter Tab: dieselbe Menge noch einmal verlangt, obwohl nur noch der Rest da ist."""
        self.uebernehmen({self.pos.pk: D("7")})
        with self.assertRaisesMessage(ValueError, "nur noch 3 offen"):
            self.uebernehmen({self.pos.pk: D("7")})
        self.assertEqual(self.rest(), D("3"))
        self.assertEqual(BelegUebernahme.objects.count(), 1)

    def test_doppelklick_mit_veralteten_mengen_rollt_alles_zurueck(self):
        """Beide Anfragen haben die offene Menge 10 gelesen; die zweite findet unter der Sperre nur noch 3 und bricht ab."""
        from unittest import mock
        with mock.patch.object(Beleg, "_uebernahme", return_value={self.pos.pk: D("7")}):
            self.ab.folgebeleg_erzeugen(art=Belegart.LIEFERSCHEIN)
            with self.assertRaisesMessage(ValueError, "nur noch 3 offen"):
                self.ab.folgebeleg_erzeugen(art=Belegart.LIEFERSCHEIN)
        self.assertEqual(self.rest(), D("3"))
        self.assertEqual(Beleg.objects.filter(art=Belegart.LIEFERSCHEIN).count(), 1)  # kein zweiter Lieferschein
        self.assertEqual(BelegUebernahme.objects.count(), 1)

    def test_alles_uebernommen_zweite_uebernahme_scheitert(self):
        self.ab.kopieren_als(Belegart.RECHNUNG)
        self.ab.refresh_from_db()
        self.assertTrue(self.ab.abgeschlossen)
        with self.assertRaisesMessage(ValueError, "nichts mehr offen"):
            self.ab.folgebeleg_erzeugen()
        self.assertEqual(Beleg.objects.filter(vorgaenger=self.ab).count(), 1)

    def test_vollstaendige_uebernahme_bleibt_umwandlung(self):
        re = self.ab.folgebeleg_erzeugen(art=Belegart.RECHNUNG)
        self.assertEqual(re.pk, self.ab.pk)  # derselbe Beleg wechselt die Art
        self.assertFalse(BelegUebernahme.objects.exists())
        self.assertEqual(re.positionen.get().menge, D("10"))

    def test_kopieren_als_rechnung_verschiebt_ebenfalls(self):
        buche_manuell(self.m, self.maus, Bewegungsart.KORREKTUR, D("-47"))
        url = reverse("verkauf:beleg_kopieren", args=[self.ab.pk])
        r = self.client.post(url, {"art": "RE", f"wahl_{self.pos.pk}": "B", f"menge_{self.pos.pk}": "3"}, follow=True)
        self.assertEqual(self.rest(), D("7"))
        re = Beleg.objects.get(art=Belegart.RECHNUNG)
        self.assertEqual(re.positionen.get().menge, D("3"))
        self.assertIn("übernommen: 3 × Maus", " ".join(str(m) for m in r.context["messages"]))

    def test_proforma_nimmt_die_menge_aus_der_bestellung(self):
        pf = self.ab.kopieren_als(Belegart.PROFORMA)
        self.assertEqual(pf.positionen.get().menge, D("10"))
        self.assertEqual(reserviert(self.m, self.maus), D("10"))  # jetzt reserviert die Proforma

    def test_versandkosten_laufen_nur_mit_dem_ersten_folgebeleg(self):
        Beleg.objects.filter(pk=self.ab.pk).update(versandkosten=D("5.00"))
        self.ab.refresh_from_db()
        buche_manuell(self.m, self.maus, Bewegungsart.KORREKTUR, D("-47"))
        ls1 = self.ab.folgebeleg_erzeugen(entscheidungen={self.pos.pk: D("2")})
        self.ab.refresh_from_db()
        self.assertEqual((ls1.versandkosten, self.ab.versandkosten), (D("5.00"), D("0.00")))
        buche_manuell(self.m, self.maus, Bewegungsart.WARENEINGANG, D("20"))
        ls2 = self.ab.folgebeleg_erzeugen()
        self.assertEqual(ls2.versandkosten, D("0.00"))

    def test_unveraenderlicher_verlauf(self):
        self.uebernehmen({self.pos.pk: D("1")})
        e = BelegUebernahme.objects.get()
        with self.assertRaises(PermissionError):
            e.menge = D("9")
            e.save()
        with self.assertRaises(PermissionError):
            e.delete()

    def test_verlauf_gehoert_zum_mandanten_der_bestellung(self):
        self.uebernehmen({self.pos.pk: D("1")})
        self.assertEqual(BelegUebernahme.objects.get().mandant, self.m)


class UebernahmeGruppeSeriennummerTest(TeiluebernahmeBasis):
    def test_gruppe_unterpositionen_anteilig(self):
        self.pos.delete()
        kopf, ram, mm = self.gruppe("10", "20")
        ls = self.uebernehmen({kopf.pk: D("7")})
        lskopf = ls.positionen.get(gruppe__isnull=True)
        self.assertEqual(lskopf.menge, D("7"))
        self.assertEqual({t.bezeichnung: t.menge for t in lskopf.unterpositionen.all()}, {"RAM": D("14"), "Maus im PC": D("7")})
        for p in (kopf, ram, mm):
            p.refresh_from_db()
        self.assertEqual((kopf.menge, ram.menge, mm.menge), (D("3"), D("6"), D("3")))
        self.assertEqual(reserviert(self.m, self.ram), D("6"))
        self.assertEqual([p.pos for p in self.ab.positionen.all()], [1, 1, 1])  # Unterpositionen tragen die Nummer ihrer Gruppe

    def test_gruppe_nicht_teilbar_meldung_und_nichts_geaendert(self):
        self.pos.delete()
        kopf, ram, mm = self.gruppe("3", "1")  # 1 RAM auf 3 PCs
        with self.assertRaisesMessage(ValueError, "nicht gleichmäßig auf „RAM“"):
            self.uebernehmen({kopf.pk: D("2")})
        for p in (kopf, ram, mm):
            p.refresh_from_db()
            self.assertEqual(p.beleg_id, self.ab.pk)
        self.assertEqual((kopf.menge, ram.menge), (D("3"), D("1")))
        self.assertFalse(BelegUebernahme.objects.exists())

    def test_gruppe_ganz_uebernommen_haengt_mit_unterpositionen_um(self):
        self.pos.delete()
        kopf, ram, mm = self.gruppe("2", "4")
        ls = self.uebernehmen({kopf.pk: D("2")})
        self.assertEqual(ls.positionen.count(), 3)
        self.assertEqual(self.ab.positionen.count(), 0)
        self.assertTrue(Beleg.objects.get(pk=self.ab.pk).abgeschlossen)

    def test_seriennummern_wandern_mit(self):
        sns = [Seriennummer.objects.create(mandant=self.m, artikel=self.maus, nummer=f"SN{i}", position=self.pos) for i in range(1, 5)]
        ls = self.uebernehmen({self.pos.pk: D("2")})
        neu = ls.positionen.get()
        self.assertEqual(sorted(sn.nummer for sn in neu.seriennummern.all()), ["SN1", "SN2"])
        self.assertEqual(sorted(sn.nummer for sn in self.pos.seriennummern.all()), ["SN3", "SN4"])
        ls2 = self.uebernehmen({self.pos.pk: D("8")})  # volle Restmenge: die Position samt SN zieht um
        self.assertEqual(sorted(sn.nummer for sn in ls2.positionen.get().seriennummern.all()), ["SN3", "SN4"])
        self.assertEqual(len(sns), 4)


class MitziehenTest(TeiluebernahmeBasis):
    def aendern(self, beleg, pos, menge):
        return self.client.post(reverse("verkauf:position_aendern", args=[beleg.pk, pos.pk]),
                                {"menge": str(menge), "preis": str(pos.einzelpreis), "rabatt": "0"}, follow=True)

    def texte(self, r):
        return " ".join(str(m) for m in r.context["messages"])

    def test_menge_senken_bucht_die_differenz_zurueck(self):
        ls = self.uebernehmen({self.pos.pk: D("7")})
        p = ls.positionen.get()
        r = self.aendern(ls, p, 5)
        self.assertEqual(self.rest(), D("5"))
        p.refresh_from_db()
        self.assertEqual(p.menge, D("5"))
        self.assertIn("2 × Maus gehen zurück in die Bestellung", self.texte(r))
        z = BelegUebernahme.objects.get(richtung=BelegUebernahme.ZURUECK)
        self.assertEqual((z.menge, z.zielposition, z.quellposition), (D("2"), p, self.pos))
        self.assertEqual(reserviert(self.m, self.maus), D("5"))

    def test_menge_erhoehen_zieht_aus_dem_rest_hoechstens_bis_null(self):
        ls = self.uebernehmen({self.pos.pk: D("7")})
        p = ls.positionen.get()
        r = self.aendern(ls, p, 9)
        self.assertEqual(self.rest(), D("1"))
        self.assertIn("2 × Maus kommen zusätzlich aus dem Rest", self.texte(r))
        r = self.aendern(ls, p, 12)  # Rest 1 -> 1 Stück aus der Bestellung, 2 sind Zusatzmenge
        self.assertIn("2 × Maus sind Zusatzmenge", self.texte(r))
        self.assertFalse(Position.objects.filter(pk=self.pos.pk).exists())  # Bestellposition aufgebraucht
        self.ab.refresh_from_db()
        self.assertTrue(self.ab.abgeschlossen)
        p.refresh_from_db()
        self.assertEqual((p.menge, p.quellposition_id, uebernahme.netto_uebernommen(p)), (D("12"), None, D("10")))

    def test_zusatzmenge_wird_beim_loeschen_nicht_zurueckgebucht(self):
        ls = self.uebernehmen({self.pos.pk: D("10")})
        p = ls.positionen.get()
        self.aendern(ls, p, 12)  # nichts mehr in der Bestellung: alles Zusatzmenge
        self.client.post(reverse("verkauf:position_loeschen", args=[ls.pk, p.pk]))
        pos = self.ab.positionen.get()  # nur die ursprünglich übernommenen 10 kehren zurück, als neue Position
        self.assertEqual(pos.menge, D("10"))

    def test_position_loeschen_bucht_zurueck_und_oeffnet_erledigte_bestellung(self):
        ls = self.uebernehmen({self.pos.pk: D("10")})
        self.ab.refresh_from_db()
        self.assertTrue(self.ab.abgeschlossen)
        p = ls.positionen.get()
        self.client.post(reverse("verkauf:position_loeschen", args=[ls.pk, p.pk]))
        self.ab.refresh_from_db()
        self.assertFalse(self.ab.abgeschlossen)  # wieder offen
        pos = self.ab.positionen.get()
        self.assertEqual((pos.menge, pos.artikel, pos.einzelpreis, pos.pos, pos.verschoben), (D("10"), self.maus, D("10"), 1, False))
        self.assertEqual(self.ab.summe_netto, D("100.00"))
        self.assertEqual(reserviert(self.m, self.maus), D("10"))
        self.assertEqual(BelegUebernahme.objects.filter(richtung=BelegUebernahme.ZURUECK).get().menge, D("10"))
        # danach lässt sich wieder übernehmen
        self.ab.folgebeleg_erzeugen()

    def test_teilmenge_loeschen_addiert_auf_die_vorhandene_position(self):
        ls = self.uebernehmen({self.pos.pk: D("7")})
        self.client.post(reverse("verkauf:position_loeschen", args=[ls.pk, ls.positionen.get().pk]))
        self.assertEqual(self.rest(), D("10"))
        self.assertEqual(self.ab.positionen.count(), 1)

    def test_versandte_bestellung_voll_uebernommen_entwurf_loeschen_bleibt_versandt(self):
        Beleg.objects.filter(pk=self.ab.pk).update(abgeschlossen=True)  # beim Kunden versandt
        self.ab.refresh_from_db()
        re = self.uebernehmen({self.pos.pk: D("10")}, art=Belegart.RECHNUNG)
        self.client.post(reverse("verkauf:beleg_loeschen", args=[re.pk]))
        self.ab.refresh_from_db()
        self.assertTrue(self.ab.abgeschlossen)
        self.assertEqual(self.ab.positionen.get().menge, D("10"))

    def test_rechnungsentwurf_loeschen_bucht_alles_zurueck(self):
        Beleg.objects.filter(pk=self.ab.pk).update(versandkosten=D("4.00"))
        self.ab.refresh_from_db()
        re = self.ab.kopieren_als(Belegart.RECHNUNG)
        self.assertEqual(self.ab.positionen.count(), 0)
        r = self.client.post(reverse("verkauf:beleg_loeschen", args=[re.pk]))
        self.assertFalse(Beleg.objects.filter(pk=re.pk).exists())
        self.ab.refresh_from_db()
        self.assertFalse(self.ab.abgeschlossen)
        self.assertEqual(self.ab.positionen.get().menge, D("10"))
        self.assertEqual(self.ab.versandkosten, D("4.00"))  # mit dem Beleg zurück
        z = BelegUebernahme.objects.filter(richtung=BelegUebernahme.ZURUECK).get()
        self.assertIsNone(z.ziel)  # Beleg gelöscht, der Verlauf bleibt lesbar
        self.assertEqual(z.ziel_art, "RE")

    def test_gruppe_mitziehen_skaliert_unterpositionen(self):
        self.pos.delete()
        kopf, ram, mm = self.gruppe("10", "20")
        ls = self.uebernehmen({kopf.pk: D("7")})
        lskopf = ls.positionen.get(gruppe__isnull=True)
        self.aendern(ls, lskopf, 5)
        for p in (kopf, ram, mm):
            p.refresh_from_db()
        self.assertEqual((kopf.menge, ram.menge, mm.menge), (D("5"), D("10"), D("5")))
        self.assertEqual({t.bezeichnung: t.menge for t in lskopf.unterpositionen.all()}, {"RAM": D("10"), "Maus im PC": D("5")})

    def test_proforma_storno_bucht_zurueck(self):
        pf = self.ab.kopieren_als(Belegart.PROFORMA)
        pf.abschliessen()
        self.ab.refresh_from_db()
        self.assertTrue(self.ab.abgeschlossen)
        proforma_stornieren(pf, "Kunde kauft nicht")
        self.ab.refresh_from_db()
        self.assertFalse(self.ab.abgeschlossen)
        self.assertEqual(self.ab.positionen.get().menge, D("10"))
        self.assertEqual(reserviert(self.m, self.maus), D("10"))
        pf.refresh_from_db()
        self.assertEqual(pf.positionen.get().menge, D("10"))  # die Proforma bleibt unverändert
        self.assertEqual(BelegUebernahme.objects.filter(richtung=BelegUebernahme.ZURUECK).count(), 1)

    def test_alter_lieferschein_ist_keine_verschiebung_nichts_wird_mitgezogen(self):
        """Altdaten-Kopien (verschoben=False) bleiben, wie sie waren: Senken im Lieferschein gibt offene Menge frei."""
        ls = Beleg.objects.create(mandant=self.m, art=Belegart.LIEFERSCHEIN, kunde=self.kunde, vorgaenger=self.ab)
        alt = Position.objects.create(beleg=ls, artikel=self.maus, bezeichnung="Maus", menge=D("7"), einzelpreis=D("10"), quellposition=self.pos)
        self.assertEqual(self.pos.offen_menge, D("3"))
        self.aendern(ls, alt, 5)
        self.assertEqual(self.rest(), D("10"))
        self.assertEqual(self.pos.offen_menge, D("5"))
        self.assertFalse(BelegUebernahme.objects.exists())

    def test_teillieferung_abschliessen_verlauf_folgt_den_positionen(self):
        zweite = Position.objects.create(beleg=self.ab, artikel=self.ram, bezeichnung="RAM", menge=D("500"), einzelpreis=D("5"))
        self.ab.berechne()
        ls = self.uebernehmen({self.pos.pk: D("10"), zweite.pk: D("500")})  # RAM reicht nicht (100)
        neu = ls.teillieferung_abschliessen()
        e = BelegUebernahme.objects.get(zielposition__artikel=self.maus)
        self.assertEqual(e.ziel_beleg, neu)
        self.assertEqual(e.ziel_text, f"Lieferschein {neu.nummer}")


class AltdatenBefehlTest(TeiluebernahmeBasis):
    def altfall(self, menge_ls="7", abgeschlossen_ls=False):
        """Alte Teilübernahme: Lieferschein als Kopie (verschoben=False), die Bestellung steht noch auf 10."""
        ls = Beleg.objects.create(mandant=self.m, art=Belegart.LIEFERSCHEIN, kunde=self.kunde, vorgaenger=self.ab)
        kopie = Position.objects.create(beleg=ls, artikel=self.maus, bezeichnung="Maus", menge=D(menge_ls), einzelpreis=D("10"),
                                        quellposition=self.pos)
        if abgeschlossen_ls:
            ls.abschliessen()
        return ls, kopie

    def befehl(self, *args):
        aus, fehler = StringIO(), StringIO()
        call_command("rueckstand_umstellen", *args, stdout=aus, stderr=fehler)
        return aus.getvalue(), fehler.getvalue()

    def test_offene_menge_rechnet_im_altfall_richtig(self):
        self.altfall()
        self.assertEqual(self.pos.offen_menge, D("3"))
        self.assertEqual(reserviert(self.m, self.maus), D("3"))

    def test_gemischter_alt_und_neufall(self):
        self.altfall("7")
        self.uebernehmen({self.pos.pk: D("2")})
        self.assertEqual(self.rest(), D("8"))  # nur die neue Übernahme hat die Menge gesenkt
        self.assertEqual(self.pos.offen_menge, D("1"))  # 8 - 7 alte Kopie
        self.assertEqual(reserviert(self.m, self.maus), D("1"))
        with self.assertRaisesMessage(ValueError, "nur noch 1 offen"):
            self.uebernehmen({self.pos.pk: D("2")})

    def test_probelauf_aendert_nichts(self):
        ls, kopie = self.altfall()
        aus, _ = self.befehl()
        self.assertIn("PROBELAUF", aus)
        self.assertIn("10 -> 3", aus)
        self.assertEqual(self.rest(), D("10"))
        kopie.refresh_from_db()
        self.assertFalse(kopie.verschoben)
        self.assertFalse(BelegUebernahme.objects.exists())

    def test_ausfuehren_stellt_auf_den_rest_um(self):
        ls, kopie = self.altfall()
        aus, _ = self.befehl("--ausfuehren", "--mandant", "OKAY")
        self.assertIn("umgestellt", aus)
        self.assertEqual(self.rest(), D("3"))
        kopie.refresh_from_db()
        self.assertTrue(kopie.verschoben)
        self.assertEqual(kopie.menge, D("7"))
        self.assertEqual(self.pos.offen_menge, D("3"))  # unverändert richtig, jetzt ohne Altformel
        e = BelegUebernahme.objects.get()
        self.assertEqual((e.richtung, e.menge, e.zielposition, e.ziel_beleg), (BelegUebernahme.UMGESTELLT, D("7"), kopie, ls))
        self.assertEqual(reserviert(self.m, self.maus), D("3"))
        aus, _ = self.befehl("--ausfuehren")  # zweiter Lauf: nichts mehr zu tun
        self.assertIn("0 umgestellt", aus)
        self.assertEqual(self.rest(), D("3"))

    def test_umgestellter_altfall_mitziehen_funktioniert(self):
        ls, kopie = self.altfall()
        self.befehl("--ausfuehren")
        self.client.post(reverse("verkauf:position_aendern", args=[ls.pk, kopie.pk]), {"menge": "6", "preis": "10", "rabatt": "0"})
        self.assertEqual(self.rest(), D("4"))

    def test_versandte_bestellung_wird_umgestellt_status_bleibt(self):
        ls, kopie = self.altfall()
        Beleg.objects.filter(pk=self.ab.pk).update(abgeschlossen=True)  # beim Kunden versandt
        aus, _ = self.befehl()
        self.assertIn("würde umgestellt (beim Kunden versandt", aus)
        self.assertEqual(self.rest(), D("10"))  # Probelauf ändert nichts
        aus, _ = self.befehl("--ausfuehren")
        self.assertIn("1 umgestellt, 0 nur gemeldet", aus)
        self.assertEqual(self.rest(), D("3"))
        self.ab.refresh_from_db()
        self.assertTrue(self.ab.abgeschlossen)
        self.assertTrue(BelegUebernahme.objects.filter(richtung=BelegUebernahme.UMGESTELLT).exists())
        self.client.post(reverse("verkauf:position_aendern", args=[ls.pk, kopie.pk]), {"menge": "6", "preis": "10", "rabatt": "0"})
        self.assertEqual(self.rest(), D("4"))  # Mitziehen funktioniert auch danach

    def test_versandte_voll_gelieferte_bestellung_bleibt_beim_rueckbuchen_versandt(self):
        ls, kopie = self.altfall("10")
        Beleg.objects.filter(pk=self.ab.pk).update(abgeschlossen=True)  # beim Kunden versandt
        aus, _ = self.befehl("--ausfuehren")
        self.assertIn("umgestellt (beim Kunden versandt", aus)
        self.assertEqual(self.rest(), D("0"))
        self.client.post(reverse("verkauf:position_aendern", args=[ls.pk, kopie.pk]), {"menge": "6", "preis": "10", "rabatt": "0"})
        self.assertEqual(self.rest(), D("4"))
        self.ab.refresh_from_db()
        self.assertTrue(self.ab.abgeschlossen)  # Status „versandt“ bleibt
        self.assertFalse(self.ab.automatisch_erledigt)

    def test_offene_bestellung_auf_null_wird_automatisch_erledigt_und_ohne_versandt_hinweis(self):
        self.altfall("10")
        aus, _ = self.befehl("--ausfuehren")
        self.assertNotIn("versandt", aus)
        self.ab.refresh_from_db()
        self.assertTrue(self.ab.abgeschlossen and self.ab.automatisch_erledigt)

    def test_aktive_proforma_wird_nur_gemeldet(self):
        Beleg.objects.create(mandant=self.m, art=Belegart.PROFORMA, kunde=self.kunde, vorgaenger=self.ab)
        self.altfall()
        aus, _ = self.befehl("--ausfuehren")
        self.assertIn("aktive Proforma", aus)
        self.assertEqual(self.rest(), D("10"))

    def test_rest_null_bleibt_stehen_und_bestellung_ist_erledigt(self):
        ls, kopie = self.altfall("10", abgeschlossen_ls=True)  # abgeschlossener (gesperrter) Lieferschein
        self.befehl("--ausfuehren")
        self.pos.refresh_from_db()
        self.assertEqual(self.pos.menge, D("0"))  # nicht gelöscht
        self.ab.refresh_from_db()
        self.assertTrue(self.ab.abgeschlossen)
        kopie.refresh_from_db()
        self.assertTrue(kopie.verschoben)  # Kennzeichen auch im gesperrten Beleg
        self.assertEqual(kopie.menge, D("10"))

    def test_gruppe_im_altfall_wird_anteilig_umgestellt(self):
        self.pos.delete()
        kopf, ram, mm = self.gruppe("10", "20")
        ls = Beleg.objects.create(mandant=self.m, art=Belegart.LIEFERSCHEIN, kunde=self.kunde, vorgaenger=self.ab)
        k = Position.objects.create(beleg=ls, typ="G", bezeichnung="PC", menge=D("7"), einzelpreis=D("0"), quellposition=kopf)
        Position.objects.create(beleg=ls, gruppe=k, artikel=self.ram, bezeichnung="RAM", menge=D("20"), einzelpreis=D("5"), quellposition=ram)  # alter Fehler: volle Menge
        self.befehl("--ausfuehren")
        for p in (kopf, ram, mm):
            p.refresh_from_db()
        self.assertEqual((kopf.menge, ram.menge, mm.menge), (D("3"), D("6"), D("3")))
        self.assertFalse(Position.objects.filter(beleg=ls, verschoben=False).exists())

    def test_nicht_teilbare_gruppe_wird_gemeldet_und_nicht_umgestellt(self):
        self.pos.delete()
        kopf, ram, mm = self.gruppe("3", "1")
        ls = Beleg.objects.create(mandant=self.m, art=Belegart.LIEFERSCHEIN, kunde=self.kunde, vorgaenger=self.ab)
        Position.objects.create(beleg=ls, typ="G", bezeichnung="PC", menge=D("2"), einzelpreis=D("0"), quellposition=kopf)
        _, fehler = self.befehl("--ausfuehren")
        self.assertIn("nicht umgestellt", fehler)
        kopf.refresh_from_db()
        self.assertEqual(kopf.menge, D("3"))


class AnzeigeUndWeitereTest(TeiluebernahmeBasis):
    def test_bestellansicht_zeigt_rest_und_verlauf_folgebeleg_zeigt_herkunft(self):
        re = self.uebernehmen({self.pos.pk: D("7")}, art=Belegart.RECHNUNG)
        seite = self.client.get(reverse("verkauf:beleg", args=[self.ab.pk]))
        self.assertContains(seite, "7 übernommen in Rechnung (Entwurf), 3 offen")
        self.assertContains(seite, "Verlauf der Übernahmen")
        ziel = self.client.get(reverse("verkauf:beleg", args=[re.pk]))
        self.assertContains(ziel, "Aus der Bestellung übernommen")
        self.assertContains(ziel, f"Bestellung {self.ab.nummer}")

    def test_vollstaendig_uebernommene_bestellung_bleibt_in_den_listen_sichtbar(self):
        self.uebernehmen({self.pos.pk: D("10")})
        self.assertContains(self.client.get(reverse("verkauf:belege"), {"status": ""}), self.ab.nummer)
        self.assertContains(self.client.get(reverse("verkauf:kunde", args=[self.kunde.pk])), self.ab.nummer)
        seite = self.client.get(reverse("verkauf:beleg", args=[self.ab.pk]))
        self.assertContains(seite, "Verlauf der Übernahmen")
        self.assertContains(seite, "Lieferschein LS")

    def test_pdf_der_bestellung_hat_block_bereits_geliefert(self):
        ls = self.uebernehmen({self.pos.pk: D("7")})
        self.assertEqual(uebernahme.pdf_zeilen(self.ab), [f"7 × M1 Maus mit Lieferschein {ls.nummer}"])
        self.assertTrue(beleg_pdf(self.ab).startswith(b"%PDF"))
        self.assertEqual(uebernahme.pdf_zeilen(Beleg.objects.create(mandant=self.m, art=Belegart.AUFTRAG, kunde=self.kunde)), [])

    def test_pdf_zeilen_zaehlen_rueckbuchungen_ab(self):
        ls = self.uebernehmen({self.pos.pk: D("7")})
        uebernahme.zurueckbuchen(ls.positionen.get(), D("7"))
        self.assertEqual(uebernahme.pdf_zeilen(self.ab), [])

    def test_rechnungsentwurf_ohne_nummer_im_pdf_in_vorbereitung(self):
        self.uebernehmen({self.pos.pk: D("7")}, art=Belegart.RECHNUNG)
        self.assertEqual(uebernahme.pdf_zeilen(self.ab), ["7 × M1 Maus mit Rechnung (in Vorbereitung)"])

    def test_zurueckbuchen_ohne_bestellung_tut_nichts(self):
        ls = self.uebernehmen({self.pos.pk: D("7")})
        p = ls.positionen.get()
        Beleg.objects.filter(pk=self.ab.pk).delete()
        p.refresh_from_db()
        self.assertEqual(uebernahme.zurueckbuchen(p, D("7")), D("0"))
        self.assertTrue(BelegUebernahme.objects.exists())  # der Verlauf überlebt das Löschen der Bestellung


class LieferantenbestellungNachTeilubernahmeTest(TeiluebernahmeBasis):
    """„Nur fehlende Mengen“ beim Lieferanten: Der Rest der Bestellung darf nicht fälschlich als bestellt gelten."""

    def setUp(self):
        super().setUp()
        from apps.stammdaten.models import ArtikelLieferant, Lieferant
        self.lf = Lieferant.objects.create(nummer="L1", name1="Techdata GmbH")
        ArtikelLieferant.objects.create(artikel=self.maus, lieferant=self.lf, bestellnummer="M1", ek=D("5"), bestand=100)

    def bestellen(self, **kw):
        from apps.einkauf import services
        liste = services.bestellungen_aus_beleg(self.ab, benutzer=self.user, **kw)
        for b in liste:
            services.bestellen(b)
        return liste

    def bestellt(self):
        from apps.einkauf import services
        return services.bestellt_je_position(self.ab)[self.pos.pk]

    def test_alles_bestellt_dann_geliefert_rest_gilt_als_bestellt(self):
        self.bestellen()  # 10 beim Lieferanten bestellt
        self.uebernehmen({self.pos.pk: D("7")})  # 7 geliefert, in der Bestellung bleiben 3
        self.assertEqual(self.bestellt(), D("3"))  # 10 - 7 verbraucht = 3: der Rest ist abgedeckt

    def test_nach_der_lieferung_bestellt_deckt_den_rest(self):
        from apps.einkauf import services
        self.uebernehmen({self.pos.pk: D("7")})
        b = services.bestellung_anlegen(self.m, self.lf, aus_beleg=self.ab)
        services.bestellposition(b, self.maus, D("5"))
        services.bestellen(b)
        self.assertEqual(self.bestellt(), D("3"))  # die Bestellung kam nach der Lieferung: sie deckt den Rest (3) ab
        self.uebernehmen({self.pos.pk: D("1")})  # danach noch 1 geliefert: 5 - 1 = 4 >= Rest 2
        self.assertEqual(self.bestellt(), D("2"))

    def test_bestellt_vor_der_lieferung_und_zu_wenig_nur_fehlendes_bestellt_den_rest(self):
        from apps.einkauf import services
        b = services.bestellung_anlegen(self.m, self.lf, aus_beleg=self.ab)
        services.bestellposition(b, self.maus, D("5"))
        services.bestellen(b)
        self.uebernehmen({self.pos.pk: D("7")})  # danach 7 geliefert: die 5 sind verbraucht, der Rest (3) ist nicht gedeckt
        self.assertEqual(self.bestellt(), D("0"))
        neu = services.bestellungen_aus_beleg(self.ab, modus=services.MODUS_FEHLEND, benutzer=self.user)[0]
        self.assertEqual(neu.positionen.get().menge, D("3"))  # nur der Rest wird nachbestellt

    def test_nach_voller_lieferung_nichts_mehr_zu_bestellen_ist_keine_falsche_menge(self):
        self.bestellen()
        self.uebernehmen({self.pos.pk: D("10")})
        from apps.einkauf import services
        self.assertEqual(services.bestellbare_positionen(self.ab), [])


class ReviewFolgenTest(TeiluebernahmeBasis):
    """Korrekturen aus dem Review: Angebots-Kopien, Seriennummern beim Mitziehen, Gruppe, Meldungen, Sperre, Probelauf."""

    def aendern(self, beleg, pos, menge, **extra):
        daten = {"menge": str(menge), "preis": str(pos.einzelpreis), "rabatt": "0", **extra}
        return self.client.post(reverse("verkauf:position_aendern", args=[beleg.pk, pos.pk]), daten, follow=True)

    def sns(self, anzahl):
        return [Seriennummer.objects.create(mandant=self.m, artikel=self.maus, nummer=f"SN{i:02d}", position=self.pos)
                for i in range(1, anzahl + 1)]

    # -- Fund: Angebots-Kopie ist keine Teilübernahme
    def test_angebot_kopie_zaehlt_nicht_als_uebernahme_und_befehl_stellt_nicht_um(self):
        self.ab.kopieren_als(Belegart.ANGEBOT)
        self.assertEqual(self.pos.offen_menge, D("10"))
        self.assertEqual(uebernahme.altfaelle(), [])
        out = StringIO()
        call_command("rueckstand_umstellen", "--ausfuehren", stdout=out)
        self.assertEqual(self.rest(), D("10"))
        self.ab.refresh_from_db()
        self.assertFalse(self.ab.abgeschlossen)
        self.assertFalse(BelegUebernahme.objects.exists())

    # -- Fund: Probelauf meldet nicht teilbare Unterpositionen
    def test_probelauf_meldet_nicht_teilbare_gruppe(self):
        self.pos.delete()
        kopf, ram, mm = self.gruppe("3", "1")
        ls = Beleg.objects.create(mandant=self.m, art=Belegart.LIEFERSCHEIN, kunde=self.kunde, vorgaenger=self.ab)
        Position.objects.create(beleg=ls, typ="G", bezeichnung="PC", menge=D("2"), einzelpreis=D("0"), quellposition=kopf)
        aus, fehler = StringIO(), StringIO()
        call_command("rueckstand_umstellen", stdout=aus, stderr=fehler)
        self.assertIn("nicht umgestellt", fehler.getvalue())
        self.assertNotIn("würde umgestellt", aus.getvalue())

    # -- Funde: Seriennummern beim Nachziehen
    def test_nachziehen_bis_rest_null_nimmt_alle_seriennummern_mit(self):
        self.sns(10)
        ls = self.uebernehmen({self.pos.pk: D("7")})
        p = ls.positionen.get()
        self.aendern(ls, p, 10)
        self.assertEqual(p.seriennummern.count(), 10)
        self.assertFalse(Seriennummer.objects.filter(position__isnull=True).exists())

    def test_nachziehen_teilmenge_nimmt_seriennummern_mit(self):
        self.sns(10)
        ls = self.uebernehmen({self.pos.pk: D("7")})
        p = ls.positionen.get()
        self.aendern(ls, p, 8)
        self.assertEqual(p.seriennummern.count(), 8)
        self.assertEqual(self.pos.seriennummern.count(), 2)

    # -- Funde: Seriennummern beim Rückbuchen
    def test_menge_senken_gibt_ueberzaehlige_seriennummern_zurueck(self):
        self.sns(7)
        ls = self.uebernehmen({self.pos.pk: D("7")})
        p = ls.positionen.get()
        self.assertEqual(p.seriennummern.count(), 7)
        self.aendern(ls, p, 5)
        self.assertEqual(p.seriennummern.count(), 5)
        self.assertEqual(self.pos.seriennummern.count(), 2)

    def test_position_loeschen_gibt_seriennummern_an_die_bestellung(self):
        self.sns(7)
        ls = self.uebernehmen({self.pos.pk: D("7")})
        p = ls.positionen.get()
        self.client.post(reverse("verkauf:position_loeschen", args=[ls.pk, p.pk]))
        self.assertFalse(Seriennummer.objects.filter(position__isnull=True).exists())
        self.assertEqual(self.pos.seriennummern.count(), 7)

    def test_entwurf_loeschen_haengt_seriennummern_an_die_neu_angelegte_bestellposition(self):
        self.sns(10)
        re = self.uebernehmen({self.pos.pk: D("10")}, art=Belegart.RECHNUNG)  # ganze Position zieht um
        re.delete()
        self.assertFalse(Seriennummer.objects.filter(position__isnull=True).exists())
        ab_pos = self.ab.positionen.get()
        self.assertEqual((ab_pos.menge, ab_pos.seriennummern.count()), (D("10"), 10))

    # -- Fund: Gruppe
    def test_uebernommene_position_nicht_in_gruppe_verschiebbar(self):
        re = self.uebernehmen({self.pos.pk: D("3")}, art=Belegart.RECHNUNG)
        p = re.positionen.get()
        g = Position.objects.create(beleg=re, typ="G", bezeichnung="Set", menge=D("1"), einzelpreis=D("0"))
        r = self.aendern(re, p, 3, gruppe=g.pk)
        p.refresh_from_db()
        self.assertIsNone(p.gruppe_id)
        self.assertIn("nicht in eine Gruppe", " ".join(str(m) for m in r.context["messages"]))
        re.delete()
        self.assertEqual(self.rest(), D("10"))  # nichts verloren

    # -- Fund: Meldung nur bei Erfolg
    def test_fehlerhafter_rabatt_keine_rueckbuchungsmeldung_und_nichts_geaendert(self):
        ls = self.uebernehmen({self.pos.pk: D("7")})
        p = ls.positionen.get()
        r = self.client.post(reverse("verkauf:position_aendern", args=[ls.pk, p.pk]),
                             {"menge": "5", "preis": "10", "rabatt": "150"}, follow=True)
        texte = " ".join(str(m) for m in r.context["messages"])
        self.assertNotIn("gehen zurück", texte)
        self.assertEqual(self.rest(), D("3"))
        p.refresh_from_db()
        self.assertEqual(p.menge, D("7"))

    # -- Fund: veralteter Beleg nach der Sperre
    def test_veralteter_beleg_gibt_versandkosten_nicht_ein_zweites_mal_mit(self):
        Beleg.objects.filter(pk=self.ab.pk).update(versandkosten=D("5.90"))
        veraltet = Beleg.objects.get(pk=self.ab.pk)
        frisch = Beleg.objects.get(pk=self.ab.pk)
        neu = Beleg.objects.create(mandant=self.m, art=Belegart.LIEFERSCHEIN, kunde=self.kunde, vorgaenger=frisch)
        uebernahme.menge_verschieben(frisch, neu, {self.pos.pk: D("7")})
        uebernahme.nebenkosten_mitgeben(frisch, neu)
        zweiter = veraltet.kopieren_als(Belegart.RECHNUNG)
        self.assertEqual(zweiter.versandkosten, D("0"))
        self.assertEqual(veraltet.versandkosten, D("0"))


class LieferantenVerbrauchTest(LieferantenbestellungNachTeilubernahmeTest):
    """Übernommene Mengen verbrauchen den Vorrat beim Lieferanten nur einmal, nicht je Lieferantenposition."""

    def anlegen(self, menge):
        from apps.einkauf import services
        b = services.bestellung_anlegen(self.m, self.lf, aus_beleg=self.ab)
        services.bestellposition(b, self.maus, D(str(menge)))
        services.bestellen(b)
        return b

    def test_zwei_bestellungen_uebernahme_wird_nur_einmal_abgezogen(self):
        self.anlegen(5)
        self.anlegen(5)
        self.uebernehmen({self.pos.pk: D("7")})
        self.assertEqual(self.bestellt(), D("3"))  # 10 bestellt - 7 übernommen: der Rest ist gedeckt

    def test_sechs_und_vier_bestellt_vier_geliefert_rest_gedeckt(self):
        self.anlegen(6)
        self.anlegen(4)
        self.uebernehmen({self.pos.pk: D("4")})
        self.assertEqual(self.bestellt(), D("6"))

    def test_umgestellter_altfall_verbraucht_spaetere_bestellung_nicht(self):
        ls = Beleg.objects.create(mandant=self.m, art=Belegart.LIEFERSCHEIN, kunde=self.kunde, vorgaenger=self.ab)
        Position.objects.create(beleg=ls, artikel=self.maus, bezeichnung="Maus", menge=D("7"), einzelpreis=D("10"), quellposition=self.pos)
        self.anlegen(3)  # nach der alten Lieferung, für den Rest
        call_command("rueckstand_umstellen", "--ausfuehren", stdout=StringIO())
        self.assertEqual(self.rest(), D("3"))
        self.assertEqual(self.bestellt(), D("3"))
