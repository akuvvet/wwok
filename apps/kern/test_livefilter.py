"""Gemeinsamer Live-Filter (base.html) und „Filter zurücksetzen“ in den Listen."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.kern.models import Mandant
from apps.stammdaten.models import Kunde

LISTEN = [
    ("lager:uebersicht", {}),
    ("werkstatt:liste", {}),
    ("einkauf:lieferanten", {}),
    ("einkauf:preislistenartikel", {}),
    ("lager:seriennummern", {}),
    ("verkauf:start", {}),
    ("verkauf:belege", {}),
]


class LiveFilterTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.kunde = Kunde.objects.create(nummer="1000", name1="Muster GmbH", ort="Solingen")
        self.client.force_login(get_user_model().objects.create_user("t", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    def test_listen_haengen_am_gemeinsamen_baustein(self):
        urls = [reverse(n, kwargs=k) for n, k in LISTEN] + [reverse("verkauf:kunde", args=[self.kunde.pk])]
        for url in urls:
            html = self.client.get(url).content.decode()
            self.assertIn("<form method=\"get\" class=\"zeile\" data-livefilter>", html, url)
            self.assertIn(f'data-ergebnis data-mandant="{self.m.pk}"', html, url)
            self.assertEqual(html.count("form[data-livefilter]"), 1, url)  # genau ein Skript, aus base.html
            self.assertIn("<noscript><button", html, url)  # ohne JavaScript bleibt der Knopf

    def test_filter_zuruecksetzen_bei_leerer_gefilterter_liste(self):
        for name in ["lager:uebersicht", "werkstatt:liste", "einkauf:lieferanten", "einkauf:preislistenartikel",
                     "lager:seriennummern", "verkauf:start", "verkauf:belege"]:
            url = reverse(name)
            html = self.client.get(url, {"q": "gibtesnicht-xyz"}).content.decode()
            self.assertIn("Keine Treffer für die aktuelle Suche", html, name)
            self.assertIn(f'<a href="{url}">Filter zurücksetzen</a>', html, name)

    def test_ohne_filter_kein_zuruecksetzen(self):
        html = self.client.get(reverse("lager:uebersicht")).content.decode()
        self.assertNotIn("Filter zurücksetzen", html)
        self.assertIn("Keine Artikel.", html)

    def test_kundenansicht_belegfilter(self):
        url = reverse("verkauf:kunde", args=[self.kunde.pk])
        html = self.client.get(url, {"art": "RE"}).content.decode()
        self.assertIn("Filter zurücksetzen", html)
        self.assertNotIn("onchange=", html)  # Auswahl wirkt über das gemeinsame Skript
        self.assertIn("Keine Belege.", self.client.get(url).content.decode())
