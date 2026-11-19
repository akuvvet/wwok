"""Weitere Kategorien, Grundpreis und UVP im Shop (Entscheidung 19.11.2026): Der Artikel steht in allen seinen Online-Kategorien."""
from decimal import Decimal as D

from django.test import TestCase

from apps.kern.models import Mandant
from apps.stammdaten.models import Artikel, Kategorie, Steuersatz

from . import services
from .models import Shop, ShopArtikel
from .tests import FakeClient


class Client(FakeClient):
    einheiten_im_shop = [{"id": "u-kg", "name": "Kilogramm", "shortCode": "kg"}, {"id": "u-l", "name": "Liter", "shortCode": "l"}]

    def einheiten(self):
        self.einheiten_abgefragt = getattr(self, "einheiten_abgefragt", 0) + 1
        return self.einheiten_im_shop


class WeitereKategorienTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.shop = Shop.objects.create(mandant=self.m, name="Webshop", url="https://shop.example.test", client_id="abc")
        self.st = Steuersatz.objects.create(bezeichnung="19", prozent=D("19"))
        self.haupt = Kategorie.objects.create(name="Notebooks", online=True)
        self.zweit = Kategorie.objects.create(name="Angebote", online=True)
        self.dritt = Kategorie.objects.create(name="Büro", online=True)
        self.aus = Kategorie.objects.create(name="Intern", online=False)
        self.a = Artikel.objects.create(nummer="A1", bezeichnung="Notebook", steuersatz=self.st, vk1=D("100"), online=True, kategorie=self.haupt)

    def senden(self, c=None):
        c = c or Client(self.shop, produkte={"A1": "prod-1"})
        services.artikeldaten_senden(self.shop, c)
        return c

    def ids(self, *kategorien):
        return [{"id": services.feste_id(self.shop, "kategorie", k.pk)} for k in kategorien]

    # -- Kategorien im Payload ---------------------------------------------------------------------
    def test_payload_enthaelt_haupt_und_weitere_kategorien(self):
        self.a.weitere_kategorien.set([self.zweit, self.dritt])
        c = self.senden()
        self.assertEqual(len(c.gesendet[0]["categories"]), 3)
        self.assertEqual(c.gesendet[0]["categories"][0], self.ids(self.haupt)[0])
        self.assertEqual({e["id"] for e in c.gesendet[0]["categories"]}, {e["id"] for e in self.ids(self.haupt, self.zweit, self.dritt)})
        self.assertEqual(len(ShopArtikel.objects.get().kategorie_ids), 3)

    def test_ohne_weitere_kategorien_bleibt_alles_beim_alten(self):
        c = self.senden()
        self.assertEqual(c.gesendet[0]["categories"], self.ids(self.haupt))
        self.assertFalse(hasattr(c, "kategorie_weg"))

    def test_entfernte_weitere_kategorie_wird_im_shop_entfernt(self):
        self.a.weitere_kategorien.set([self.zweit, self.dritt])
        self.senden()
        self.a.weitere_kategorien.set([self.dritt])
        c = self.senden()
        self.assertEqual(c.kategorie_weg, [self.ids(self.zweit)[0]["id"]])
        self.assertEqual({e["id"] for e in c.gesendet[0]["categories"]}, {e["id"] for e in self.ids(self.haupt, self.dritt)})
        self.assertEqual(set(ShopArtikel.objects.get().kategorie_ids), {e["id"] for e in self.ids(self.haupt, self.dritt)})

    def test_wechsel_der_hauptkategorie_entfernt_die_alte(self):
        self.senden()
        self.a.kategorie = self.zweit
        self.a.save()
        c = self.senden()
        self.assertEqual(c.kategorie_weg, [self.ids(self.haupt)[0]["id"]])

    def test_offline_kategorie_wird_nicht_uebertragen(self):
        self.a.weitere_kategorien.set([self.aus, self.zweit])
        c = self.senden()
        self.assertEqual({e["id"] for e in c.gesendet[0]["categories"]}, {e["id"] for e in self.ids(self.haupt, self.zweit)})
        self.assertFalse(hasattr(c, "kategorien_geschrieben") and any(k["name"] == "Intern" for k in c.kategorien_geschrieben))

    def test_weitere_kategorie_wird_offline_und_im_shop_entfernt(self):
        self.a.weitere_kategorien.set([self.zweit])
        self.senden()
        self.zweit.online = False
        self.zweit.save()
        c = self.senden()
        self.assertEqual(c.kategorie_weg, [self.ids(self.zweit)[0]["id"]])
        self.assertEqual(c.gesendet[0]["categories"], self.ids(self.haupt))

    # -- Sichtbarkeit ------------------------------------------------------------------------------
    def sichtbar(self):
        return Artikel.objects.shop_sichtbar().filter(pk=self.a.pk).exists()

    def test_offline_hauptkategorie_aber_online_zusatzkategorie_ist_sichtbar(self):
        self.a.kategorie = self.aus
        self.a.save()
        self.assertFalse(self.sichtbar())
        self.assertTrue(Artikel.objects.wegen_kategorie_offline().filter(pk=self.a.pk).exists())
        self.a.weitere_kategorien.set([self.zweit])
        self.assertTrue(self.sichtbar())
        self.assertFalse(Artikel.objects.wegen_kategorie_offline().filter(pk=self.a.pk).exists())
        c = self.senden()
        self.assertEqual(c.gesendet[0]["categories"], self.ids(self.zweit))

    def test_alle_kategorien_offline_ist_nicht_sichtbar(self):
        aus2 = Kategorie.objects.create(name="Intern 2", online=False)
        self.a.kategorie = self.aus
        self.a.save()
        self.a.weitere_kategorien.set([aus2])
        self.assertFalse(self.sichtbar())

    def test_online_hauptkategorie_mit_offline_zusatz_bleibt_sichtbar(self):
        self.a.weitere_kategorien.set([self.aus])
        self.assertTrue(self.sichtbar())

    def test_ohne_hauptkategorie_zaehlen_die_weiteren(self):
        self.a.kategorie = None
        self.a.save()
        self.assertTrue(self.sichtbar())  # gar keine Kategorie: sichtbar wie bisher
        self.a.weitere_kategorien.set([self.aus])
        self.assertFalse(self.sichtbar())
        self.a.weitere_kategorien.add(self.zweit)
        self.assertTrue(self.sichtbar())

    def test_offline_elternkategorie_macht_zusatz_unterkategorie_offline(self):
        self.aus_unter = Kategorie.objects.create(name="Unter", eltern=self.aus, online=True)
        self.a.kategorie = self.aus
        self.a.save()
        self.a.weitere_kategorien.set([self.aus_unter])
        self.assertFalse(self.sichtbar())

    def test_nur_ueber_zusatzkategorie_sichtbarer_artikel_wird_nicht_deaktiviert(self):
        self.a.weitere_kategorien.set([self.zweit])
        self.senden()
        self.haupt.online = False
        self.haupt.save()
        c = Client(self.shop, produkte={"A1": "prod-1"})
        services.abgleichen(self.shop, c)
        self.assertNotIn({"id": "prod-1", "active": False}, c.gesendet)
        self.assertTrue(ShopArtikel.objects.get().im_shop_aktiv)

    def test_wegen_aller_kategorien_offline_wird_im_shop_deaktiviert(self):
        self.a.weitere_kategorien.set([self.zweit])
        self.senden()
        self.haupt.online = self.zweit.online = False
        self.haupt.save()
        self.zweit.save()
        c = Client(self.shop, produkte={"A1": "prod-1"})
        services.abgleichen(self.shop, c)
        self.assertIn({"id": "prod-1", "active": False}, c.gesendet)

    # -- UVP und Grundpreis ------------------------------------------------------------------------
    def test_uvp_wird_als_listenpreis_gesendet(self):
        self.a.uvp = D("150")
        self.a.save()
        c = self.senden()
        preis = c.gesendet[0]["price"][0]
        self.assertEqual((preis["net"], preis["gross"]), (100.0, 119.0))
        self.assertEqual((preis["listPrice"]["net"], preis["listPrice"]["gross"]), (150.0, 178.5))

    def test_ohne_uvp_kein_listenpreis(self):
        c = self.senden()
        self.assertNotIn("listPrice", c.gesendet[0]["price"][0])

    def test_uvp_aenderung_wird_nachgesendet_und_bestand_preis_abgleich_behaelt_listenpreis(self):
        self.senden()
        self.a.uvp = D("200")
        self.a.save()
        c = self.senden()
        self.assertEqual(c.gesendet[0]["price"][0]["listPrice"]["net"], 200.0)
        c2 = Client(self.shop, produkte={"A1": "prod-1"})
        services.abgleichen(self.shop, c2, alle=True)
        self.assertEqual(c2.gesendet[0]["price"][0]["listPrice"]["gross"], 238.0)

    def test_grundpreis_als_inhalt_bezugsgroesse_und_einheit(self):
        self.a.grundpreis_faktor, self.a.grundpreis_einheit = D("10"), "kg"
        self.a.save()
        c = self.senden()
        e = c.gesendet[0]
        self.assertEqual((e["purchaseUnit"], e["referenceUnit"], e["unitId"]), (0.1, 1.0, "u-kg"))

    def test_grundpreis_einheit_ueber_namen_ohne_gross_klein(self):
        self.a.grundpreis_faktor, self.a.grundpreis_einheit = D("4"), "LITER"
        self.a.save()
        e = self.senden().gesendet[0]
        self.assertEqual((e["purchaseUnit"], e["unitId"]), (0.25, "u-l"))

    def test_grundpreis_mit_unbekannter_einheit_wird_weggelassen_und_gemeldet(self):
        self.a.grundpreis_faktor, self.a.grundpreis_einheit = D("10"), "100 g"
        self.a.save()
        c = Client(self.shop, produkte={"A1": "prod-1"})
        erg = services.artikeldaten_senden(self.shop, c)
        self.assertNotIn("purchaseUnit", c.gesendet[0])
        self.assertNotIn("unitId", c.gesendet[0])
        self.assertTrue(any("Einheit" in f and "100 g" in f for f in erg["fehler"]))

    def test_ohne_grundpreis_keine_einheitenabfrage(self):
        c = self.senden()
        self.assertFalse(hasattr(c, "einheiten_abgefragt"))
        self.assertNotIn("purchaseUnit", c.gesendet[0])
