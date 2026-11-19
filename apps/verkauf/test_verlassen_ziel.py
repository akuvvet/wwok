"""Wunsch des Nutzers 19.11.2026: Wer einen nicht gespeicherten Beleg über ein Menü verlässt und „Abschließen“ oder „Verwerfen“
wählt, landet danach beim angeklickten Menüpunkt. Nur Adressen dieser Anwendung (kein offener Redirect)."""
from decimal import Decimal as D

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.einkauf import services as einkauf
from apps.kern.models import Mandant
from apps.stammdaten.models import Artikel, Kunde, Lieferant, Steuersatz

from .models import Beleg, Belegart, Position


@override_settings(SECURE_SSL_REDIRECT=False)
class VerlassenZielTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.kunde = Kunde.objects.create(nummer="1", name1="Muster GmbH")
        self.st = Steuersatz.objects.get(nummer=1)
        self.client.force_login(get_user_model().objects.create_superuser("chef", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    def angebot(self):
        b = Beleg.objects.create(mandant=self.m, art=Belegart.ANGEBOT, kunde=self.kunde, vorlaeufig=True)
        Position.objects.create(beleg=b, bezeichnung="Dienst", einzelpreis=D("10"))
        return b

    def test_abschliessen_fuehrt_zum_ziel(self):
        b = self.angebot()
        r = self.client.post(reverse("verkauf:beleg_abschliessen", args=[b.pk]), {"weiter": "http://testserver/einkauf/"})
        self.assertRedirects(r, "/einkauf/", fetch_redirect_response=False)
        b.refresh_from_db()
        self.assertTrue(b.abgeschlossen and b.nummer)

    def test_fremdes_ziel_wird_ignoriert(self):
        b = self.angebot()
        r = self.client.post(reverse("verkauf:beleg_abschliessen", args=[b.pk]), {"weiter": "https://boese.example/klau"})
        self.assertRedirects(r, reverse("verkauf:kunde", args=[self.kunde.pk]), fetch_redirect_response=False)

    def test_ohne_ziel_wie_bisher(self):
        b = self.angebot()
        r = self.client.post(reverse("verkauf:beleg_abschliessen", args=[b.pk]))
        self.assertRedirects(r, reverse("verkauf:kunde", args=[self.kunde.pk]), fetch_redirect_response=False)

    def test_belegmaske_reicht_das_ziel_weiter(self):
        from .vorgang import vorgang_beginnen
        b = self.angebot()
        vorgang_beginnen(b, "neu")
        html = self.client.get(reverse("verkauf:beleg", args=[b.pk])).content.decode()
        self.assertIn('<input type="hidden" name="weiter" value="">', html)
        self.assertIn("verwerfen(ziel)", html)
        self.assertIn("location.href = ziel || d.weiter", html)

    def test_lieferantenbestellung_abschliessen_fuehrt_zum_ziel(self):
        lf = Lieferant.objects.create(nummer="L1", name1="Lieferant")
        b = einkauf.bestellung_anlegen(self.m, lf, vorlaeufig=True)
        a = Artikel.objects.create(nummer="A1", bezeichnung="Maus", steuersatz=self.st)
        einkauf.bestellposition(b, a, D("1"), ek=D("5"))
        r = self.client.post(reverse("einkauf:bestellung_abschliessen", args=[b.pk]), {"weiter": "/verkauf/"})
        self.assertRedirects(r, "/verkauf/", fetch_redirect_response=False)
        b.refresh_from_db()
        self.assertTrue(b.nummer and not b.vorlaeufig)
        html = self.client.get(reverse("einkauf:bestellung", args=[b.pk])).content.decode()
        self.assertNotIn("verwerfen(ziel)", html)  # abgeschlossen: kein Verlassen-Skript mehr
