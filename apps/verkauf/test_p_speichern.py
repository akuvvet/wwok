"""Regel des Nutzers (18.11.2026): Eine Bestellung mit Preislistenartikeln (P) lässt sich speichern – die P-Positionen wandern
dabei mit Warnung in ein Angebot, damit nachvollziehbar bleibt, was dem Kunden angeboten wurde."""
from decimal import Decimal

from django.urls import reverse

from .models import Beleg, Belegart, Position
from .test_p_kopieren import PKopierenTest


class PSpeichernTest(PKopierenTest):
    def speichern(self, b, **daten):
        return self.client.post(reverse("verkauf:beleg_abschliessen", args=[b.pk]), daten, follow=True)

    def neue_bestellung(self, mit_a=True, mit_p=True):
        """Wie in der Maske: „Neuer Beleg“ ist vorläufig und hat noch keine Nummer."""
        self.client.post(reverse("verkauf:beleg_neu", args=[self.kunde.pk]), {"art": Belegart.AUFTRAG})
        b = Beleg.objects.filter(art=Belegart.AUFTRAG).latest("pk")
        self.assertTrue(b.vorlaeufig and not b.nummer)
        if mit_a:
            Position.objects.create(beleg=b, artikel=self.maus, bezeichnung="Maus", menge=2, einzelpreis=Decimal("20"))
        if mit_p:
            Position.objects.create(beleg=b, typ="P", preisliste_artikel=self.pl, bezeichnung="Netzteil", menge=1, einzelpreis=Decimal("50"))
        b.berechne()
        return b

    def angebot(self):
        return Beleg.objects.get(art=Belegart.ANGEBOT)

    # ---- gemischt: P ins Angebot, Bestellung ohne sie gespeichert -----------------------------------
    def test_neue_bestellung_p_wandert_ins_angebot(self):
        ab = self.neue_bestellung()
        r = self.speichern(ab)
        ab.refresh_from_db()
        self.assertTrue(ab.abgeschlossen and ab.nummer.startswith("AB") and not ab.vorlaeufig)
        self.assertEqual(self.pos(ab), [(1, "Maus")])
        self.assertEqual(ab.summe_netto, Decimal("40.00"))
        an = self.angebot()
        self.assertEqual([p.bezeichnung for p in an.positionen.all()], ["Netzteil"])
        self.assertTrue(an.nummer.startswith("AN") and not an.vorlaeufig and not an.abgeschlossen)
        self.assertEqual((an.kunde, an.vorgaenger, an.summe_netto), (self.kunde, ab, Decimal("50.00")))
        text = " ".join(self.meldungen(r))
        self.assertIn(f"1 Preislistenartikel (P) wurden in das Angebot {an.nummer} verschoben", text)
        self.assertIn(f"die Bestellung {ab.nummer} ist ohne sie gespeichert", text)

    def test_bestellung_mit_nummer_p_wandert_ins_angebot(self):
        ab = self.beleg(Belegart.AUFTRAG)  # Altentwurf mit Nummer
        nummer = ab.nummer
        self.speichern(ab)
        ab.refresh_from_db()
        self.assertTrue(ab.abgeschlossen)
        self.assertEqual((ab.nummer, self.pos(ab)), (nummer, [(1, "Maus")]))
        self.assertEqual([p.bezeichnung for p in self.angebot().positionen.all()], ["Netzteil"])

    def test_gruppe_mit_p_geht_als_ganzes_ins_angebot(self):
        ab = self.neue_bestellung(mit_p=False)
        self.gruppe_mit_p(ab)
        self.speichern(ab)
        ab.refresh_from_db()
        self.assertEqual([p.bezeichnung for p in ab.positionen.all()], ["Maus"])
        self.assertEqual(sorted(p.bezeichnung for p in self.angebot().positionen.all()), ["Maus im Paket", "Netzteil im Paket", "Paket"])

    def test_ohne_p_entsteht_kein_angebot(self):
        ab = self.neue_bestellung(mit_p=False)
        r = self.speichern(ab)
        self.assertFalse(Beleg.objects.filter(art=Belegart.ANGEBOT).exists())
        self.assertNotIn("Preislistenartikel", " ".join(self.meldungen(r)))

    def test_doppeltes_speichern_legt_nur_ein_angebot_an(self):
        ab = self.neue_bestellung()
        self.speichern(ab)
        self.speichern(ab)
        self.assertEqual(Beleg.objects.filter(art=Belegart.ANGEBOT).count(), 1)

    # ---- nur P: die Bestellung wird zum Angebot --------------------------------------------------
    def test_neue_bestellung_nur_p_wird_angebot(self):
        ab = self.neue_bestellung(mit_a=False)
        Position.objects.create(beleg=ab, bezeichnung="Hinweis: Lieferzeit 3 Tage", menge=1, einzelpreis=Decimal("0"))  # freie Zeile geht mit
        r = self.speichern(ab)
        self.assertFalse(Beleg.objects.filter(pk=ab.pk).exists())  # hatte keine Nummer: keine Lücke im Nummernkreis
        self.assertFalse(Beleg.objects.filter(art=Belegart.AUFTRAG).exists())
        an = self.angebot()
        self.assertEqual([p.bezeichnung for p in an.positionen.all()], ["Netzteil", "Hinweis: Lieferzeit 3 Tage"])
        self.assertTrue(an.nummer.startswith("AN") and not an.vorlaeufig)
        self.assertIsNone(an.vorgaenger)
        self.assertEqual(r.redirect_chain[-1][0], reverse("verkauf:beleg", args=[an.pk]))
        self.assertIn(f"wurde als Angebot {an.nummer} gespeichert", " ".join(self.meldungen(r)))

    def test_bestellung_mit_nummer_nur_p_bleibt_leer_erledigt(self):
        ab = self.beleg(Belegart.AUFTRAG, mit_a=False)
        nummer = ab.nummer
        self.speichern(ab)
        ab.refresh_from_db()
        self.assertTrue(ab.abgeschlossen and ab.automatisch_erledigt)
        self.assertEqual((ab.nummer, ab.positionen.count()), (nummer, 0))
        an = self.angebot()
        self.assertEqual(([p.bezeichnung for p in an.positionen.all()], an.vorgaenger), (["Netzteil"], ab))

    # ---- unverändert -------------------------------------------------------------------------
    def test_abschliessen_ohne_auslagern_bleibt_gesperrt(self):
        """Nur das Speichern in der Maske lagert aus; der bloße Abschluss einer Bestellung mit P bleibt verboten."""
        ab = self.beleg(Belegart.AUFTRAG)
        with self.assertRaisesMessage(ValueError, "enthält Preislistenartikel (P) – nur als Angebot möglich"):
            ab.abschliessen()

    def test_rechnung_mit_p_laesst_sich_weiter_nicht_speichern(self):
        re = self.beleg(Belegart.RECHNUNG)
        self.speichern(re)
        re.refresh_from_db()
        self.assertFalse(re.abgeschlossen)
        self.assertFalse(Beleg.objects.filter(art=Belegart.ANGEBOT).exists())

    def test_hinweis_in_der_bestellung_kuendigt_das_verschieben_an(self):
        ab = self.neue_bestellung()
        self.assertIn("Beim Speichern werden sie in ein Angebot verschoben", ab.p_hinweis)
        self.assertContains(self.client.get(reverse("verkauf:beleg", args=[ab.pk])), "Beim Speichern werden sie in ein Angebot verschoben")
