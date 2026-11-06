import http.client
import itertools
import shutil
import tempfile
from datetime import timedelta
from decimal import Decimal as D
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.kern.models import Mandant
from apps.lager.models import Bewegungsart
from apps.lager.services import buche_manuell
from apps.stammdaten.models import Artikel, ArtikelBild, Hersteller, Kategorie, Steuersatz

from . import services
from .models import Shop, ShopArtikel, ShopBestellung, ShopKategorie, SyncProtokoll
from .shopware import ShopFehler, ShopwareClient
from .tests import FakeClient


class Basis(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.st = Steuersatz.objects.create(bezeichnung="19", prozent=D("19"))
        self.shop = Shop.objects.create(mandant=self.m, name="Webshop", url="https://shop.example.test", client_id="abc")
        self.a = Artikel.objects.create(nummer="A1", bezeichnung="Notebook", steuersatz=self.st, vk1=D("100"), online=True,
                                        bezeichnung_erweitert="Zeile 1\n\nZeile 2 < 3", gewicht_kg=D("1.5"), laenge_cm=D("30"))

    def client_fuer(self, **produkte):
        return FakeClient(self.shop, produkte=produkte)


class ArtikeldatenTest(Basis):
    def test_neuer_artikel_wird_sichtbar_angelegt(self):
        buche_manuell(self.m, self.a, Bewegungsart.WARENEINGANG, D("3"))
        c = self.client_fuer()
        erg = services.artikeldaten_senden(self.shop, c)
        self.assertEqual((erg["neu"], erg["aktualisiert"]), (1, 0))
        e = c.gesendet[0]
        self.assertEqual((e["productNumber"], e["name"], e["taxId"], e["stock"], e["active"]), ("A1", "Notebook", "tax19", 3, True))
        self.assertEqual(e["visibilities"][0]["salesChannelId"], "kanal1")
        self.assertEqual(e["visibilities"][0]["visibility"], 30)
        self.assertEqual(e["price"][0]["gross"], 119.0)
        self.assertEqual((e["weight"], e["length"]), (1.5, 300.0))
        self.assertEqual(e["description"], "<p>Zeile 1</p>\n\n<p>Zeile 2 &lt; 3</p>")
        z = ShopArtikel.objects.get()
        self.assertEqual((z.produkt_id, z.gemeldeter_bestand, z.gemeldeter_preis), (e["id"], 3, D("119.00")))

    def test_vorhandener_artikel_ohne_neuanlage_felder_und_leeres_bleibt_weg(self):
        c = self.client_fuer(A1="prod-1")
        services.artikeldaten_senden(self.shop, c)
        e = c.gesendet[0]
        self.assertEqual(e["id"], "prod-1")
        for feld in ("productNumber", "stock", "price", "active", "taxId", "visibilities", "ean", "metaTitle", "manufacturerNumber", "media"):
            self.assertNotIn(feld, e)

    def test_nur_aenderungen_und_alle(self):
        services.artikeldaten_senden(self.shop, self.client_fuer(A1="prod-1"))
        c = self.client_fuer(A1="prod-1")
        self.assertEqual(services.artikeldaten_senden(self.shop, c)["unveraendert"], 1)
        self.assertEqual(c.gesendet, [])
        self.assertEqual(services.artikeldaten_senden(self.shop, c, alle=True)["aktualisiert"], 1)
        self.a.bezeichnung = "Notebook neu"
        self.a.save()
        c = self.client_fuer(A1="prod-1")
        self.assertEqual(services.artikeldaten_senden(self.shop, c)["aktualisiert"], 1)
        self.assertEqual(c.gesendet[0]["name"], "Notebook neu")

    def test_kategorie_nur_bei_eindeutigem_namen(self):
        self.a.kategorie = Kategorie.objects.create(name="Notebooks", online=True)
        self.a.save()
        c = self.client_fuer(A1="p")
        c.shop_kategorien = [{"id": "k1", "name": "notebooks "}]
        services.artikeldaten_senden(self.shop, c)
        self.assertEqual(c.gesendet[0]["categories"], [{"id": "k1"}])
        ShopKategorie.objects.all().delete()
        c = self.client_fuer(A1="p")
        c.shop_kategorien = [{"id": "k1", "name": "Notebooks", "parentId": "wurzel"},  # doppelt auf derselben Ebene: nichts anlegen
                             {"id": "k2", "name": "Notebooks", "parentId": "wurzel"}]
        erg = services.artikeldaten_senden(self.shop, c, alle=True)
        self.assertNotIn("categories", c.gesendet[0])
        self.assertEqual(erg["kategorie_fehlt"], {"Notebooks"})
        self.assertFalse(hasattr(c, "kategorien_geschrieben"))
        self.assertIn("Notebooks", SyncProtokoll.objects.filter(art="artikeldaten").first().meldung)

    def test_hersteller_wird_zugeordnet_oder_einmal_angelegt(self):
        self.a.hersteller = Hersteller.objects.create(name="ACME")
        self.a.save()
        b = Artikel.objects.create(nummer="A2", bezeichnung="x", steuersatz=self.st, vk1=D("1"), online=True, hersteller=self.a.hersteller)
        c = self.client_fuer(A1="p1", A2="p2")
        services.artikeldaten_senden(self.shop, c)
        self.assertEqual(c.neue_hersteller, ["ACME"])
        self.assertEqual(c.gesendet[0]["manufacturerId"], c.gesendet[1]["manufacturerId"])
        c = self.client_fuer(A1="p1", A2="p2")
        c.shop_hersteller = [{"id": "h9", "name": "acme"}]
        services.artikeldaten_senden(self.shop, c, alle=True)
        self.assertEqual(c.gesendet[0]["manufacturerId"], "h9")
        self.assertFalse(hasattr(c, "neue_hersteller"))
        self.assertEqual(b.nummer, "A2")

    def test_ohne_preis_wird_nicht_angelegt_aber_aktualisiert(self):
        self.a.vk1 = None
        self.a.save()
        with mock.patch("apps.shop.services.stufenpreis", return_value=None):
            c = self.client_fuer()
            self.assertEqual(services.artikeldaten_senden(self.shop, c)["ohne_preis"], 1)
            self.assertEqual(c.gesendet, [])
            c = self.client_fuer(A1="p")
            self.assertEqual(services.artikeldaten_senden(self.shop, c)["aktualisiert"], 1)

    def test_offline_nicht_aktiv_leistung_und_fremde_kommen_nicht_mit(self):
        Artikel.objects.create(nummer="O1", bezeichnung="x", steuersatz=self.st, vk1=D("1"))
        Artikel.objects.create(nummer="O2", bezeichnung="x", steuersatz=self.st, vk1=D("1"), online=True, nicht_aktiv=True)
        Artikel.objects.create(nummer="L1", bezeichnung="x", steuersatz=self.st, vk1=D("1"), typ="L")
        c = self.client_fuer()
        services.artikeldaten_senden(self.shop, c)
        self.assertEqual([e["productNumber"] for e in c.gesendet], ["A1"])

    def test_nur_eine_artikelnummer(self):
        Artikel.objects.create(nummer="A2", bezeichnung="x", steuersatz=self.st, vk1=D("1"), online=True)
        c = self.client_fuer()
        services.artikeldaten_senden(self.shop, c, nummer="A2")
        self.assertEqual([e["productNumber"] for e in c.gesendet], ["A2"])
        with self.assertRaisesRegex(ShopFehler, "nicht online"):
            services.artikeldaten_senden(self.shop, c, nummer="GIBTSNICHT")

    def test_fehler_bei_einem_artikel_stoppt_die_anderen_nicht(self):
        Artikel.objects.create(nummer="A2", bezeichnung="x", steuersatz=self.st, vk1=D("1"), online=True)
        c = self.client_fuer()
        original = c.produkte_aktualisieren

        def wirft(payload):
            if payload[0]["productNumber"] == "A1":
                raise ShopFehler("Shop antwortet mit 400")
            original(payload)
        c.produkte_aktualisieren = wirft
        erg = services.artikeldaten_senden(self.shop, c)
        self.assertEqual(erg["neu"], 1)
        self.assertIn("A1", erg["fehler"][0])
        self.assertEqual(ShopArtikel.objects.get().artikel.nummer, "A2")  # A1 wird beim nächsten Lauf neu versucht
        self.assertFalse(SyncProtokoll.objects.filter(art="artikeldaten").first().erfolgreich)

    def test_zeitlimit_laesst_rest_fuer_den_naechsten_lauf(self):
        Artikel.objects.create(nummer="A2", bezeichnung="x", steuersatz=self.st, vk1=D("1"), online=True)
        with mock.patch("apps.shop.services.time.monotonic", side_effect=itertools.count(0, 13)):
            erg = services.artikeldaten_senden(self.shop, self.client_fuer(), zeitlimit=25)
        self.assertEqual((erg["neu"], erg["offen"]), (1, 1))
        self.assertEqual(services.artikeldaten_senden(self.shop, self.client_fuer(A1=ShopArtikel.objects.get().produkt_id))["neu"], 1)

    def test_wiederholung_nach_abbruch_nutzt_dieselbe_id(self):
        c = self.client_fuer()
        services.artikeldaten_senden(self.shop, c)
        ShopArtikel.objects.all().delete()
        d = self.client_fuer()
        services.artikeldaten_senden(self.shop, d)
        self.assertEqual(c.gesendet[0]["id"], d.gesendet[0]["id"])
        self.assertEqual(len(c.gesendet[0]["id"]), 32)

    def test_bestandsabgleich_sendet_neu_angelegten_nicht_nochmal(self):
        buche_manuell(self.m, self.a, Bewegungsart.WARENEINGANG, D("3"))
        c = self.client_fuer()
        services.artikeldaten_senden(self.shop, c)
        z = ShopArtikel.objects.get()
        d = FakeClient(self.shop, produkte={"A1": z.produkt_id})
        self.assertEqual(services.abgleichen(self.shop, d)["gesendet"], 0)

    def test_gruppe_ohne_bestand_wird_mit_null_angelegt(self):
        self.a.typ = "G"
        self.a.save()
        with mock.patch("apps.shop.services.meldebestand", return_value=None):
            c = self.client_fuer()
            services.artikeldaten_senden(self.shop, c)
        self.assertEqual(c.gesendet[0]["stock"], 0)

    def test_steuersatz_fehlt_im_shop(self):
        self.a.steuersatz = Steuersatz.objects.create(bezeichnung="7", prozent=D("7.00"))
        self.a.save()
        erg = services.artikeldaten_senden(self.shop, self.client_fuer())
        self.assertIn("Steuersatz", erg["fehler"][0])


class ArtikeldatenRandfaelle(Basis):
    def test_vk_null_wird_nicht_angelegt(self):
        self.a.vk1 = D("0")
        self.a.save()
        c = self.client_fuer()
        self.assertEqual(services.artikeldaten_senden(self.shop, c)["ohne_preis"], 1)
        self.assertEqual(c.gesendet, [])

    def test_neuer_artikel_nur_mit_bestand_bestellbar(self):
        c = self.client_fuer()
        services.artikeldaten_senden(self.shop, c)
        self.assertIs(c.gesendet[0]["isCloseout"], True)
        d = self.client_fuer(A1="p")
        services.artikeldaten_senden(self.shop, d, alle=True)
        self.assertNotIn("isCloseout", d.gesendet[0])

    def test_fehlende_kategorie_wird_angelegt(self):
        self.a.kategorie = Kategorie.objects.create(name="Notebooks", online=True)
        self.a.save()
        c = self.client_fuer(A1="p")
        erg = services.artikeldaten_senden(self.shop, c)
        ext = services.feste_id(self.shop, "kategorie", self.a.kategorie_id)
        self.assertEqual((erg["kategorie_fehlt"], erg["kategorie_neu"]), (set(), ["Notebooks"]))
        self.assertEqual(c.gesendet[0]["categories"], [{"id": ext}])

    def test_kategoriewechsel_entfernt_nur_die_eigene_alte(self):
        self.a.kategorie = Kategorie.objects.create(name="Notebooks", online=True)
        self.a.save()
        c = self.client_fuer(A1="p")
        c.shop_kategorien = [{"id": "k1", "name": "Notebooks"}, {"id": "k2", "name": "Tablets"}]
        services.artikeldaten_senden(self.shop, c)
        self.a.kategorie = Kategorie.objects.create(name="Tablets", online=True)
        self.a.save()
        d = self.client_fuer(A1="p")
        d.shop_kategorien = c.shop_kategorien
        services.artikeldaten_senden(self.shop, d)
        self.assertEqual(d.kategorie_weg, ["k1"])
        self.assertEqual(ShopArtikel.objects.get().kategorie_id, "k2")

    def test_shop_kategorie_ohne_namen_trifft_artikel_ohne_kategorie_nicht(self):
        c = self.client_fuer(A1="p")
        c.shop_kategorien = [{"id": "root", "name": None}]
        services.artikeldaten_senden(self.shop, c)
        self.assertNotIn("categories", c.gesendet[0])

    def test_alle_mit_zeitlimit_erreicht_auch_die_hinteren(self):
        Artikel.objects.create(nummer="A2", bezeichnung="x", steuersatz=self.st, vk1=D("1"), online=True)
        services.artikeldaten_senden(self.shop, self.client_fuer(A1="p1", A2="p2"))
        gesendet = []
        for _ in range(2):
            c = self.client_fuer(A1="p1", A2="p2")
            with mock.patch("apps.shop.services.time.monotonic", side_effect=itertools.count(0, 13)):
                services.artikeldaten_senden(self.shop, c, alle=True, zeitlimit=25)
            gesendet += [e["id"] for e in c.gesendet]
        self.assertEqual(sorted(gesendet), ["p1", "p2"])

    def test_geaenderte_artikelnummer_ergibt_keine_dublette(self):
        services.artikeldaten_senden(self.shop, self.client_fuer(A1="p1"))
        self.a.nummer = "A1-NEU"
        self.a.save()
        c = FakeClient(self.shop, produkte={"A1": "p1"})
        erg = services.artikeldaten_senden(self.shop, c)
        self.assertEqual(erg["aktualisiert"], 1)
        self.assertEqual((c.gesendet[0]["id"], c.gesendet[0]["productNumber"]), ("p1", "A1-NEU"))
        self.assertNotIn("stock", c.gesendet[0])

    def test_im_shop_geloeschtes_produkt_wird_neu_angelegt(self):
        services.artikeldaten_senden(self.shop, self.client_fuer(A1="p1"))
        self.assertEqual(services.artikeldaten_senden(self.shop, self.client_fuer())["neu"], 1)

    def test_vorlauffehler_steht_im_protokoll(self):
        c = self.client_fuer()
        c.kategorien = mock.Mock(side_effect=ShopFehler("Shop nicht erreichbar"))
        with self.assertRaises(ShopFehler):
            services.artikeldaten_senden(self.shop, c)
        p = SyncProtokoll.objects.filter(art="artikeldaten").first()
        self.assertEqual((p.erfolgreich, p.meldung), (False, "Shop nicht erreichbar"))

    def test_abgleich_laesst_artikeldaten_stehen(self):
        services.artikeldaten_senden(self.shop, self.client_fuer(A1="p1"))
        stand = ShopArtikel.objects.get().daten_stand
        services.abgleichen(self.shop, self.client_fuer(A1="p1"))
        self.assertEqual(ShopArtikel.objects.get().daten_stand, stand)


class BilderTest(Basis):
    def setUp(self):
        super().setUp()
        self.ordner = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.ordner, ignore_errors=True)
        ueber = override_settings(MEDIA_ROOT=self.ordner)
        ueber.enable()
        self.addCleanup(ueber.disable)

    def bild(self, position=1, name="a.jpg"):
        b = ArtikelBild(artikel=self.a, position=position)
        b.bild.save(name, ContentFile(b"xx"))
        return b

    def test_erstes_bild_wird_titelbild_und_nur_aenderungen_laden_hoch(self):
        eins, zwei = self.bild(1), self.bild(2, "b.png")
        c = self.client_fuer(A1="p")
        services.artikeldaten_senden(self.shop, c)
        self.assertEqual([h[1] for h in c.hochgeladen], ["jpg", "png"])
        self.assertEqual(c.gesendet[0]["coverId"], c.gesendet[0]["media"][0]["id"])
        self.assertEqual([m["position"] for m in c.gesendet[0]["media"]], [1, 2])
        d = self.client_fuer(A1="p")
        services.artikeldaten_senden(self.shop, d)
        self.assertFalse(hasattr(d, "hochgeladen"))

    def test_entferntes_bild_wird_nur_als_eigenes_geloescht(self):
        eins, zwei = self.bild(1), self.bild(2, "b.png")
        services.artikeldaten_senden(self.shop, self.client_fuer(A1="p"))
        alt = list(ShopArtikel.objects.get().bild_ids)
        zwei.delete()
        c = self.client_fuer(A1="p")
        services.artikeldaten_senden(self.shop, c)
        self.assertEqual(c.geloescht, [alt[1]])
        self.assertEqual(ShopArtikel.objects.get().bild_ids, [alt[0]])

    def test_textaenderung_laedt_bilder_nicht_neu(self):
        self.bild(1)
        services.artikeldaten_senden(self.shop, self.client_fuer(A1="p"))
        self.a.bezeichnung = "Neu"
        self.a.save()
        c = self.client_fuer(A1="p")
        services.artikeldaten_senden(self.shop, c)
        self.assertFalse(hasattr(c, "hochgeladen"))
        self.assertNotIn("media", c.gesendet[0])
        self.assertFalse(hasattr(c, "geloescht"))

    def test_fremder_typ_ist_hinweis(self):
        # Neue Uploads bekommen immer eine erlaubte Endung; ältere Bestände können noch fremde Typen haben.
        from django.core.files.storage import default_storage
        name = default_storage.save(f"artikel/{self.a.pk}/alt.bmp", ContentFile(b"xx"))
        ArtikelBild.objects.create(artikel=self.a, position=1, bild=name)
        c = self.client_fuer(A1="p")
        erg = services.artikeldaten_senden(self.shop, c)
        self.assertEqual(erg["aktualisiert"], 1)
        self.assertIn("Dateityp", erg["fehler"][0])
        self.assertNotIn("media", c.gesendet[0])

    def test_fehlende_datei_laesst_bilder_im_shop_und_holt_sie_nach(self):
        self.bild(1)
        services.artikeldaten_senden(self.shop, self.client_fuer(A1="p"))
        alt = list(ShopArtikel.objects.get().bild_ids)
        zwei = self.bild(2, "b.jpg")
        pfad = zwei.bild.name
        zwei.bild.storage.delete(pfad)
        c = self.client_fuer(A1="p")
        erg = services.artikeldaten_senden(self.shop, c)
        self.assertIn("Datei fehlt", erg["fehler"][0])
        self.assertFalse(hasattr(c, "geloescht"))
        self.assertFalse(hasattr(c, "hochgeladen"))
        self.assertEqual(ShopArtikel.objects.get().bild_ids, alt)
        zwei.bild.storage.save(pfad, ContentFile(b"xx"))
        d = self.client_fuer(A1="p")
        services.artikeldaten_senden(self.shop, d)
        self.assertEqual(len(d.hochgeladen), 2)  # beim nächsten Lauf nachgeholt

    def test_vorhandene_galerie_im_shop_wird_nicht_verdoppelt(self):
        self.bild(1)
        c = self.client_fuer(A1="p")
        c.shop_bilder = ["fremd"]
        erg = services.artikeldaten_senden(self.shop, c, alle=True)
        self.assertEqual(erg["aktualisiert"], 1)
        self.assertFalse(hasattr(c, "hochgeladen"))
        self.assertNotIn("media", c.gesendet[0])

    def test_neuer_artikel_bekommt_bilder(self):
        self.bild(1)
        c = self.client_fuer()
        services.artikeldaten_senden(self.shop, c)
        self.assertEqual(len(c.hochgeladen), 1)
        self.assertIn("media", c.gesendet[0])

    def test_eigene_bilder_aus_abgebrochenem_lauf_gelten_nicht_als_fremd(self):
        b = self.bild(1)
        c = self.client_fuer(A1="p")
        c.shop_bilder = [services.feste_id(self.shop, "produktbild", b.pk)]
        services.artikeldaten_senden(self.shop, c)
        self.assertEqual(len(c.hochgeladen), 1)
        self.assertEqual(len(ShopArtikel.objects.get().bild_ids), 1)

    def test_zeitlimit_in_den_bildern_kommt_ueber_mehrere_laeufe_durch(self):
        for i, name in enumerate(["a.jpg", "b.jpg", "c.jpg"], 1):
            self.bild(i, name)
        hochgeladen = []
        for _ in range(3):
            c = self.client_fuer(A1="p")
            with mock.patch("apps.shop.services.time.monotonic", side_effect=itertools.count(0, 30)):
                erg = services.artikeldaten_senden(self.shop, c, zeitlimit=45)
            hochgeladen += getattr(c, "hochgeladen", [])
            self.assertEqual(erg["aktualisiert"], 1)
        self.assertEqual(len(hochgeladen), 3)  # jedes Bild genau einmal
        self.assertEqual(erg["offen"], 0)
        self.assertEqual(len(ShopArtikel.objects.get().bild_ids), 3)
        self.assertEqual(services.artikeldaten_senden(self.shop, self.client_fuer(A1="p"))["unveraendert"], 1)

    def test_im_shop_geloeschtes_produkt_bekommt_seine_bilder_zurueck(self):
        self.bild(1)
        services.artikeldaten_senden(self.shop, self.client_fuer(A1="p"))
        c = self.client_fuer()
        self.assertEqual(services.artikeldaten_senden(self.shop, c)["neu"], 1)
        self.assertEqual(len(c.hochgeladen), 1)
        self.assertIn("media", c.gesendet[0])
        self.assertFalse(hasattr(c, "geloescht"))


class KundenpreisTest(Basis):
    def setUp(self):
        super().setUp()
        self.regel = "a" * 32
        self.shop.preisstufe_kunden, self.shop.regel_kunden_id = 3, self.regel
        self.shop.save()
        self.a.vk3 = D("80")
        self.a.save()

    def test_abgleich_sendet_vk1_fuer_gaeste_und_vk3_fuer_angemeldete(self):
        c = self.client_fuer(A1="p")
        services.abgleichen(self.shop, c)
        e = c.gesendet[0]
        self.assertEqual((e["price"][0]["gross"], e["price"][0]["net"]), (119.0, 100.0))
        p = e["prices"][0]
        self.assertEqual((p["ruleId"], p["quantityStart"], p["quantityEnd"]), (self.regel, 1, None))
        self.assertEqual((p["price"][0]["net"], p["price"][0]["gross"]), (80.0, 95.2))
        self.assertEqual(ShopArtikel.objects.get().gemeldeter_kundenpreis, D("95.20"))

    def test_nur_aenderungen_und_preisaenderung_wird_gesendet(self):
        services.abgleichen(self.shop, self.client_fuer(A1="p"))
        c = self.client_fuer(A1="p")
        self.assertEqual(services.abgleichen(self.shop, c)["gesendet"], 0)
        self.a.vk3 = D("90")
        self.a.save()
        d = self.client_fuer(A1="p")
        services.abgleichen(self.shop, d)
        self.assertEqual(d.gesendet[0]["prices"][0]["price"][0]["net"], 90.0)
        self.assertEqual(d.gesendet[0]["prices"][0]["id"], services.feste_id(self.shop, "kundenpreis", self.a.pk))

    def test_entfallener_vk3_loescht_den_kundenpreis(self):
        services.abgleichen(self.shop, self.client_fuer(A1="p"))
        self.a.vk3 = None
        self.a.save()
        c = self.client_fuer(A1="p")
        services.abgleichen(self.shop, c)
        self.assertNotIn("prices", c.gesendet[0])
        self.assertEqual(c.preis_weg, [services.feste_id(self.shop, "kundenpreis", self.a.pk)])
        self.assertIsNone(ShopArtikel.objects.get().gemeldeter_kundenpreis)

    def test_ohne_einstellung_kein_kundenpreis(self):
        self.shop.preisstufe_kunden = 0
        self.shop.save()
        c = self.client_fuer(A1="p")
        services.abgleichen(self.shop, c)
        self.assertNotIn("prices", c.gesendet[0])

    def test_neuer_artikel_bekommt_den_kundenpreis_und_wird_nicht_nochmal_gesendet(self):
        c = self.client_fuer()
        services.artikeldaten_senden(self.shop, c)
        self.assertEqual(c.gesendet[0]["prices"][0]["price"][0]["net"], 80.0)
        d = self.client_fuer(A1=ShopArtikel.objects.get().produkt_id)
        self.assertEqual(services.abgleichen(self.shop, d)["gesendet"], 0)

    def test_stufe_ohne_gueltige_regel_wird_abgelehnt(self):
        for wert in ("", "regel1", "A" * 32):
            self.shop.regel_kunden_id = wert
            with self.assertRaises(ValidationError):
                self.shop.clean()

    def test_fremder_preis_im_shop_bleibt_und_wird_gemeldet(self):
        c = self.client_fuer(A1="p")
        c.shop_preise = [{"id": "fremd", "productId": "p"}]
        erg = services.abgleichen(self.shop, c)
        self.assertNotIn("prices", c.gesendet[0])
        self.assertEqual(c.gesendet[0]["price"][0]["gross"], 119.0)  # Standardpreis geht trotzdem raus
        self.assertEqual(erg["kundenpreis_belegt"], 1)
        self.assertIsNone(ShopArtikel.objects.get().gemeldeter_kundenpreis)
        self.assertIn("schon ein Preis mit Regel", SyncProtokoll.objects.filter(art="abgleich").first().meldung)

    def test_eigener_preis_im_shop_zaehlt_nicht_als_fremd(self):
        services.abgleichen(self.shop, self.client_fuer(A1="p"))
        self.a.vk3 = D("90")
        self.a.save()
        c = self.client_fuer(A1="p")
        c.shop_preise = [{"id": services.feste_id(self.shop, "kundenpreis", self.a.pk), "productId": "p"}]
        services.abgleichen(self.shop, c)
        self.assertIn("prices", c.gesendet[0])

    def test_fehlende_regel_haelt_bestand_und_standardpreis_nicht_auf(self):
        c = self.client_fuer(A1="p")
        c.regel_da = False
        erg = services.abgleichen(self.shop, c)
        self.assertEqual(erg["gesendet"], 1)
        self.assertNotIn("prices", c.gesendet[0])
        self.assertTrue(SyncProtokoll.objects.filter(erfolgreich=False, meldung__contains="gibt es im Shop nicht").exists())
        d = self.client_fuer()
        d.regel_da = False
        Artikel.objects.create(nummer="A2", bezeichnung="x", steuersatz=self.st, vk1=D("1"), vk3=D("1"), online=True)
        services.artikeldaten_senden(self.shop, d, nummer="A2")
        self.assertNotIn("prices", d.gesendet[0])

    def test_regelwechsel_wird_gesendet(self):
        services.abgleichen(self.shop, self.client_fuer(A1="p"))
        self.shop.regel_kunden_id = "b" * 32
        self.shop.save()
        c = self.client_fuer(A1="p")
        services.abgleichen(self.shop, c)
        self.assertEqual(c.gesendet[0]["prices"][0]["ruleId"], "b" * 32)
        self.assertEqual(ShopArtikel.objects.get().gemeldete_regel, "b" * 32)

    def test_neu_angelegtes_produkt_bekommt_den_preis_wieder(self):
        services.abgleichen(self.shop, self.client_fuer(A1="p"))
        c = self.client_fuer(A1="neu")
        services.abgleichen(self.shop, c)
        self.assertIn("prices", c.gesendet[0])

    def test_fehler_beim_entfernen_wird_beim_naechsten_abgleich_wiederholt(self):
        services.abgleichen(self.shop, self.client_fuer(A1="p"))
        self.a.vk3 = None
        self.a.save()
        c = self.client_fuer(A1="p")
        c.kundenpreise_loeschen = mock.Mock(side_effect=ShopFehler("Shop antwortet mit 500"))
        services.abgleichen(self.shop, c)
        self.assertIsNotNone(ShopArtikel.objects.get().gemeldeter_kundenpreis)
        self.assertTrue(SyncProtokoll.objects.filter(erfolgreich=False, meldung__contains="nicht entfernt").exists())
        d = self.client_fuer(A1="p")
        services.abgleichen(self.shop, d)
        self.assertEqual(len(d.preis_weg), 1)
        self.assertIsNone(ShopArtikel.objects.get().gemeldeter_kundenpreis)


class NettoBestellungTest(Basis):
    def bestellung(self, status, **extra):
        from .tests import bestellung
        b = bestellung(produkt="A1", menge=1)
        b["lineItems"] = [z for z in b["lineItems"] if z["type"] == "product"]
        b["lineItems"][0]["unitPrice"] = 80.0
        b["deliveries"][0]["shippingCosts"]["totalPrice"] = 5.0
        b.update(taxStatus=status, **extra)
        return b

    def test_netto_kunde_wird_nicht_nochmal_heruntergerechnet(self):
        b = self.bestellung("net", amountTotal=101.15)
        beleg = services.bestellung_importieren(self.shop, b).beleg
        self.assertEqual(beleg.positionen.get().einzelpreis, D("80.00"))
        self.assertEqual(beleg.versandkosten, D("5.00"))
        self.assertEqual(beleg.summe_brutto, D("101.15"))
        self.assertEqual(ShopBestellung.objects.get().hinweis.count("weicht"), 0)

    def test_brutto_kunde_bleibt_wie_bisher(self):
        beleg = services.bestellung_importieren(self.shop, self.bestellung("gross", amountTotal=85.0)).beleg
        self.assertEqual(beleg.positionen.get().einzelpreis, D("67.23"))

    def test_steuerfrei_ohne_steuer(self):
        beleg = services.bestellung_importieren(self.shop, self.bestellung("tax-free", amountTotal=85.0)).beleg
        p = beleg.positionen.get()
        self.assertEqual((p.einzelpreis, p.steuer_prozent), (D("80.00"), D("0")))


class OfflineTest(Basis):
    def melden(self):
        services.abgleichen(self.shop, FakeClient(self.shop, produkte={"A1": "prod-1"}))

    def test_haken_weg_deaktiviert_und_haken_wieder_aktiviert(self):
        self.melden()
        self.a.online = False
        self.a.save()
        c = FakeClient(self.shop, produkte={"A1": "prod-1"})
        erg = services.abgleichen(self.shop, c)
        self.assertEqual(c.gesendet, [{"id": "prod-1", "active": False}])
        self.assertEqual(erg["deaktiviert"], 1)
        d = FakeClient(self.shop, produkte={"A1": "prod-1"})
        services.abgleichen(self.shop, d)
        self.assertEqual(d.gesendet, [])  # nur einmal
        self.a.online = True
        self.a.save()
        e = FakeClient(self.shop, produkte={"A1": "prod-1"})
        services.abgleichen(self.shop, e)
        self.assertIs(e.gesendet[0]["active"], True)
        self.assertTrue(ShopArtikel.objects.get().im_shop_aktiv)

    def test_massenhaftes_abschalten_wird_gebremst(self):
        Artikel.objects.bulk_create([Artikel(nummer=f"M{i}", bezeichnung="x", steuersatz=self.st, vk1=D("1"), online=True) for i in range(51)])
        produkte = {f"M{i}": f"p{i}" for i in range(51)}
        services.abgleichen(self.shop, FakeClient(self.shop, produkte=produkte))
        Artikel.objects.filter(nummer__startswith="M").update(online=False)
        c = FakeClient(self.shop, produkte=produkte)
        self.assertEqual(services.abgleichen(self.shop, c)["deaktiviert"], 0)
        self.assertEqual(c.gesendet, [])
        self.assertTrue(SyncProtokoll.objects.filter(erfolgreich=False, meldung__contains="ungewöhnlich viel").exists())
        d = FakeClient(self.shop, produkte=produkte)
        self.assertEqual(services.abgleichen(self.shop, d, alle=True)["deaktiviert"], 51)

    def test_fremdes_produkt_mit_der_neuen_nummer_bleibt_unberuehrt(self):
        self.melden()
        self.a.nummer, self.a.online = "X9", False
        self.a.save()
        c = FakeClient(self.shop, produkte={"A1": "prod-1", "X9": "fremd"})
        services.abgleichen(self.shop, c)
        self.assertEqual(c.gesendet, [{"id": "prod-1", "active": False}])

    def test_fehler_beim_deaktivieren_haelt_den_bestand_nicht_auf(self):
        Artikel.objects.create(nummer="A2", bezeichnung="x", steuersatz=self.st, vk1=D("1"), online=True)
        services.abgleichen(self.shop, FakeClient(self.shop, produkte={"A1": "prod-1", "A2": "prod-2"}))
        self.a.online = False
        self.a.save()
        Artikel.objects.filter(nummer="A2").update(vk1=D("2"))
        c = FakeClient(self.shop, produkte={"A1": "prod-1", "A2": "prod-2"})
        c.produkt_vorhanden = mock.Mock(side_effect=ShopFehler("Shop antwortet mit 500"))
        erg = services.abgleichen(self.shop, c)
        self.assertEqual(erg["gesendet"], 1)
        self.assertTrue(SyncProtokoll.objects.filter(erfolgreich=False, meldung__contains="Deaktivieren").exists())
        self.assertTrue(ShopArtikel.objects.get(artikel=self.a).im_shop_aktiv)  # wird beim nächsten Abgleich nachgeholt

    def test_geaenderte_nummer_wird_ueber_die_id_deaktiviert(self):
        self.melden()
        self.a.nummer, self.a.online = "NEU", False
        self.a.save()
        c = FakeClient(self.shop, produkte={"A1": "prod-1"})
        services.abgleichen(self.shop, c)
        self.assertEqual(c.gesendet, [{"id": "prod-1", "active": False}])

    def test_nicht_aktiv_deaktiviert(self):
        self.melden()
        self.a.nicht_aktiv = True
        self.a.save()
        c = FakeClient(self.shop, produkte={"A1": "prod-1"})
        services.abgleichen(self.shop, c)
        self.assertEqual(c.gesendet, [{"id": "prod-1", "active": False}])

    def kategorie_mit_artikel(self, online=True):
        self.ober = Kategorie.objects.create(name="Hardware", online=True)
        self.kat = Kategorie.objects.create(name="Notebooks", eltern=self.ober, online=online)
        self.a.kategorie = self.kat
        self.a.save()
        self.shop_kategorien = [{"id": "k-ober", "name": "Hardware", "parentId": "wurzel"},
                                {"id": "k-unter", "name": "Notebooks", "parentId": "k-ober"}, {"id": "k-fremd", "name": "Fremd", "parentId": "wurzel"}]

    def shop_client(self):
        c = FakeClient(self.shop, produkte={"A1": "prod-1"})
        c.shop_kategorien = self.shop_kategorien
        return c

    def test_kategorie_offline_deaktiviert_artikel_und_shop_kategorie_und_online_bringt_zurueck(self):
        self.kategorie_mit_artikel()
        services.artikeldaten_senden(self.shop, self.shop_client())
        services.abgleichen(self.shop, self.shop_client())
        self.kat.online = False
        self.kat.save()
        c = self.shop_client()
        erg = services.abgleichen(self.shop, c)
        self.assertEqual(c.gesendet, [{"id": "prod-1", "active": False}])
        self.assertEqual(erg["deaktiviert"], 1)
        self.assertEqual(c.kategorie_aktiv, [("k-unter", False)])  # nur die zugeordnete, nie "k-fremd"
        d = self.shop_client()
        services.abgleichen(self.shop, d)
        self.assertEqual((d.gesendet, getattr(d, "kategorie_aktiv", [])), ([], []))  # nur einmal
        self.kat.online = True
        self.kat.save()
        e = self.shop_client()
        services.abgleichen(self.shop, e)
        self.assertIs(e.gesendet[0]["active"], True)
        self.assertEqual(e.kategorie_aktiv, [("k-unter", True)])

    def test_artikel_mit_haken_aus_bleibt_nach_kategorie_online_offline(self):
        self.kategorie_mit_artikel()
        services.artikeldaten_senden(self.shop, self.shop_client())
        services.abgleichen(self.shop, self.shop_client())
        self.a.online = False
        self.a.save()
        services.abgleichen(self.shop, self.shop_client())
        self.kat.online = False
        self.kat.save()
        services.abgleichen(self.shop, self.shop_client())
        self.kat.online = True
        self.kat.save()
        c = self.shop_client()
        services.abgleichen(self.shop, c)
        self.assertEqual(c.gesendet, [])
        self.assertFalse(ShopArtikel.objects.get().im_shop_aktiv)

    def test_elternkategorie_offline_schaltet_unterkategorie_ab(self):
        self.kategorie_mit_artikel()
        services.artikeldaten_senden(self.shop, self.shop_client())
        services.abgleichen(self.shop, self.shop_client())
        self.ober.online = False
        self.ober.save()
        c = self.shop_client()
        services.abgleichen(self.shop, c)
        self.assertEqual(c.gesendet, [{"id": "prod-1", "active": False}])
        self.assertIn(self.kat.pk, Kategorie.offline_ids())
        self.assertIsNone(self.kat.offline_grund().eltern_id)
        self.assertEqual(self.kat.offline_grund(), self.ober)

    def test_neue_kategorie_ist_standardmaessig_offline_und_artikel_nicht_sichtbar(self):
        k = Kategorie.objects.create(name="Neu")
        self.assertFalse(k.online)
        self.a.kategorie = k
        self.a.save()
        self.assertFalse(Artikel.objects.shop_sichtbar().filter(pk=self.a.pk).exists())
        self.assertTrue(Artikel.objects.wegen_kategorie_offline().filter(pk=self.a.pk).exists())

    def test_artikel_ohne_kategorie_unveraendert_sichtbar(self):
        self.assertTrue(Artikel.objects.shop_sichtbar().filter(pk=self.a.pk).exists())

    def test_schutzgrenze_blockiert_bewusst_abgeschaltete_kategorie_nicht(self):
        kat = Kategorie.objects.create(name="Masse", online=True)
        Artikel.objects.bulk_create([Artikel(nummer=f"M{i}", bezeichnung="x", steuersatz=self.st, vk1=D("1"), online=True, kategorie=kat)
                                     for i in range(51)])
        produkte = {f"M{i}": f"p{i}" for i in range(51)}
        services.abgleichen(self.shop, FakeClient(self.shop, produkte=produkte))
        kat.online = False
        kat.save()
        c = FakeClient(self.shop, produkte=produkte)
        self.assertEqual(services.abgleichen(self.shop, c)["deaktiviert"], 51)

    def test_nie_gemeldeter_artikel_bleibt_im_shop_unberuehrt(self):
        Artikel.objects.create(nummer="OFF", bezeichnung="x", steuersatz=self.st, vk1=D("1"))
        c = FakeClient(self.shop, produkte={"OFF": "prod-9"})
        services.abgleichen(self.shop, c)
        self.assertEqual(c.gesendet, [])

    def test_im_shop_geloeschtes_produkt_bricht_nichts_ab(self):
        self.melden()
        self.a.online = False
        self.a.save()
        c = FakeClient(self.shop)
        services.abgleichen(self.shop, c)
        self.assertEqual(c.gesendet, [])
        self.assertFalse(ShopArtikel.objects.get().im_shop_aktiv)


class TaktTest(Basis):
    def test_standard_nur_per_knopf(self):
        self.assertFalse(services.artikeldaten_faellig(self.shop))
        with mock.patch("apps.shop.services.ShopwareClient", lambda s: FakeClient(s, [])):
            call_command("shop_sync", stdout=mock.MagicMock())
        self.assertFalse(SyncProtokoll.objects.filter(art="artikeldaten").exists())

    def test_takt_laeuft_und_wartet(self):
        self.shop.artikeldaten_takt = 60
        self.shop.save()
        with mock.patch("apps.shop.services.ShopwareClient", lambda s: FakeClient(s, [])):
            call_command("shop_sync", stdout=mock.MagicMock())
            call_command("shop_sync", stdout=mock.MagicMock())
        self.assertEqual(SyncProtokoll.objects.filter(art="artikeldaten").count(), 1)
        self.shop.refresh_from_db()
        self.assertFalse(services.artikeldaten_faellig(self.shop))
        self.assertTrue(services.artikeldaten_faellig(self.shop, timezone.now() + timedelta(minutes=61)))

    def test_nur_bestellungen_laesst_artikeldaten_aus(self):
        self.shop.artikeldaten_takt = 10
        self.shop.save()
        with mock.patch("apps.shop.services.ShopwareClient", lambda s: FakeClient(s, [])):
            call_command("shop_sync", nur_bestellungen=True, stdout=mock.MagicMock())
        self.assertFalse(SyncProtokoll.objects.filter(art="artikeldaten").exists())


class ClientTest(Basis):
    def test_bild_geht_roh_mit_dateityp_hinaus(self):
        client = ShopwareClient(self.shop)
        with mock.patch.dict("os.environ", {self.shop.secret_variable: "g"}), \
                mock.patch("apps.shop.shopware.urllib.request.urlopen") as url:
            url.return_value.__enter__.return_value.read.side_effect = [b'{"access_token": "T", "expires_in": 600}', b'{}', b"", b""]
            client.bild_hochladen("m1", "wwok-1-2", "png", b"\x89PNG")
        anfrage = url.call_args_list[-1].args[0]
        self.assertEqual(anfrage.data, b"\x89PNG")
        self.assertEqual(anfrage.get_header("Content-type"), "image/png")
        self.assertIn("/api/_action/media/m1/upload?extension=png&fileName=wwok-1-2", anfrage.full_url)

    def test_suchpfade_haben_bindestriche(self):
        client = ShopwareClient(self.shop)
        with mock.patch.object(ShopwareClient, "api", return_value={"data": []}) as api:
            client.hersteller()
            client.kategorien()
            client.steuern()
        self.assertEqual([c.args[1] for c in api.call_args_list],
                         ["/api/search/product-manufacturer", "/api/search/category", "/api/search/tax"])
        self.assertEqual(api.call_args_list[0].args[2]["includes"], {"product_manufacturer": ["id", "name"]})

    def test_loeschen_404_ist_erledigt_andere_fehler_nicht(self):
        client = ShopwareClient(self.shop)
        with mock.patch.object(ShopwareClient, "api", side_effect=ShopFehler("weg", 404)):
            client.produktbild_loeschen("p", "b")
            client.kategorie_entfernen("p", "k")
        with mock.patch.object(ShopwareClient, "api", side_effect=ShopFehler("kaputt", 500)):
            with self.assertRaises(ShopFehler):
                client.produktbild_loeschen("p", "b")

    def test_abgeschnittene_antwort_wird_shopfehler(self):
        client = ShopwareClient(self.shop)
        with mock.patch("apps.shop.shopware.urllib.request.urlopen", side_effect=http.client.IncompleteRead(b"")):
            with self.assertRaises(ShopFehler):
                client._anfrage("GET", "/api/x")

    def test_medienordner_wird_einmal_geholt_und_gesendet(self):
        client = ShopwareClient(self.shop)
        with mock.patch.object(ShopwareClient, "api", side_effect=[{"data": [{"folder": {"id": "ordner1"}}]}, {}, {}, {}, {}]) as api:
            client.bild_hochladen("m1", "n", "jpg", b"x")
            client.bild_hochladen("m2", "n2", "jpg", b"x")
        self.assertEqual(api.call_args_list[1].args[2]["write-medien"]["payload"], [{"id": "m1", "mediaFolderId": "ordner1"}])
        self.assertEqual(api.call_count, 5)  # Ordner nur einmal, je Bild Anlegen und Hochladen

    def test_verkaufskanal(self):
        client = ShopwareClient(self.shop)
        with mock.patch.object(ShopwareClient, "api", return_value={"data": [{"id": "k1"}]}):
            self.assertEqual(client.verkaufskanal(), "k1")
        with mock.patch.object(ShopwareClient, "api", return_value={"data": [{"id": "k1"}, {"id": "k2"}]}):
            with self.assertRaisesRegex(ShopFehler, "2 aktive"):
                client.verkaufskanal()
        self.shop.verkaufskanal_id = "fest"
        self.assertEqual(client.verkaufskanal(), "fest")

    def test_alle_laeuft_ueber_mehrere_seiten(self):
        client = ShopwareClient(self.shop)
        seiten = [{"data": [{"id": str(i), "name": "x"} for i in range(500)]}, {"data": [{"id": "z", "name": "y"}]}]
        with mock.patch.object(ShopwareClient, "api", side_effect=seiten):
            self.assertEqual(len(client.kategorien()), 501)


@override_settings(SECURE_SSL_REDIRECT=False)
class MaskeTest(Basis):
    def setUp(self):
        super().setUp()
        self.client.force_login(get_user_model().objects.create_superuser("chef", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    def test_knopf_und_meldung(self):
        url = reverse("shop:artikeldaten", args=[self.shop.pk])
        self.assertContains(self.client.get(reverse("shop:uebersicht")), "Artikeldaten senden")
        self.assertEqual(self.client.get(url).status_code, 405)
        with mock.patch("apps.shop.services.ShopwareClient", lambda s: FakeClient(s, [])):
            r = self.client.post(url, {"nummer": " A1 "}, follow=True)
            self.assertContains(r, "1 neu angelegt")
            r = self.client.post(url, {"nummer": "NEIN"}, follow=True)
            self.assertContains(r, "nicht online")

    def test_nur_fuer_admins(self):
        self.client.force_login(get_user_model().objects.create_user("t", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()
        with mock.patch("apps.shop.services.artikeldaten_senden") as senden:
            self.client.post(reverse("shop:artikeldaten", args=[self.shop.pk]))
        senden.assert_not_called()

    def test_fremder_mandant(self):
        zone = Mandant.objects.create(kurzname="ZONE", firmenname="ZONE Handel")
        fremd = Shop.objects.create(mandant=zone, name="Z", url="https://z.example.test", client_id="z")
        self.assertEqual(self.client.post(reverse("shop:artikeldaten", args=[fremd.pk])).status_code, 404)

    def test_einstellung_in_der_verwaltung(self):
        seite = self.client.get(reverse("admin:shop_shop_change", args=[self.shop.pk]))
        self.assertContains(seite, "Artikeldaten automatisch senden")
        self.assertContains(seite, f"SHOP_SECRET_{self.shop.pk}")
        self.assertContains(seite, f'href="{reverse("shop:uebersicht")}"')
        self.assertEqual(self.client.get(reverse("admin:shop_shop_add")).status_code, 200)


class ShopKategorieAnlegenTest(Basis):
    """Issue 59: fehlende Kategorie samt Eltern im Shop anlegen, über den Pfad zuordnen, Änderungen nur für eigene nachziehen."""

    def pfad(self):
        self.hw = Kategorie.objects.create(name="Hardware", online=True)
        self.nb = Kategorie.objects.create(name="Notebooks", eltern=self.hw, online=True)
        self.a.kategorie = self.nb
        self.a.save()

    def shop_c(self, kategorien=()):
        c = self.client_fuer(A1="p")
        c.shop_kategorien = list(kategorien)
        return c

    def test_pfad_wird_unter_der_hauptnavigation_angelegt_und_nicht_doppelt(self):
        self.pfad()
        c = self.shop_c()
        erg = services.artikeldaten_senden(self.shop, c)
        hw, nb = services.feste_id(self.shop, "kategorie", self.hw.pk), services.feste_id(self.shop, "kategorie", self.nb.pk)
        self.assertEqual([(k["id"], k["name"], k["parentId"]) for k in c.kategorien_geschrieben],
                         [(hw, "Hardware", "wurzel"), (nb, "Notebooks", hw)])
        self.assertEqual(c.gesendet[0]["categories"], [{"id": nb}])
        self.assertEqual(erg["kategorie_neu"], ["Hardware", "Hardware › Notebooks"])
        self.assertIn("2 Kategorie(n) im Shop angelegt", SyncProtokoll.objects.filter(art="artikeldaten").first().meldung)
        self.assertTrue(all(z.angelegt for z in ShopKategorie.objects.all()))
        d = self.shop_c([{"id": hw, "name": "Hardware", "parentId": "wurzel"}, {"id": nb, "name": "Notebooks", "parentId": hw}])
        services.artikeldaten_senden(self.shop, d, alle=True)
        self.assertFalse(hasattr(d, "kategorien_geschrieben"))  # zweiter Lauf: nichts doppelt
        self.assertEqual(d.gesendet[0]["categories"], [{"id": nb}])

    def test_gleichnamige_unterkategorien_ueber_den_pfad(self):
        netz = Kategorie.objects.create(name="Netzwerk", online=True)
        audio = Kategorie.objects.create(name="Audio", online=True)
        kabel = Kategorie.objects.create(name="Kabel", eltern=audio, online=True)
        Kategorie.objects.create(name="Kabel", eltern=netz, online=True)
        self.a.kategorie = kabel
        self.a.save()
        c = self.shop_c([{"id": "s-netz", "name": "Netzwerk", "parentId": "wurzel"}, {"id": "s-netzkabel", "name": "Kabel", "parentId": "s-netz"},
                         {"id": "s-audio", "name": "Audio", "parentId": "wurzel"}, {"id": "s-audiokabel", "name": "Kabel", "parentId": "s-audio"}])
        services.artikeldaten_senden(self.shop, c)
        self.assertEqual(c.gesendet[0]["categories"], [{"id": "s-audiokabel"}])
        self.assertFalse(hasattr(c, "kategorien_geschrieben"))
        self.assertFalse(ShopKategorie.objects.get(kategorie=kabel).angelegt)  # vorhandene übernommen

    def test_hauptkategorie_mehrfach_anderswo_wird_unter_der_hauptnavigation_angelegt(self):
        kabel = Kategorie.objects.create(name="Kabel", online=True)  # in wwok Hauptkategorie (wie aus Mention übernommen)
        self.a.kategorie = kabel
        self.a.save()
        c = self.shop_c([{"id": "s-netz", "name": "Netzwerk", "parentId": "wurzel"}, {"id": "s-netzkabel", "name": "Kabel", "parentId": "s-netz"},
                         {"id": "s-audio", "name": "Audio", "parentId": "wurzel"}, {"id": "s-audiokabel", "name": "Kabel", "parentId": "s-audio"}])
        erg = services.artikeldaten_senden(self.shop, c)
        ext = services.feste_id(self.shop, "kategorie", kabel.pk)
        self.assertEqual([(k["id"], k["name"], k["parentId"]) for k in c.kategorien_geschrieben], [(ext, "Kabel", "wurzel")])
        self.assertEqual(c.gesendet[0]["categories"], [{"id": ext}])
        self.assertEqual((erg["kategorie_fehlt"], erg["kategorie_neu"]), (set(), ["Kabel"]))
        # einmal angelegt, gilt beim nächsten Lauf die gespeicherte Zuordnung, nichts wird doppelt angelegt
        d = self.shop_c(c.shop_kategorien + [{"id": ext, "name": "Kabel", "parentId": "wurzel"}])
        services.artikeldaten_senden(self.shop, d, alle=True)
        self.assertFalse(hasattr(d, "kategorien_geschrieben"))
        self.assertEqual(d.gesendet[0]["categories"], [{"id": ext}])

    def test_hauptkategorie_direkt_unter_der_wurzel_hat_vorrang(self):
        kabel = Kategorie.objects.create(name="Kabel", online=True)
        self.a.kategorie = kabel
        self.a.save()
        c = self.shop_c([{"id": "s-kabel", "name": "Kabel", "parentId": "wurzel"}, {"id": "s-netz", "name": "Netzwerk", "parentId": "wurzel"},
                         {"id": "s-netzkabel", "name": "Kabel", "parentId": "s-netz"}])
        services.artikeldaten_senden(self.shop, c)
        self.assertFalse(hasattr(c, "kategorien_geschrieben"))
        self.assertEqual(c.gesendet[0]["categories"], [{"id": "s-kabel"}])

    def test_verlorene_zuordnung_der_eigenen_hauptkategorie_wird_wiedergefunden(self):
        kabel = Kategorie.objects.create(name="Kabel", online=True)
        self.a.kategorie = kabel
        self.a.save()
        ext = services.feste_id(self.shop, "kategorie", kabel.pk)
        c = self.shop_c([{"id": "s-audio", "name": "Audio", "parentId": "wurzel"}, {"id": "s-audiokabel", "name": "Kabel", "parentId": "s-audio"},
                         {"id": ext, "name": "Kabel", "parentId": "wurzel"}])
        services.artikeldaten_senden(self.shop, c)
        self.assertFalse(hasattr(c, "kategorien_geschrieben"))
        self.assertEqual(c.gesendet[0]["categories"], [{"id": ext}])
        self.assertTrue(ShopKategorie.objects.get(kategorie=kabel).angelegt)

    def test_anderswo_schon_vergebene_shop_kategorie_wird_nicht_geteilt(self):
        netz = Kategorie.objects.create(name="Netzwerk", online=True)
        netzkabel = Kategorie.objects.create(name="Kabel", eltern=netz, online=True)
        ShopKategorie.objects.create(shop=self.shop, kategorie=netzkabel, kategorie_id_extern="s-netzkabel")
        kabel = Kategorie.objects.create(name="Kabel", online=True)
        self.a.kategorie = kabel
        self.a.save()
        c = self.shop_c([{"id": "s-netz", "name": "Netzwerk", "parentId": "wurzel"}, {"id": "s-netzkabel", "name": "Kabel", "parentId": "s-netz"}])
        services.artikeldaten_senden(self.shop, c)
        ext = services.feste_id(self.shop, "kategorie", kabel.pk)  # eigene Hauptkategorie statt der von „Netzwerk › Kabel“
        self.assertEqual([(k["id"], k["parentId"]) for k in c.kategorien_geschrieben], [(ext, "wurzel")])

    def test_hauptkategorie_einmal_anderswo_wird_wie_bisher_uebernommen(self):
        kabel = Kategorie.objects.create(name="Kabel", online=True)
        self.a.kategorie = kabel
        self.a.save()
        c = self.shop_c([{"id": "s-netz", "name": "Netzwerk", "parentId": "wurzel"}, {"id": "s-netzkabel", "name": "Kabel", "parentId": "s-netz"}])
        services.artikeldaten_senden(self.shop, c)
        self.assertFalse(hasattr(c, "kategorien_geschrieben"))
        self.assertEqual(c.gesendet[0]["categories"], [{"id": "s-netzkabel"}])

    def test_vorhandene_ebene_wird_genutzt_nur_rest_angelegt(self):
        self.pfad()
        c = self.shop_c([{"id": "s-hw", "name": "hardware ", "parentId": "wurzel"}])
        services.artikeldaten_senden(self.shop, c)
        self.assertEqual([(k["name"], k["parentId"]) for k in c.kategorien_geschrieben], [("Notebooks", "s-hw")])

    def test_offline_kategorie_wird_nicht_angelegt(self):
        self.pfad()
        Kategorie.objects.filter(pk=self.hw.pk).update(online=False)
        c = self.shop_c()
        services.artikeldaten_senden(self.shop, c)
        self.assertEqual((c.gesendet, getattr(c, "kategorien_geschrieben", [])), ([], []))  # Artikel ist nicht sichtbar, nichts angelegt

    def test_ohne_online_artikel_wird_nichts_angelegt(self):
        self.pfad()
        Artikel.objects.filter(pk=self.a.pk).update(online=False)
        c = self.shop_c()
        services.artikeldaten_senden(self.shop, c)
        self.assertFalse(hasattr(c, "kategorien_geschrieben"))

    def test_umbenennen_und_verschieben_nur_fuer_eigene(self):
        self.pfad()
        c = self.shop_c([{"id": "s-hw", "name": "Hardware", "parentId": "wurzel"}])  # Hardware: vorhanden (fremd)
        services.artikeldaten_senden(self.shop, c)
        nb = services.feste_id(self.shop, "kategorie", self.nb.pk)
        stand = [{"id": "s-hw", "name": "Hardware", "parentId": "wurzel"}, {"id": nb, "name": "Notebooks", "parentId": "s-hw"}]
        neu = Kategorie.objects.create(name="Mobil", online=True)
        Kategorie.objects.filter(pk=self.nb.pk).update(name="Laptops", eltern=neu)
        Kategorie.objects.filter(pk=self.hw.pk).update(name="Rechner")  # fremde Shop-Kategorie: nicht umbenennen
        d = self.shop_c(stand)
        erg = services.artikeldaten_senden(self.shop, d, alle=True)
        mobil = services.feste_id(self.shop, "kategorie", neu.pk)
        self.assertEqual([(k["id"], k["name"], k["parentId"]) for k in d.kategorien_geschrieben],
                         [(mobil, "Mobil", "wurzel"), (nb, "Laptops", mobil)])
        self.assertEqual(erg["kategorie_geaendert"], ["Mobil › Laptops"])
        self.assertNotIn("s-hw", [k["id"] for k in d.kategorien_geschrieben])

    def test_fehler_beim_anlegen_stoppt_artikel_nicht(self):
        self.pfad()
        c = self.shop_c()
        c.kategorie_fehler = ShopFehler("403 Recht fehlt")
        erg = services.artikeldaten_senden(self.shop, c)
        self.assertNotIn("categories", c.gesendet[0])
        self.assertTrue(any("403 Recht fehlt" in f for f in erg["fehler"]))
        self.assertEqual(erg["neu"] + erg["aktualisiert"], 1)
