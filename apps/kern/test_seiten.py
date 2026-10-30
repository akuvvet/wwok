from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import URLPattern, URLResolver, get_resolver, reverse

from .models import Mandant

AUSLASSEN = {"logout", "login", "password_change", "password_change_done", "password_reset", "password_reset_done",
             "password_reset_complete"}


def parameterlose_urls(muster=None, praefix="", namensraum=""):
    """Alle URL-Namen ohne Pfadparameter (Verwaltung, Anmeldung und Abmeldung ausgenommen)."""
    ergebnis = []
    for eintrag in (muster if muster is not None else get_resolver().url_patterns):
        if isinstance(eintrag, URLResolver):
            ns = f"{namensraum}{eintrag.namespace}:" if eintrag.namespace else namensraum
            if eintrag.namespace == "admin" or "konto" in str(eintrag.pattern):
                continue
            ergebnis += parameterlose_urls(eintrag.url_patterns, praefix + str(eintrag.pattern), ns)
        elif isinstance(eintrag, URLPattern) and eintrag.name and "<" not in str(eintrag.pattern) \
                and "<" not in praefix and eintrag.name not in AUSLASSEN:
            ergebnis.append(namensraum + eintrag.name)
    return ergebnis


@override_settings(SECURE_SSL_REDIRECT=False)
class SeitenTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.user = get_user_model().objects.create_superuser("a", "a@a.de", "x" * 14)
        self.client.force_login(self.user)

    def mit_mandant(self):
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    def test_alle_listen_seiten_liefern_nie_500(self):
        namen = parameterlose_urls()
        self.assertGreater(len(namen), 20)
        self.mit_mandant()
        for name in namen:
            antwort = self.client.get(reverse(name))
            self.assertLess(antwort.status_code, 500, f"{name} -> {antwort.status_code}")

    def test_ohne_mandant_nie_500(self):
        for name in parameterlose_urls():
            antwort = self.client.get(reverse(name))
            self.assertLess(antwort.status_code, 500, f"{name} -> {antwort.status_code}")

    def test_ohne_login_wird_zur_anmeldung_geleitet(self):
        self.client.logout()
        for name in parameterlose_urls():
            antwort = self.client.get(reverse(name))
            self.assertIn(antwort.status_code, (302, 405), f"{name} ohne Login -> {antwort.status_code}")
            if antwort.status_code == 302:
                self.assertIn("login", antwort["Location"], name)

    def test_menues_und_zuordnung_der_bereiche(self):
        self.mit_mandant()
        verkauf = self.client.get(reverse("verkauf:belege")).content.decode()
        for text in ("<span>Kunde</span>", "Alle Belege", "Wiederkehrende Rechnungen", "Shops &amp; Online-Bestellungen"):
            self.assertIn(text, verkauf)
        lager = self.client.get(reverse("lager:uebersicht")).content.decode()
        for text in ("Artikel &amp; Bestand", "Inventur / Zählliste", "Seriennummern", "ESD-Keys"):
            self.assertIn(text, lager)
        # ESD-Keys und Shop lassen den jeweiligen Hauptbereich markiert und zeigen dessen Menü
        self.assertEqual(self.client.get(reverse("esd:uebersicht")).context["hauptbereich"], "lager")
        self.assertEqual(self.client.get(reverse("shop:uebersicht")).context["hauptbereich"], "verkauf")
        self.assertIn("Alle Belege", self.client.get(reverse("shop:uebersicht")).content.decode())
        self.assertIn("Offene Posten", self.client.get(reverse("finanzen:offene_posten")).content.decode())
        self.assertIn("Bestellvorschlag", self.client.get(reverse("einkauf:vorschlag")).content.decode())
