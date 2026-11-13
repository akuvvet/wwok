"""Pfad im Kopfbereich: Bereich › Menüpunkt, die aktuelle Seite hängt das Skript aus dem Seitentitel an."""
from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase, override_settings
from django.urls import resolve, reverse

from .kontext import PFAD, seitenpfad
from .models import Mandant


def pfad_fuer(pfad, benutzer):
    anfrage = RequestFactory().get(pfad)
    anfrage.resolver_match, anfrage.user = resolve(pfad), benutzer
    return seitenpfad(anfrage).get("seitenpfad")


@override_settings(SECURE_SSL_REDIRECT=False)
class SeitenpfadTest(TestCase):
    def setUp(self):
        Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.user = get_user_model().objects.create_superuser("a", "a@a.de", "x" * 14)

    def test_listenseite_ohne_eigenen_link(self):
        self.assertEqual(pfad_fuer(reverse("einkauf:bestellungen"), self.user), [("Einkauf", None), ("Bestellungen", None)])

    def test_detailseite_verlinkt_den_menuepunkt(self):
        self.assertEqual(pfad_fuer(reverse("einkauf:bestellung", args=[1]), self.user),
                         [("Einkauf", None), ("Bestellungen", reverse("einkauf:bestellungen"))])

    def test_spezielle_vor_allgemeinen_treffern(self):
        self.assertEqual(pfad_fuer(reverse("einkauf:preislistenartikel"), self.user)[1][0], "Preislistenartikel")
        self.assertEqual(pfad_fuer(reverse("einkauf:preislisten"), self.user)[1][0], "Preislisten & EK-Import")
        self.assertEqual(pfad_fuer(reverse("lager:inventur_liste"), self.user)[1][0], "Inventur / Zählliste")
        self.assertEqual(pfad_fuer(reverse("verkauf:abos"), self.user)[1][0], "Wiederkehrende Rechnungen")

    def test_shop_und_esd_unter_ihrem_bereich(self):
        self.assertEqual(pfad_fuer(reverse("shop:uebersicht"), self.user)[0][0], "Verkauf")
        self.assertEqual(pfad_fuer(reverse("esd:uebersicht"), self.user)[0][0], "Artikel")

    def test_dashboard(self):
        self.assertEqual(pfad_fuer(reverse("dashboard"), self.user), [("Dashboard", None)])

    def test_alle_menueziele_gibt_es(self):
        for bereich, punkte in PFAD.values():
            for text, ziel, muster in punkte:
                self.assertTrue(reverse(ziel), text)

    def test_ohne_anmeldung_kein_pfad(self):
        from django.contrib.auth.models import AnonymousUser
        self.assertIsNone(pfad_fuer(reverse("einkauf:bestellungen"), AnonymousUser()))

    def test_im_kopfbereich_sichtbar(self):
        self.client.force_login(self.user)
        r = self.client.get(reverse("einkauf:bestellungen"))
        self.assertContains(r, '<nav class="seitenpfad" aria-label="Pfad">')
        self.assertContains(r, '<span aria-current="page">Bestellungen</span>')
