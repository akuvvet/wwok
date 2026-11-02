import http.client
import itertools
import shutil
import tempfile
from datetime import timedelta
from decimal import Decimal as D
from unittest import mock

from django.contrib.auth import get_user_model
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
from .models import Shop, ShopArtikel, SyncProtokoll
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
        self.a.kategorie = Kategorie.objects.create(name="Notebooks")
        self.a.save()
        c = self.client_fuer(A1="p")
        c.shop_kategorien = [{"id": "k1", "name": "notebooks "}]
        services.artikeldaten_senden(self.shop, c)
        self.assertEqual(c.gesendet[0]["categories"], [{"id": "k1"}])
        for liste in ([], [{"id": "k1", "name": "Notebooks"}, {"id": "k2", "name": "Notebooks"}]):
            c = self.client_fuer(A1="p")
            c.shop_kategorien = liste
            erg = services.artikeldaten_senden(self.shop, c, alle=True)
            self.assertNotIn("categories", c.gesendet[0])
            self.assertEqual(erg["kategorie_fehlt"], {"Notebooks"})
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

    def test_spaeter_angelegte_kategorie_wird_nachgetragen(self):
        self.a.kategorie = Kategorie.objects.create(name="Notebooks")
        self.a.save()
        c = self.client_fuer(A1="p")
        self.assertEqual(services.artikeldaten_senden(self.shop, c)["kategorie_fehlt"], {"Notebooks"})
        c = self.client_fuer(A1="p")
        self.assertEqual(services.artikeldaten_senden(self.shop, c)["kategorie_fehlt"], {"Notebooks"})  # Hinweis bleibt
        c = self.client_fuer(A1="p")
        c.shop_kategorien = [{"id": "k1", "name": "Notebooks"}]
        services.artikeldaten_senden(self.shop, c)
        self.assertEqual(c.gesendet[0]["categories"], [{"id": "k1"}])

    def test_kategoriewechsel_entfernt_nur_die_eigene_alte(self):
        self.a.kategorie = Kategorie.objects.create(name="Notebooks")
        self.a.save()
        c = self.client_fuer(A1="p")
        c.shop_kategorien = [{"id": "k1", "name": "Notebooks"}, {"id": "k2", "name": "Tablets"}]
        services.artikeldaten_senden(self.shop, c)
        self.a.kategorie = Kategorie.objects.create(name="Tablets")
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
        self.bild(1, "a.bmp")
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
