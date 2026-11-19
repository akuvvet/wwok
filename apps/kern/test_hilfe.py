"""Hilfe der Schnellbedienung (Strg+K): Einträge, Suche und Darstellung in der Palette."""
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from apps.kern import hilfe
from apps.kern.hilfe import HILFE, hilfe_suchen, ist_frage, woerter
from apps.kern.models import Mandant
from apps.stammdaten.models import Kunde


class HilfeEintraegeTest(SimpleTestCase):
    """Die Daten selbst: vollständig, und jedes Sprungziel löst sich noch auf (so veralten Links nicht unbemerkt)."""

    def test_umfang(self):
        self.assertGreaterEqual(len(HILFE), 50)

    def test_kennungen_und_fragen_eindeutig(self):
        self.assertEqual(len({h.kennung for h in HILFE}), len(HILFE))
        self.assertEqual(len({h.frage for h in HILFE}), len(HILFE))

    def test_jeder_eintrag_ist_vollstaendig(self):
        for h in HILFE:
            with self.subTest(h.kennung):
                self.assertTrue(h.frage.strip())
                self.assertTrue(h.frage.endswith("?"), "Fragen enden mit ?")
                self.assertTrue(h.stichworte, "Stichworte fehlen")
                self.assertTrue(h.wege, "mindestens ein Weg")
                self.assertTrue(h.ziel.strip())
                for weg in h.wege:
                    self.assertTrue(weg.titel.strip())
                    self.assertTrue(weg.schritte, "ein Weg braucht Schritte")
                    for schritt in weg.schritte:
                        self.assertTrue(schritt.strip())
                        self.assertNotIn(" → ", schritt)

    def test_jedes_sprungziel_loest_sich_auf(self):
        for h in HILFE:
            with self.subTest(h.kennung):
                url = h.url()
                self.assertTrue(url.startswith("/") and not url.startswith("//"))
                self.assertEqual(url.split("?")[0], reverse(h.url_name, args=h.args))

    def test_wege_nennen_keine_veralteten_oder_privaten_dinge(self):
        text = " ".join(w.hinweis + " ".join(w.schritte) for h in HILFE for w in h.wege)
        self.assertNotIn("Menü Lager", text)  # das Menü heißt „Artikel“ (früher „Lager“)


class HilfeSucheTest(SimpleTestCase):
    def erster(self, text):
        treffer = hilfe_suchen(text)
        self.assertTrue(treffer, f"kein Treffer für {text!r}")
        return treffer[0].kennung

    def test_beispielfragen_liefern_den_passenden_eintrag_zuerst(self):
        for text, kennung in [
            ("Wie buche ich eine Kundenrechnung", "rechnung"),
            ("Wie buche ich eine Kundenrechnung?", "rechnung"),
            ("Wie buche ich eine Lieferantenrechnung", "lieferantenrechnung"),
            ("Wie erstelle ich eine Mahnung", "mahnung"),
            ("gutschrift ohne rechnung", "freie_gutschrift"),
            ("skonto", "skonto"),
        ]:
            with self.subTest(text):
                self.assertEqual(self.erster(text), kennung)

    def test_jede_frage_findet_sich_selbst_zuerst(self):
        for h in HILFE:
            with self.subTest(h.kennung):
                self.assertEqual(self.erster(h.frage), h.kennung)

    def test_umgangssprache_und_tippvarianten(self):
        for text, kennung in [
            ("rechnung buchen", "rechnung"),
            ("zahlung eingang", "zahlung_kunde"),
            ("bezahlt markieren", "zahlung_kunde"),
            ("mahnen", "mahnung"),
            ("kulanz", "freie_gutschrift"),
            ("vorkasse", "proforma"),
            ("abo", "abo"),
            ("datev", "datev"),
            ("inventur", "inventur"),
            ("seriennummer", "seriennummern"),
            ("passwort vergessen", "passwort"),
        ]:
            with self.subTest(text):
                self.assertEqual(self.erster(text), kennung)
        self.assertIn("gutschrift_zur_rechnung", [h.kennung for h in hilfe_suchen("storno")])

    def test_gross_klein_und_umlaute(self):
        self.assertEqual(self.erster("ÜBERZAHLUNG"), self.erster("ueberzahlung"))
        self.assertEqual(self.erster("Überzahlung"), "ueberzahlung")
        self.assertEqual(self.erster("lieferantenrechnung BEZAHLEN"), "lieferantenrechnung_bezahlen")
        self.assertEqual(woerter("Größe Äpfel ß"), ["groesse", "aepfel", "ss"])

    def test_stoppwoerter_zaehlen_nicht(self):
        self.assertEqual(self.erster("ich die der das eine wie skonto"), "skonto")
        self.assertEqual(hilfe_suchen("wie ich eine der die das"), [])

    def test_unsinn_und_sonderzeichen_ergeben_nichts(self):
        for text in ("xyzzyq", "%", "a_", "'\"; --", "", "   "):
            with self.subTest(text):
                self.assertEqual(hilfe_suchen(text), [])

    def test_hoechstens_fuenf_treffer(self):
        self.assertLessEqual(len(hilfe_suchen("rechnung")), 5)
        self.assertLessEqual(len(hilfe_suchen("rechnung", maximal=2)), 2)

    def test_frage_erkennen(self):
        for text in ("Wie buche ich eine Rechnung", "wo finde ich das", "Was ist ein Abo", "wann mahnen", "kann ich stornieren",
                     "warum keine Nummer", "Rechnung buchen?", "  skonto ? "):
            with self.subTest(text):
                self.assertTrue(ist_frage(text))
        for text in ("rechnung buchen", "skonto", "Müller", "10001", ""):
            with self.subTest(text):
                self.assertFalse(ist_frage(text))

    def test_treffer_darstellung(self):
        eintrag = next(h for h in HILFE if h.kennung == "mahnung")
        treffer = hilfe.als_treffer(eintrag)
        self.assertEqual(treffer["gruppe"], "Hilfe")
        self.assertEqual(treffer["titel"], eintrag.frage)
        self.assertEqual(treffer["url"], reverse("finanzen:mahnwesen"))
        self.assertEqual(len(treffer["hilfe"]["wege"]), len(eintrag.wege))
        self.assertEqual(treffer["hilfe"]["wege"][0]["schritte"][0], "Finanzen")


@override_settings(SECURE_SSL_REDIRECT=False)
class HilfePaletteTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.user = get_user_model().objects.create_user("anna", password="x" * 14)
        self.client.force_login(self.user)
        sitzung = self.client.session
        sitzung["mandant_id"] = self.m.pk
        sitzung.save()

    def antwort(self, q):
        antwort = self.client.get(reverse("schnellsuche"), {"q": q})
        self.assertEqual(antwort.status_code, 200)
        return antwort.json()

    def test_antwort_enthaelt_wege_und_sprungadresse(self):
        daten = self.antwort("Wie erstelle ich eine Mahnung?")
        self.assertTrue(daten["frage"])
        erster = daten["treffer"][0]
        self.assertEqual(erster["gruppe"], "Hilfe")
        self.assertEqual(erster["titel"], "Wie erstelle ich eine Mahnung?")
        self.assertEqual(erster["url"], reverse("finanzen:mahnwesen"))
        wege = erster["hilfe"]["wege"]
        self.assertTrue(wege)
        self.assertEqual(wege[0]["schritte"][:3], ["Finanzen", "Weiteres", "Mahnwesen"])
        self.assertIn("Ausgewählte Mahnungen erstellen", wege[0]["schritte"][-1])

    def test_kundenrechnung_weg_nennt_die_echten_knoepfe(self):
        erster = self.antwort("Wie buche ich eine Kundenrechnung?")["treffer"][0]
        text = " ".join(" ".join(w["schritte"]) for w in erster["hilfe"]["wege"])
        self.assertIn("Als Rechnung übernehmen", text)
        self.assertIn("Speichern: abschließen & Nummer vergeben", text)

    def test_hilfe_steht_bei_fragen_oben(self):
        Kunde.objects.create(nummer="10001", name1="Wie rechnung buchen GmbH", ort="Köln")
        daten = self.antwort("wie rechnung buchen")
        gruppen = [t["gruppe"] for t in daten["treffer"]]
        self.assertTrue(daten["frage"])
        self.assertEqual(gruppen[0], "Hilfe")
        self.assertIn("Kunde", gruppen)
        self.assertLess(gruppen.index("Hilfe"), gruppen.index("Kunde"))

    def test_hilfe_steht_ohne_frage_nach_den_treffern(self):
        Kunde.objects.create(nummer="10001", name1="Rechnung buchen GmbH", ort="Köln")
        daten = self.antwort("rechnung buchen")
        gruppen = [t["gruppe"] for t in daten["treffer"]]
        self.assertFalse(daten["frage"])
        self.assertEqual(gruppen[0], "Kunde")
        self.assertEqual(gruppen[-1], "Hilfe")

    def test_hoechstens_fuenf_hilfetreffer(self):
        treffer = self.antwort("rechnung")["treffer"]
        self.assertLessEqual(len([t for t in treffer if t["gruppe"] == "Hilfe"]), 5)

    def test_ohne_passende_hilfe_bleibt_die_suche_wie_sie_war(self):
        Kunde.objects.create(nummer="10001", name1="Müller Computer GmbH", ort="Hamburg")
        gruppen = {t["gruppe"] for t in self.antwort("müller")["treffer"]}
        self.assertEqual(gruppen, {"Kunde"})

    def test_hilfe_nur_nach_anmeldung(self):
        self.client.logout()
        antwort = self.client.get(reverse("schnellsuche"), {"q": "Wie erstelle ich eine Mahnung?"})
        self.assertEqual(antwort.status_code, 302)

    def test_palette_kann_die_antwort_darstellen(self):
        html = self.client.get(reverse("dashboard"), follow=True).content.decode()
        for baustein in ('id="palette-antwort"', "Hingehen", "antwortZeigen", "antwortSchliessen", "palette-esc-text"):
            self.assertIn(baustein, html)
