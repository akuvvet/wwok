"""Flächenfarben je Mandant (Verwaltung → Mandanten → Aussehen): Kopfbereich, Seitenleiste und Karten getrennt oder in gleicher Tonart."""
from apps.kern.testhilfe import MANDANT_INLINES_LEER
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from . import layout
from .models import Mandant


class FarbrechnungTest(SimpleTestCase):
    def test_dunkle_flaeche_bekommt_helle_schrift_und_umgekehrt(self):
        self.assertEqual(layout.bereich("#102a43", "#0b5cad")["text"], layout.TEXT_HELL)
        self.assertEqual(layout.bereich("#fff3c4", "#0b5cad")["text"], layout.TEXT_DUNKEL)

    def test_akzent_bleibt_auf_jeder_flaeche_lesbar(self):
        for flaeche in ("#0b5cad", "#102a43", "#fff3c4", "#15803d", "#808080", "#000000", "#ffffff"):
            b = layout.bereich(flaeche, "#0b5cad")
            self.assertGreaterEqual(layout.kontrast(b["akzent"], flaeche), 3, flaeche)
            self.assertGreaterEqual(layout.kontrast(b["text"], flaeche), 4.5, flaeche)
            self.assertGreaterEqual(layout.kontrast(b["knopftext"], b["akzent"]), 3, flaeche)

    def test_dunkelvariante_ist_dunkel(self):
        self.assertLess(layout.helligkeit(layout.dunkelvariante("#fff3c4")), 0.2)
        self.assertEqual(layout.dunkelvariante("#0a0a0a"), "#0a0a0a")


@override_settings(SECURE_SSL_REDIRECT=False)
class FlaechenfarbenTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="ZONE", firmenname="ZONE Handel", farbe="#15803d")
        self.client.force_login(get_user_model().objects.create_superuser("chef", "c@example.test", "x" * 14))
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    def test_standard_bleibt_neutral(self):
        self.assertEqual(self.m.flaechenmodus, "standard")
        self.assertEqual(self.m.flaechen_css, "")
        self.assertNotContains(self.client.get(reverse("dashboard")), "data-flaechenfarben")

    def test_eigene_farben_je_bereich_getrennt(self):
        Mandant.objects.filter(pk=self.m.pk).update(flaechenmodus="eigen", farbe_kopf="#102a43", farbe_leiste="#fff3c4")
        r = self.client.get(reverse("dashboard"))
        self.assertContains(r, ".kopfbereich header{--karte:#102a43;--text:#ffffff")
        self.assertContains(r, ".seitenleiste{--karte:#fff3c4;--text:#171b22")
        self.assertNotContains(r, ".karte{--karte")  # Karten leer gelassen = neutral

    def test_eigene_farben_wirken_nur_im_modus_eigen(self):
        Mandant.objects.filter(pk=self.m.pk).update(farbe_kopf="#102a43")
        self.assertNotContains(self.client.get(reverse("dashboard")), "data-flaechenfarben")

    def test_gleiche_tonart_leitet_alle_drei_aus_der_layoutfarbe_ab(self):
        Mandant.objects.filter(pk=self.m.pk).update(flaechenmodus="ton")
        r = self.client.get(reverse("dashboard"))
        self.assertContains(r, ".kopfbereich header{--karte:#15803d")
        self.assertContains(r, ".seitenleiste{--karte:")
        self.assertContains(r, ".karte{--karte:")
        self.assertContains(r, "@media (prefers-color-scheme: dark){")

    def test_gleiche_tonart_ohne_layoutfarbe_nimmt_standardblau(self):
        Mandant.objects.filter(pk=self.m.pk).update(flaechenmodus="ton", farbe="")
        self.assertContains(self.client.get(reverse("dashboard")), ".kopfbereich header{--karte:#0b5cad")

    def test_verwaltung_uebernimmt_die_kopffarbe(self):
        Mandant.objects.filter(pk=self.m.pk).update(flaechenmodus="eigen", farbe_kopf="#102a43")
        self.assertContains(self.client.get(reverse("admin:index")), "--header-bg:#102a43")

    def test_ungueltiger_wert_wird_abgelehnt_und_nie_ausgegeben(self):
        self.m.farbe_kopf = "red;}body{display:none"[:7]
        with self.assertRaises(ValidationError):
            self.m.full_clean()
        Mandant.objects.filter(pk=self.m.pk).update(flaechenmodus="eigen", farbe_kopf="</styl")
        self.assertNotContains(self.client.get(reverse("dashboard")), "data-flaechenfarben")

    def test_zuruecksetzen_setzt_hausfarbe_und_neutrale_flaechen(self):
        okay = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer", farbe="#ff0000", flaechenmodus="ton")
        fremd = Mandant.objects.create(kurzname="TEST", firmenname="Test", farbe="#ff0000", flaechenmodus="ton")
        Mandant.objects.filter(pk=self.m.pk).update(flaechenmodus="eigen", farbe_kopf="#102a43", farbe_karte="#f5f0ff")
        url = reverse("admin:kern_mandant_change", args=[self.m.pk])
        self.assertContains(self.client.get(url), 'name="_aussehen_zuruecksetzen"')
        r = self.client.post(url, {"_aussehen_zuruecksetzen": "1"})
        self.assertRedirects(r, url)
        self.m.refresh_from_db()
        self.assertEqual((self.m.farbe, self.m.flaechenmodus, self.m.farbe_kopf, self.m.farbe_leiste, self.m.farbe_karte),
                         ("#83bb2a", "standard", "", "", ""))
        # Sammelaktion in der Liste: OKAY bekommt sein Blau, andere Mandanten das Standardblau (leer)
        self.client.post(reverse("admin:kern_mandant_changelist"),
                         {"action": "aussehen_zuruecksetzen", "_selected_action": [okay.pk, fremd.pk]})
        okay.refresh_from_db()
        fremd.refresh_from_db()
        self.assertEqual((okay.farbe, okay.flaechenmodus), ("#2b72b4", "standard"))
        self.assertEqual((fremd.farbe, fremd.flaechenmodus), ("", "standard"))

    def test_zuruecksetzen_nur_mit_aenderungsrecht(self):
        self.client.force_login(get_user_model().objects.create_user("gast", password="x" * 14, is_staff=True))
        Mandant.objects.filter(pk=self.m.pk).update(flaechenmodus="ton")
        r = self.client.post(reverse("admin:kern_mandant_change", args=[self.m.pk]), {"_aussehen_zuruecksetzen": "1"})
        self.assertEqual(r.status_code, 403)
        self.m.refresh_from_db()
        self.assertEqual(self.m.flaechenmodus, "ton")

    def test_verwaltung_speichert_die_auswahl(self):
        url = reverse("admin:kern_mandant_change", args=[self.m.pk])
        r = self.client.get(url)
        self.assertContains(r, 'name="flaechenmodus"')
        self.assertContains(r, 'name="farbe_kopf"')
        self.assertContains(r, "Leeren")
        daten = {k: ("" if v is None else v) for k, v in r.context["adminform"].form.initial.items() if k != "logo"}
        daten.update(flaechenmodus="eigen", farbe_kopf="#102a43", farbe_leiste="", farbe_karte="#f5f0ff", **MANDANT_INLINES_LEER)
        r = self.client.post(url, daten)
        self.assertEqual(r.status_code, 302, getattr(r, "context", None) and r.context["adminform"].form.errors)
        self.m.refresh_from_db()
        self.assertEqual((self.m.flaechenmodus, self.m.farbe_kopf, self.m.farbe_leiste, self.m.farbe_karte),
                         ("eigen", "#102a43", "", "#f5f0ff"))
