"""Vorläufiges Angebot: Übernehmen und Kopieren daraus fragen nicht nach. Das Angebot wird erst mit „Speichern: abschließen &
Nummer vergeben“ des neuen Belegs abgeschlossen und nummeriert; wird der neue Beleg verworfen, ist alles wie vorher (das Angebot
bleibt nicht gespeichert, ohne Nummer, mit eigenem Vorgang)."""
from datetime import timedelta
from decimal import Decimal

from django.urls import reverse
from django.utils import timezone

from . import vorgang
from .models import Beleg, BelegUebernahme, BelegUmwandlung, BelegVorgang, Belegart, Position, Vorgangsweg
from .test_vorlaeufig import VorlaeufigBasis, letzte

D = Decimal


class AngebotBasis(VorlaeufigBasis):
    def angebot(self):
        an = self.vorlaeufig(Belegart.ANGEBOT)  # eine Maus (2) und ein eigener Vorgang NEU
        an.refresh_from_db()
        return an

    def kopie(self, an, art=Belegart.RECHNUNG):
        r = self.client.post(reverse("verkauf:beleg_kopieren", args=[an.pk]), {"art": art, "urbeleg_abschliessen": "0"}, follow=True)
        self.assertEqual(r.status_code, 200)
        return Beleg.objects.get(art=art, vorlaeufig=True), r

    def positionen(self, beleg):
        return list(Position.objects.filter(beleg=beleg).order_by("pk").values_list("pk", "pos", "artikel_id", "menge", "einzelpreis"))

    def aktiv_alt(self, beleg, stunden=3):
        Beleg.objects.filter(pk=beleg.pk).update(zuletzt_aktiv=timezone.now() - timedelta(hours=stunden))


class OhneRueckfrageTest(AngebotBasis):
    def test_kopie_ohne_rueckfrage_und_angebot_bleibt_ohne_nummer(self):
        for art in (Belegart.RECHNUNG, Belegart.AUFTRAG):
            an = self.angebot()
            vorher = letzte(self.m, Belegart.ANGEBOT)
            ziel, r = self.kopie(an, art)
            self.assertTemplateNotUsed(r, "verkauf/erst_abschliessen_frage.html")
            an.refresh_from_db()
            self.assertTrue(an.vorlaeufig and not an.abgeschlossen and an.nummer is None, art)
            self.assertEqual(an.vorgang.weg, Vorgangsweg.NEU)
            self.assertEqual(letzte(self.m, Belegart.ANGEBOT), vorher)
            v = ziel.vorgang
            self.assertEqual((v.weg, v.quelle_id, v.quelle_abgeschlossen_durch_vorgang, v.stand["abschluss_modus"]),
                             (Vorgangsweg.KOPIE, an.pk, True, "angebot"))
            vorgang.beleg_verwerfen(ziel)
            self.assertFalse(Beleg.objects.filter(pk=ziel.pk).exists())
            vorgang.beleg_verwerfen(an)

    def test_bestellung_folgebeleg_per_view_ohne_rueckfrage(self):
        an = self.angebot()
        r = self.client.post(reverse("verkauf:beleg_folgebeleg", args=[an.pk]), {"von_art": "AN"}, follow=True)
        self.assertTemplateNotUsed(r, "verkauf/erst_abschliessen_frage.html")
        an.refresh_from_db()
        self.assertEqual((an.art, an.vorlaeufig, an.nummer), (Belegart.AUFTRAG, True, None))
        self.assertEqual(an.vorgang.weg, Vorgangsweg.UMWANDLUNG)
        self.assertTrue(any("nicht gespeichert" in str(m) for m in r.context["messages"]))

    def test_bestellung_bleibt_bei_der_rueckfrage(self):
        ab = self.vorlaeufig(Belegart.AUFTRAG)
        r = self.client.post(reverse("verkauf:beleg_folgebeleg", args=[ab.pk]), {"von_art": "AB", "art": "LS"})
        self.assertTemplateUsed(r, "verkauf/erst_abschliessen_frage.html")
        r = self.client.post(reverse("verkauf:beleg_kopieren", args=[ab.pk]), {"art": "AN"})
        self.assertTemplateUsed(r, "verkauf/erst_abschliessen_frage.html")
        ab.refresh_from_db()
        self.assertTrue(ab.vorlaeufig and ab.nummer is None)

    def test_template_beim_vorlaeufigen_angebot_ohne_data_erst_bei_der_bestellung_mit(self):
        an = self.angebot()
        html = self.client.get(reverse("verkauf:beleg", args=[an.pk])).content.decode()
        self.assertNotIn("data-bleibt data-erst", html)
        self.assertIn('data-art="AN" data-vorlaeufig data-bleibt', html)  # kein „Angebot offen lassen?“
        ab = self.vorlaeufig(Belegart.AUFTRAG)
        html = self.client.get(reverse("verkauf:beleg", args=[ab.pk])).content.decode()
        self.assertIn("data-bleibt data-erst", html)
        self.assertNotIn("data-vorlaeufig", html)


class AbschlussTest(AngebotBasis):
    def test_ziel_abschliessen_nummeriert_erst_das_angebot_dann_das_ziel(self):
        an = self.angebot()
        ziel, _ = self.kopie(an)
        vorher = (letzte(self.m, Belegart.ANGEBOT), letzte(self.m, Belegart.RECHNUNG))
        ziel.abschliessen()
        an.refresh_from_db()
        ziel.refresh_from_db()
        self.assertTrue(an.nummer.startswith("AN") and an.abgeschlossen and not an.vorlaeufig and an.zuletzt_aktiv is None)
        self.assertTrue(ziel.nummer.startswith("RE") and ziel.abgeschlossen)
        self.assertFalse(BelegVorgang.objects.filter(beleg__in=[an, ziel]).exists())  # der eigene NEU-Vorgang entfällt
        self.assertEqual((letzte(self.m, Belegart.ANGEBOT), letzte(self.m, Belegart.RECHNUNG)), (vorher[0] + 1, vorher[1] + 1))

    def test_ziel_abschliessen_ueber_die_view(self):
        an = self.angebot()
        ziel, _ = self.kopie(an, Belegart.AUFTRAG)
        self.client.post(reverse("verkauf:beleg_abschliessen", args=[ziel.pk]), {"danach": ""})
        an.refresh_from_db()
        self.assertTrue(an.abgeschlossen and an.nummer)

    def test_angebot_zwischendurch_selbst_gespeichert_nichts_doppelt(self):
        an = self.angebot()
        ziel, _ = self.kopie(an)
        an.abschliessen()
        nummer = Beleg.objects.get(pk=an.pk).nummer
        zaehler = letzte(self.m, Belegart.ANGEBOT)
        ziel.abschliessen()
        self.assertEqual(Beleg.objects.get(pk=an.pk).nummer, nummer)
        self.assertEqual(letzte(self.m, Belegart.ANGEBOT), zaehler)
        self.assertTrue(Beleg.objects.get(pk=ziel.pk).nummer)

    def test_scheitert_das_angebot_bleibt_alles_unveraendert(self):
        an = self.angebot()
        ziel, _ = self.kopie(an)
        Position.objects.filter(beleg=an).delete()  # das Angebot ist jetzt leer und nicht abschließbar
        zaehler = (letzte(self.m, Belegart.ANGEBOT), letzte(self.m, Belegart.RECHNUNG))
        with self.assertRaises(ValueError):
            ziel.abschliessen()
        ziel.refresh_from_db()
        an.refresh_from_db()
        self.assertTrue(ziel.vorlaeufig and ziel.nummer is None and not ziel.abgeschlossen)
        self.assertTrue(an.vorlaeufig and an.nummer is None)
        self.assertEqual((letzte(self.m, Belegart.ANGEBOT), letzte(self.m, Belegart.RECHNUNG)), zaehler)
        self.assertTrue(BelegVorgang.objects.filter(beleg=ziel).exists())


class ReviewFolgeTest(AngebotBasis):
    def test_kopie_als_angebot_dann_umwandeln_schliesst_die_bestellung_nicht_still_ab(self):
        an = self.angebot()
        r = self.client.post(reverse("verkauf:beleg_kopieren", args=[an.pk]), {"art": "AN", "urbeleg_abschliessen": "0"}, follow=True)
        kopie = Beleg.objects.get(art=Belegart.ANGEBOT, vorlaeufig=True, vorgang__quelle=an)  # Kopie derselben Art: vorgaenger None
        self.assertEqual(kopie.vorgang.quelle_id, an.pk)
        # Umwandlung wird nicht angeboten: stattdessen entsteht ein neuer Folgebeleg
        self.assertFalse(an.umwandlung_moeglich(Belegart.AUFTRAG, None, []))
        with self.assertRaises(ValueError):
            an._umwandeln(Belegart.AUFTRAG, vorlaeufig=True)
        self.client.post(reverse("verkauf:beleg_folgebeleg", args=[an.pk]), {"von_art": "AN"}, follow=True)
        an.refresh_from_db()
        self.assertEqual(an.art, Belegart.ANGEBOT)  # nicht umgewandelt
        ab = Beleg.objects.get(art=Belegart.AUFTRAG, vorlaeufig=True)
        self.assertNotEqual(ab.pk, an.pk)
        # Kopie speichern: das Angebot wird mit abgeschlossen, die Bestellung bleibt vorläufig
        kopie.abschliessen()
        an.refresh_from_db()
        ab.refresh_from_db()
        self.assertTrue(an.abgeschlossen and an.nummer.startswith("AN"))
        self.assertTrue(ab.vorlaeufig and not ab.abgeschlossen and ab.nummer is None)

    def test_angebot_vorab_schliesst_nur_ein_vorlaeufiges_angebot(self):
        an = self.angebot()
        ziel, _ = self.kopie(an)
        Beleg.objects.filter(pk=an.pk).update(art=Belegart.AUFTRAG)  # Quelle ist kein Angebot mehr
        zaehler = letzte(self.m, Belegart.AUFTRAG)
        ziel.abschliessen()
        an.refresh_from_db()
        self.assertTrue(an.vorlaeufig and not an.abgeschlossen and an.nummer is None)
        self.assertEqual(letzte(self.m, Belegart.AUFTRAG), zaehler)

    def test_leeres_angebot_meldung_nennt_das_angebot(self):
        an = self.angebot()
        ziel, _ = self.kopie(an)
        Position.objects.filter(beleg=an).delete()
        with self.assertRaisesMessage(ValueError, "Das Angebot, aus dem dieser Beleg entstanden ist, lässt sich nicht abschließen"):
            ziel.abschliessen()
        ziel.refresh_from_db()
        self.assertTrue(ziel.vorlaeufig and ziel.nummer is None)

    def test_leeres_angebot_ueber_die_view_zeigt_die_meldung_am_ziel(self):
        an = self.angebot()
        ziel, _ = self.kopie(an)
        Position.objects.filter(beleg=an).delete()
        r = self.client.post(reverse("verkauf:beleg_abschliessen", args=[ziel.pk]), {"danach": ""}, follow=True)
        texte = [str(m) for m in r.context["messages"]]
        self.assertTrue(any("Das Angebot, aus dem dieser Beleg entstanden ist" in t for t in texte), texte)

    def test_ziellink_im_balken_wird_vom_verlassen_skript_durchgelassen(self):
        an = self.angebot()
        ziel, _ = self.kopie(an)
        html = self.client.get(reverse("verkauf:beleg", args=[an.pk])).content.decode()
        self.assertIn('id="zielbalken" role="status" data-bleibt', html)
        self.assertIn("wert: 'verlassen'", html)
        self.assertNotIn("wert: 'verwerfen'", html)
        # ohne offenes Ziel gibt es „Verlassen“ nicht, dafür „Verwerfen“
        vorgang.beleg_verwerfen(ziel)
        html = self.client.get(reverse("verkauf:beleg", args=[an.pk])).content.decode()
        self.assertIn("wert: 'verwerfen'", html)
        self.assertNotIn("wert: 'verlassen'", html)

    def test_teillieferung_aus_angebotskopie_sperrt_zuerst_das_angebot(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        from apps.stammdaten.models import Artikel, Steuersatz
        knapp = Artikel.objects.create(nummer="K1", bezeichnung="Knapp", steuersatz=Steuersatz.objects.get(nummer=1), vk1=D("8"))
        an = self.angebot()
        self.pos(an, knapp, menge="5", preis="8")
        an.berechne()
        re = an.kopieren_als(Belegart.RECHNUNG, urbeleg_abschliessen=True, vorlaeufig=True)
        with CaptureQueriesContext(connection) as q:
            re.teillieferung_abschliessen()
        sqls = [x["sql"] for x in q.captured_queries if 'FROM "verkauf_beleg"' in x["sql"]]

        def erster(pk):
            return next(i for i, s in enumerate(sqls) if f'"verkauf_beleg"."id" = {pk}' in s)
        # nach der Prüfung des Bestands (teilung) wird erst das Angebot gelesen/gesperrt, dann der Beleg
        self.assertLess(erster(an.pk), len(sqls))
        sperr_ziel = [i for i, s in enumerate(sqls) if f'"verkauf_beleg"."id" = {re.pk}' in s]
        self.assertTrue(sperr_ziel)
        self.assertLess(erster(an.pk), sperr_ziel[0])


class VerwerfenTest(AngebotBasis):
    def test_ziel_verwerfen_angebot_wieder_wie_vorher(self):
        an = self.angebot()
        an.bemerkung, an.vortext = "intern", "Hallo"
        an.save()
        vorher = (self.positionen(an), letzte(self.m, Belegart.ANGEBOT), letzte(self.m, Belegart.RECHNUNG))
        self.aktiv_alt(an)
        ziel, _ = self.kopie(an)
        ergebnis = vorgang.beleg_verwerfen(ziel)
        self.assertEqual(ergebnis["weiter"], reverse("verkauf:beleg", args=[an.pk]))
        self.assertFalse(Beleg.objects.filter(pk=ziel.pk).exists())
        an.refresh_from_db()
        self.assertTrue(an.vorlaeufig and not an.abgeschlossen and an.nummer is None)
        self.assertEqual((an.bemerkung, an.vortext), ("intern", "Hallo"))
        self.assertEqual(an.vorgang.weg, Vorgangsweg.NEU)
        self.assertGreater(an.zuletzt_aktiv, timezone.now() - timedelta(minutes=5))  # neue Frist
        self.assertEqual((self.positionen(an), letzte(self.m, Belegart.ANGEBOT), letzte(self.m, Belegart.RECHNUNG)), vorher)
        self.assertFalse(BelegUebernahme.objects.exists())
        # das Angebot lässt sich danach selbst speichern oder verwerfen
        self.assertIsNotNone(vorgang.beleg_verwerfen(an))

    def test_ziel_verwerfen_per_view_json(self):
        an = self.angebot()
        ziel, _ = self.kopie(an, Belegart.AUFTRAG)
        r = self.client.post(reverse("verkauf:beleg_verwerfen", args=[ziel.pk]), HTTP_X_WWOK_STAND="1")
        self.assertEqual(r.json(), {"ok": True, "weiter": reverse("verkauf:beleg", args=[an.pk])})
        self.assertTrue(Beleg.objects.get(pk=an.pk).vorlaeufig)

    def test_angebot_verwerfen_waehrend_ziel_offen_ist_ein_hindernis(self):
        an = self.angebot()
        ziel, _ = self.kopie(an)
        with self.assertRaises(ValueError) as fehler:
            vorgang.beleg_verwerfen(an)
        self.assertIn("erst diesen speichern oder verwerfen", str(fehler.exception))
        r = self.client.post(reverse("verkauf:beleg_verwerfen", args=[an.pk]), HTTP_X_WWOK_STAND="1")
        self.assertEqual(r.status_code, 409)
        r = self.client.post(reverse("verkauf:beleg_loeschen", args=[an.pk]), follow=True)
        self.assertTrue(any("erst diesen speichern oder verwerfen" in str(m) for m in r.context["messages"]))
        self.assertTrue(Beleg.objects.filter(pk=an.pk).exists())
        vorgang.beleg_verwerfen(ziel)
        self.assertIsNotNone(vorgang.beleg_verwerfen(an))

    def test_angebot_bearbeiten_und_selbst_speichern_bleibt_erlaubt(self):
        an = self.angebot()
        ziel, _ = self.kopie(an)
        r = self.client.get(reverse("verkauf:beleg", args=[an.pk]))
        html = r.content.decode()
        self.assertIn("zielbalken", html)
        self.assertIn(reverse("verkauf:beleg", args=[ziel.pk]), html)
        self.assertIn('id="abschlussform"', html)
        self.assertNotIn("wert: 'verwerfen'", html)  # der Verlassen-Dialog bietet kein Verwerfen an, das scheitern würde
        self.assertNotIn('id="verwerfenform"', html)
        self.pos(an, self.ram, menge="1", preis="5")
        an.berechne()
        an.abschliessen()
        self.assertTrue(Beleg.objects.get(pk=an.pk).abgeschlossen)

    def test_bereinigung_verwirft_das_ziel_das_angebot_bleibt(self):
        an = self.angebot()
        ziel, _ = self.kopie(an)
        self.aktiv_alt(an)
        self.aktiv_alt(ziel)
        liste = Beleg.vorlaeufige_verwerfen()
        self.assertEqual([(e["beleg"].pk, e["verworfen"], e["fehler"]) for e in liste], [(ziel.pk, True, "")])
        self.assertFalse(Beleg.objects.filter(pk=ziel.pk).exists())
        an.refresh_from_db()
        self.assertTrue(an.vorlaeufig and an.nummer is None and an.vorgang.weg == Vorgangsweg.NEU)
        self.assertEqual(Beleg.vorlaeufige_verwerfen(), [])  # das Angebot hat eine neue Frist
        self.aktiv_alt(an)
        self.assertEqual([e["beleg"].pk for e in Beleg.vorlaeufige_verwerfen()], [an.pk])

    def test_bereinigung_laesst_ein_angebot_mit_aktivem_ziel_stehen(self):
        an = self.angebot()
        self.kopie(an)
        self.aktiv_alt(an)
        self.assertEqual(Beleg.vorlaeufige_verwerfen(), [])
        self.assertTrue(Beleg.objects.filter(pk=an.pk).exists())


class UmwandlungTest(AngebotBasis):
    def umgewandelt(self):
        an = self.angebot()
        stand = (self.positionen(an), an.belegdatum, letzte(self.m, Belegart.ANGEBOT), letzte(self.m, Belegart.AUFTRAG))
        self.client.post(reverse("verkauf:beleg_folgebeleg", args=[an.pk]), {"von_art": "AN"})
        return Beleg.objects.get(pk=an.pk), stand

    def test_umwandlung_verwerfen_gibt_das_vorlaeufige_angebot_zurueck(self):
        ab, stand = self.umgewandelt()
        self.assertEqual((ab.art, ab.vorlaeufig, ab.nummer), (Belegart.AUFTRAG, True, None))
        self.assertEqual(ab.vorgang.weg, Vorgangsweg.UMWANDLUNG)
        self.aktiv_alt(ab)
        ergebnis = vorgang.beleg_verwerfen(ab)
        self.assertEqual(ergebnis["weiter"], reverse("verkauf:beleg", args=[ab.pk]))
        an = Beleg.objects.get(pk=ab.pk)
        self.assertEqual((an.art, an.vorlaeufig, an.abgeschlossen, an.nummer), (Belegart.ANGEBOT, True, False, None))
        self.assertEqual(an.vorgang.weg, Vorgangsweg.NEU)
        self.assertGreater(an.zuletzt_aktiv, timezone.now() - timedelta(minutes=5))
        self.assertFalse(BelegUmwandlung.objects.filter(beleg=an).exists())
        self.assertEqual((self.positionen(an), an.belegdatum, letzte(self.m, Belegart.ANGEBOT), letzte(self.m, Belegart.AUFTRAG)), stand)
        self.assertIsNotNone(vorgang.beleg_verwerfen(an))  # und es lässt sich selbst verwerfen
        self.assertFalse(Beleg.objects.filter(pk=ab.pk).exists())

    def test_umwandlung_abschliessen_gibt_dem_angebot_im_verlauf_seine_nummer(self):
        ab, _ = self.umgewandelt()
        zaehler = letzte(self.m, Belegart.ANGEBOT)
        ab.abschliessen()
        ab.refresh_from_db()
        self.assertTrue(ab.nummer.startswith("AB") and ab.abgeschlossen and not ab.vorlaeufig)
        u = ab.umwandlungen.get()
        self.assertEqual(u.von_art, Belegart.ANGEBOT)
        self.assertTrue(u.von_nummer.startswith("AN"))
        self.assertEqual(letzte(self.m, Belegart.ANGEBOT), zaehler + 1)
        self.assertFalse(BelegVorgang.objects.filter(beleg=ab).exists())
        html = self.client.get(reverse("verkauf:beleg", args=[ab.pk])).content.decode()
        self.assertIn(u.von_nummer, html)  # Verlauf „Angebot AN… → Bestellung“

    def test_umwandlung_aus_dem_angebot_ist_ohne_hindernis_verwerfbar(self):
        ab, _ = self.umgewandelt()
        self.assertIsNone(vorgang.verwerfhindernis(ab, BelegVorgang.objects.get(beleg=ab)))
