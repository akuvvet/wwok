"""Preislistenartikel (P) gibt es nur im Angebot: beim Weiterführen/Kopieren bleiben sie zurück bzw. wandern aus der Bestellung ins Angebot."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.kern.models import Mandant
from apps.lager.models import Bewegungsart
from apps.lager.services import buche_manuell
from apps.stammdaten.models import Artikel, Kunde, Lieferant, Preislistenartikel, Steuersatz

from .models import Beleg, Belegart, Position


@override_settings(SECURE_SSL_REDIRECT=False)
class PKopierenTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.kunde = Kunde.objects.create(nummer="1", name1="Kunde GmbH")
        api = Lieferant.objects.create(nummer="70272", name1="API GmbH", kurzname="API")
        self.pl = Preislistenartikel.objects.create(
            lieferant=api, bestellnummer="500", herstellernummer="PSAZ-650", bezeichnung="Netzteil", ek=Decimal("40.0000"), bestand=5)
        self.maus = Artikel.objects.create(nummer="M1", bezeichnung="Maus", steuersatz=Steuersatz.objects.get(nummer=1), vk1=Decimal("20"))
        buche_manuell(self.m, self.maus, Bewegungsart.WARENEINGANG, Decimal("10"))  # genug Bestand: keine Engpass-Rückfrage
        self.client.force_login(get_user_model().objects.create_user("t", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    def beleg(self, art, mit_a=True, mit_p=True):
        b = Beleg.objects.create(mandant=self.m, art=art, kunde=self.kunde)
        if mit_a:
            Position.objects.create(beleg=b, artikel=self.maus, bezeichnung="Maus", menge=2, einzelpreis=Decimal("20"))
        if mit_p:
            Position.objects.create(beleg=b, typ="P", preisliste_artikel=self.pl, bezeichnung="Netzteil", menge=1, einzelpreis=Decimal("50"))
        b.berechne()
        return b

    def gruppe_mit_p(self, b):
        kopf = Position.objects.create(beleg=b, typ="G", bezeichnung="Paket", menge=1, einzelpreis=Decimal("0"))
        Position.objects.create(beleg=b, gruppe=kopf, artikel=self.maus, bezeichnung="Maus im Paket", menge=1, einzelpreis=Decimal("20"))
        Position.objects.create(beleg=b, gruppe=kopf, typ="P", preisliste_artikel=self.pl, bezeichnung="Netzteil im Paket",
                                menge=1, einzelpreis=Decimal("50"))
        b.berechne()
        return kopf

    def pos(self, b):
        return [(p.pos, p.bezeichnung) for p in b.positionen.all()]

    # ---- Quelle Angebot: P bleibt zurück ----------------------------------------------------------
    def test_angebot_zu_bestellung_nur_a_p_bleibt_im_angebot(self):
        an = self.beleg(Belegart.ANGEBOT)
        ab = an.folgebeleg_erzeugen()
        self.assertEqual(self.pos(ab), [(1, "Maus")])
        self.assertEqual(ab.summe_netto, Decimal("40.00"))
        self.assertEqual(an.positionen.count(), 2)  # Angebot unverändert
        self.assertEqual(ab.p_info["anzahl"], 1)
        self.assertIsNone(ab.p_info["angebot"])
        self.assertEqual(ab.p_hinweis, "")

    def test_angebot_kopieren_als_ls_und_rechnung_nur_a(self):
        an = self.beleg(Belegart.ANGEBOT)
        for art in (Belegart.AUFTRAG, Belegart.RECHNUNG):
            neu = an.kopieren_als(art)
            self.assertEqual(self.pos(neu), [(1, "Maus")], art)
            self.assertEqual(an.positionen.count(), 2)
        with self.assertRaisesMessage(ValueError, "nur aus einer Bestellung"):
            an.kopieren_als(Belegart.LIEFERSCHEIN)

    def test_angebot_nur_p_fehler_nichts_angelegt(self):
        an = self.beleg(Belegart.ANGEBOT, mit_a=False)
        for aufruf in (lambda: an.folgebeleg_erzeugen(), lambda: an.kopieren_als(Belegart.RECHNUNG)):
            with self.assertRaisesMessage(ValueError, "enthält nur Preislistenartikel (P) – sie bleiben Angebot"):
                aufruf()
        self.assertEqual(Beleg.objects.count(), 1)
        self.assertEqual(an.positionen.count(), 1)

    def test_angebot_zu_angebot_kopiert_p_weiter(self):
        an = self.beleg(Belegart.ANGEBOT)
        kopie = an.kopieren_als(Belegart.ANGEBOT)
        self.assertEqual(kopie.positionen.count(), 2)
        self.assertEqual(kopie.p_positionen().count(), 1)
        self.assertIsNone(getattr(kopie, "p_info", None))

    def test_gruppe_mit_p_unterposition_bleibt_im_angebot_komplett(self):
        an = self.beleg(Belegart.ANGEBOT, mit_p=False)
        self.gruppe_mit_p(an)
        ab = an.folgebeleg_erzeugen()
        self.assertEqual(self.pos(ab), [(1, "Maus")])
        self.assertEqual(an.positionen.count(), 4)

    # ---- Quelle Bestellung: P wandert in ein neues Angebot -----------------------------------------
    def test_bestellung_zu_rechnung_p_wandert_ins_angebot_bestellung_abgeschlossen(self):
        ab = self.beleg(Belegart.AUFTRAG)
        re = ab.kopieren_als(Belegart.RECHNUNG)
        self.assertEqual(self.pos(re), [(1, "Maus")])
        angebot = Beleg.objects.get(art=Belegart.ANGEBOT)
        self.assertEqual((angebot.kunde, angebot.vorgaenger, angebot.abgeschlossen), (self.kunde, ab, False))
        self.assertEqual(self.pos(angebot), [(1, "Netzteil")])
        self.assertEqual(angebot.summe_netto, Decimal("50.00"))
        ab.refresh_from_db()
        self.assertEqual(self.pos(ab), [])  # die Maus wanderte in die Rechnung, das Netzteil ins Angebot
        self.assertEqual(ab.summe_netto, Decimal("0.00"))
        self.assertFalse(ab.p_positionen().exists())
        self.assertTrue(ab.abgeschlossen)
        self.assertEqual(re.p_info["angebot"], angebot)
        self.assertEqual(re.p_info["anzahl"], 1)
        self.assertEqual(re.p_hinweis, "")

    def test_bestellung_zu_lieferschein_folgebeleg_ebenso(self):
        ab = self.beleg(Belegart.AUFTRAG)
        ls = ab.folgebeleg_erzeugen()
        self.assertEqual((ls.art, self.pos(ls)), (Belegart.LIEFERSCHEIN, [(1, "Maus")]))
        angebot = Beleg.objects.get(art=Belegart.ANGEBOT)
        self.assertEqual(self.pos(angebot), [(1, "Netzteil")])
        ab.refresh_from_db()
        self.assertTrue(ab.abgeschlossen)
        self.assertFalse(ab.p_positionen().exists())

    def test_bestellung_mit_urbeleg_abschliessen_scheitert_nicht_am_p(self):
        ab = self.beleg(Belegart.AUFTRAG)
        ab.kopieren_als(Belegart.RECHNUNG, urbeleg_abschliessen=True)
        ab.refresh_from_db()
        self.assertTrue(ab.abgeschlossen)

    def test_gruppe_mit_p_wandert_komplett_ins_angebot(self):
        ab = self.beleg(Belegart.AUFTRAG, mit_p=False)
        kopf = self.gruppe_mit_p(ab)
        re = ab.kopieren_als(Belegart.RECHNUNG)
        self.assertEqual(self.pos(re), [(1, "Maus")])
        angebot = Beleg.objects.get(art=Belegart.ANGEBOT)
        self.assertEqual(angebot.positionen.count(), 3)
        self.assertEqual(angebot.positionen.get(typ="G").pk, kopf.pk)
        self.assertEqual({p.pos for p in angebot.positionen.all()}, {1})  # Unterpositionen tragen die Nummer ihrer Gruppe
        self.assertEqual(angebot.positionen.filter(gruppe=kopf).count(), 2)
        self.assertEqual(ab.positionen.count(), 0)  # die Maus wanderte in die Rechnung, die Gruppe ins Angebot
        ab.refresh_from_db()
        self.assertTrue(ab.abgeschlossen)  # leer gewordene Bestellung = erledigt
        self.assertEqual(re.p_info["anzahl"], 1)

    def test_positionsnummern_bleiben_lueckenlos(self):
        ab = self.beleg(Belegart.AUFTRAG)
        Position.objects.create(beleg=ab, artikel=self.maus, bezeichnung="Maus 2", menge=1, einzelpreis=Decimal("20"))
        Position.objects.create(beleg=ab, typ="P", preisliste_artikel=self.pl, bezeichnung="Netzteil 2", menge=1, einzelpreis=Decimal("50"))
        ab.berechne()
        self.assertEqual([p.pos for p in ab.positionen.all()], [1, 2, 3, 4])
        re = ab.kopieren_als(Belegart.RECHNUNG)
        angebot = Beleg.objects.get(art=Belegart.ANGEBOT)
        self.assertEqual([p.pos for p in re.positionen.all()], [1, 2])
        self.assertEqual([p.pos for p in angebot.positionen.all()], [1, 2])
        ab.refresh_from_db()
        self.assertEqual([p.pos for p in ab.positionen.all()], [])  # beide A-Positionen wanderten in die Rechnung
        self.assertTrue(ab.abgeschlossen)

    def test_bestellung_nur_p_fehler_nichts_verschoben(self):
        ab = self.beleg(Belegart.AUFTRAG, mit_a=False)
        for aufruf in (lambda: ab.folgebeleg_erzeugen(), lambda: ab.kopieren_als(Belegart.RECHNUNG)):
            with self.assertRaisesMessage(ValueError, "nur noch Preislistenartikel (P)"):
                aufruf()
        self.assertEqual(Beleg.objects.count(), 1)
        self.assertEqual(ab.positionen.count(), 1)
        self.assertFalse(ab.abgeschlossen)

    def test_teiluebernahme_mit_rest_bleibt_offen(self):
        from unittest import mock
        ab = self.beleg(Belegart.AUFTRAG)
        maus = ab.positionen.get(artikel=self.maus)
        mengen = {maus.pk: Decimal("1")}  # Teillieferung: 1 von 2 Stück (Engpass-Entscheidung)
        with mock.patch.object(Beleg, "uebernahme_mengen", return_value=mengen):
            re = ab.kopieren_als(Belegart.RECHNUNG)
        self.assertEqual(re.positionen.get().menge, Decimal("1"))
        angebot = Beleg.objects.get(art=Belegart.ANGEBOT)
        self.assertEqual(self.pos(angebot), [(1, "Netzteil")])
        ab.refresh_from_db()
        self.assertFalse(ab.abgeschlossen)  # Rest (1 Maus) bleibt offen und reserviert
        self.assertEqual(ab.offene_mengen(), {maus.pk: Decimal("1.000")})
        self.assertFalse(ab.p_positionen().exists())

    def test_nur_p_plus_textposition_zaehlt_als_nur_p(self):
        # Angebot: P + freie Textposition → nichts übernehmbar, nichts angelegt
        an = self.beleg(Belegart.ANGEBOT, mit_a=False)
        Position.objects.create(beleg=an, bezeichnung="Hinweistext", menge=1, einzelpreis=Decimal("0"))
        for aufruf in (lambda: an.folgebeleg_erzeugen(), lambda: an.kopieren_als(Belegart.RECHNUNG)):
            with self.assertRaisesMessage(ValueError, "enthält nur Preislistenartikel (P) – sie bleiben Angebot"):
                aufruf()
        self.assertEqual(Beleg.objects.count(), 1)
        self.assertEqual(an.positionen.count(), 2)
        # Bestellung: P + Textposition → ebenso nichts verschoben, nichts angelegt
        ab = self.beleg(Belegart.AUFTRAG, mit_a=False)
        Position.objects.create(beleg=ab, bezeichnung="Hinweistext", menge=1, einzelpreis=Decimal("0"))
        for aufruf in (lambda: ab.folgebeleg_erzeugen(), lambda: ab.kopieren_als(Belegart.RECHNUNG)):
            with self.assertRaisesMessage(ValueError, "nur noch Preislistenartikel (P)"):
                aufruf()
        self.assertEqual(Beleg.objects.count(), 2)
        self.assertEqual(ab.positionen.count(), 2)
        self.assertFalse(ab.abgeschlossen)

    # ---- Proforma: die Menge wandert in die Proforma, der Storno bucht sie zurück --------------------
    def test_bestellung_zu_proforma_p_im_angebot_storno_oeffnet_bestellung_wieder(self):
        from apps.finanzen.anzahlung import proforma_stornieren
        ab = self.beleg(Belegart.AUFTRAG)
        pf = ab.kopieren_als(Belegart.PROFORMA)
        self.assertEqual(pf.art, Belegart.PROFORMA)
        self.assertEqual(self.pos(pf), [(1, "Maus")])
        angebot = Beleg.objects.get(art=Belegart.ANGEBOT)
        self.assertEqual(self.pos(angebot), [(1, "Netzteil")])
        ab.refresh_from_db()
        self.assertTrue(ab.abgeschlossen)  # A ist in der Proforma, P im Angebot: die Bestellung ist leer = erledigt
        self.assertNotIn("abgeschlossen", pf.p_info)
        self.assertEqual(ab.offene_mengen(), {})
        self.assertFalse(ab.positionen.exists())
        proforma_stornieren(pf, "Kunde kauft nicht")
        ab.refresh_from_db()
        self.assertFalse(ab.abgeschlossen)  # Storno bucht die Menge zurück und öffnet die Bestellung wieder
        maus = ab.positionen.get(artikel=self.maus)
        self.assertEqual(ab.offene_mengen(), {maus.pk: Decimal("2.000")})  # A wieder offen
        self.assertFalse(ab.gesperrt)
        self.assertEqual(self.pos(ab), [(1, "Maus")])

    # ---- Engpass mit P -------------------------------------------------------------------------------
    def engpass_bestellung(self):
        ab = self.beleg(Belegart.AUFTRAG, mit_a=False)
        pos = Position.objects.create(beleg=ab, artikel=self.maus, bezeichnung="Maus", menge=15, einzelpreis=Decimal("20"))
        ab.berechne()
        return ab, pos

    def test_engpass_nichts_uebernehmen_mit_p_fehler_p_bleibt(self):
        ab, pos = self.engpass_bestellung()  # Bestand 10, bestellt 15
        with self.assertRaisesMessage(ValueError, "Es wird nichts übernommen"):
            ab.folgebeleg_erzeugen(entscheidungen={pos.pk: 0})
        self.assertEqual(Beleg.objects.count(), 1)
        self.assertEqual(ab.positionen.count(), 2)
        self.assertEqual(ab.p_positionen().count(), 1)
        self.assertFalse(ab.abgeschlossen)

    def test_engpass_teilmenge_mit_p_ls_teilmenge_p_im_angebot_ab_offen(self):
        ab, pos = self.engpass_bestellung()
        ls = ab.folgebeleg_erzeugen(entscheidungen={pos.pk: Decimal("4")})
        self.assertEqual(ls.art, Belegart.LIEFERSCHEIN)
        self.assertEqual(ls.positionen.get().menge, Decimal("4"))
        angebot = Beleg.objects.get(art=Belegart.ANGEBOT)
        self.assertEqual(self.pos(angebot), [(1, "Netzteil")])
        ab.refresh_from_db()
        self.assertFalse(ab.abgeschlossen)
        self.assertEqual(ab.offene_mengen(), {pos.pk: Decimal("11.000")})
        self.assertFalse(ab.p_positionen().exists())

    # ---- Meldungen -------------------------------------------------------------------------------
    def meldungen(self, antwort):
        return [str(m) for m in antwort.context["messages"]]

    def test_meldung_beim_kopieren_angebot(self):
        an = self.beleg(Belegart.ANGEBOT)
        r = self.client.post(reverse("verkauf:beleg_kopieren", args=[an.pk]), {"art": Belegart.RECHNUNG}, follow=True)
        text = " ".join(self.meldungen(r))
        self.assertIn("1 Preislistenartikel (P) sind im Angebot", text)
        self.assertIn("geblieben", text)

    def test_meldung_beim_kopieren_bestellung(self):
        ab = self.beleg(Belegart.AUFTRAG)
        r = self.client.post(reverse("verkauf:beleg_kopieren", args=[ab.pk]), {"art": Belegart.RECHNUNG}, follow=True)
        text = " ".join(self.meldungen(r))
        angebot = Beleg.objects.get(art=Belegart.ANGEBOT)
        self.assertIn(f"1 Preislistenartikel (P) wurden in das neue Angebot {angebot.nummer} übernommen", text)
        self.assertIn("ist erledigt", text)

    def test_meldung_beim_folgebeleg_angebot(self):
        an = self.beleg(Belegart.ANGEBOT)
        r = self.client.post(reverse("verkauf:beleg_folgebeleg", args=[an.pk]), {"von_art": "AN"}, follow=True)
        self.assertIn("Preislistenartikel (P) sind im Angebot", " ".join(self.meldungen(r)))
