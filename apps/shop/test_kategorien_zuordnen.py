"""Shops → „Kategorien zuordnen“: Vergleich der Online-Kategorien mit dem Shop und Auswahl bei mehrfach vorhandenen."""
from decimal import Decimal as D
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.kern.models import Mandant
from apps.stammdaten.models import Artikel, Kategorie, Steuersatz

from . import services
from .models import Shop, ShopKategorie
from .shopware import ShopFehler
from .tests import FakeClient

DOPPELT = [{"id": "b1", "name": "Bildschirme", "parentId": "wurzel"},
           {"id": "b2", "name": "bildschirme", "parentId": "wurzel", "active": False},
           {"id": "zub", "name": "Zubehör", "parentId": "wurzel"}]


@override_settings(SECURE_SSL_REDIRECT=False)
class KategorienZuordnenTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.shop = Shop.objects.create(mandant=self.m, name="Webshop", url="https://shop.example.test", client_id="abc")
        self.ober = Kategorie.objects.create(nummer="10100", name="Bildschirme", online=True)
        self.unter = Kategorie.objects.create(nummer="10115", name="Curved", eltern=self.ober, online=True)
        self.zub = Kategorie.objects.create(name="Zubehör", online=True)
        self.neu = Kategorie.objects.create(name="Drucker", online=True)
        Kategorie.objects.create(name="Intern", online=False)
        st = Steuersatz.objects.create(bezeichnung="19", prozent=D("19"))
        self.a = Artikel.objects.create(nummer="A1", bezeichnung="Monitor", steuersatz=st, vk1=D("100"), online=True, kategorie=self.unter)

    def shop_c(self, kategorien=DOPPELT):
        c = FakeClient(self.shop, produkte={"A1": "p"})
        c.shop_kategorien = list(kategorien)
        return c

    def stand(self, c=None):
        return {str(z["kategorie"]): z for z in services.kategorien_stand(self.shop, c or self.shop_c())}

    def test_uebersicht_zeigt_vorhanden_fehlt_und_mehrfach_und_schreibt_nichts(self):
        c = self.shop_c()
        s = self.stand(c)
        self.assertEqual({k: z["stand"] for k, z in s.items()}, {
            "Bildschirme": "doppelt", "Bildschirme › Curved": "eltern", "Zubehör": "eindeutig", "Drucker": "fehlt"})  # „Intern“ ist offline
        self.assertEqual([(k["id"], k["inaktiv"]) for k in s["Bildschirme"]["kandidaten"]], [("b1", False), ("b2", True)])
        self.assertEqual(s["Zubehör"]["pfad"], "Zubehör")
        self.assertFalse(ShopKategorie.objects.exists())
        self.assertFalse(hasattr(c, "kategorien_geschrieben"))

    def test_unterkategorie_einer_fehlenden_oberkategorie_fehlt_ebenfalls(self):
        s = self.stand(self.shop_c([]))
        self.assertEqual((s["Bildschirme"]["stand"], s["Bildschirme › Curved"]["stand"]), ("fehlt", "fehlt"))

    def test_auswahl_gilt_und_die_unterkategorie_wird_darunter_angelegt(self):
        self.assertEqual(services.artikeldaten_senden(self.shop, self.shop_c())["kategorie_fehlt"], {"Bildschirme"})
        self.assertEqual(services.kategorie_zuordnen(self.shop, self.ober, " b1 ", self.shop_c()), "Bildschirme")
        z = ShopKategorie.objects.get()
        self.assertEqual((z.kategorie, z.kategorie_id_extern, z.angelegt), (self.ober, "b1", False))
        s = self.stand()
        self.assertEqual((s["Bildschirme"]["stand"], s["Bildschirme › Curved"]["stand"]), ("zugeordnet", "fehlt"))
        c = self.shop_c()
        erg = services.artikeldaten_senden(self.shop, c)
        self.assertEqual(erg["kategorie_fehlt"], set())
        self.assertEqual([(k["name"], k["parentId"]) for k in c.kategorien_geschrieben], [("Curved", "b1")])  # nur die Unterkategorie
        self.assertEqual(c.gesendet[0]["categories"], [{"id": ShopKategorie.objects.get(kategorie=self.unter).kategorie_id_extern}])

    def test_ungueltige_zuordnungen_werden_abgelehnt(self):
        for kategorie, ext, meldung in ((self.ober, "", "auswählen"), (self.ober, "gibtsnicht", "gibt es im Shop nicht"),
                                        (Kategorie.objects.get(name="Intern"), "b1", "nicht online")):
            with self.assertRaisesRegex(ShopFehler, meldung):
                services.kategorie_zuordnen(self.shop, kategorie, ext, self.shop_c())
        services.kategorie_zuordnen(self.shop, self.zub, "zub", self.shop_c())
        with self.assertRaisesRegex(ShopFehler, "schon „Zubehör“ zugeordnet"):
            services.kategorie_zuordnen(self.shop, self.neu, "zub", self.shop_c())
        self.assertEqual(ShopKategorie.objects.count(), 1)

    def test_seite_nur_fuer_admins_mit_auswahl_und_filter(self):
        url = reverse("shop:kategorien", args=[self.shop.pk])
        s = self.client.session
        self.client.force_login(get_user_model().objects.create_user("t", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()
        with mock.patch("apps.shop.services.ShopwareClient", lambda shop: self.shop_c()):
            self.assertEqual(self.client.get(url).status_code, 302)
            self.client.post(url, {"kategorie": self.ober.pk, "ext": "b1"})
            self.assertFalse(ShopKategorie.objects.exists())
            self.client.force_login(get_user_model().objects.create_superuser("chef", password="x" * 14))
            s = self.client.session
            s["mandant_id"] = self.m.pk
            s.save()
            self.assertContains(self.client.get(reverse("shop:uebersicht")), "Kategorien zuordnen")
            r = self.client.get(url)
            for text in ("10115", "im Shop mehrfach", "fehlt im Shop", "im Shop vorhanden", "Oberkategorie offen", "(inaktiv)",
                         "https://shop.example.test/admin#/sw/category/index/b2", "nur offene anzeigen (2)"):
                self.assertContains(r, text)
            self.assertNotContains(r, "Intern")
            self.assertNotContains(self.client.get(url + "?nur=offen"), "Drucker")
            r = self.client.post(url, {"kategorie": self.ober.pk, "ext": "", "ext_frei": "b2"}, follow=True)
            self.assertContains(r, "gehört jetzt zur Shop-Kategorie")
            self.assertEqual(ShopKategorie.objects.get().kategorie_id_extern, "b2")
            r = self.client.post(url, {"kategorie": self.zub.pk, "ext_frei": "b2"}, follow=True)
            self.assertContains(r, "ist schon „Bildschirme“ zugeordnet")

    def test_shop_nicht_erreichbar_zeigt_meldung(self):
        self.client.force_login(get_user_model().objects.create_superuser("chef", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()
        c = self.shop_c()
        c.kategorien = mock.Mock(side_effect=ShopFehler("Shop antwortet nicht"))
        with mock.patch("apps.shop.services.ShopwareClient", lambda shop: c):
            self.assertContains(self.client.get(reverse("shop:kategorien", args=[self.shop.pk])), "Shop antwortet nicht")
