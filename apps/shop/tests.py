from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.finanzen import services as fin
from apps.kern.models import Mandant
from apps.lager.models import Bewegungsart
from apps.lager.services import buche_manuell
from apps.stammdaten.models import Artikel, Kunde, Steuersatz, Zahlungsbedingung
from apps.verkauf.models import Beleg, Belegart

from . import services
from .models import Shop, ShopArtikel, ShopBestellung
from .shopware import ShopFehler, ShopwareClient


def bestellung(nr="10001", mail="max@example.test", firma="", produkt="WIN11", menge=2, bezahlt=False, id_="o1"):
    adresse = {"id": "a1", "firstName": "Max", "lastName": "Muster", "company": firma, "street": "Hauptstr. 1",
               "zipcode": "42651", "city": "Solingen", "country": {"iso": "DE"}, "phoneNumber": "0212 123"}
    return {
        "id": id_, "orderNumber": nr, "orderDateTime": "2026-10-20T09:30:00.000+00:00", "amountTotal": 242.80,
        "customerComment": "Bitte schnell", "billingAddressId": "a1", "addresses": [adresse],
        "orderCustomer": {"email": mail, "firstName": "Max", "lastName": "Muster", "company": firma},
        "lineItems": [
            {"type": "product", "label": "Windows 11 Pro", "quantity": menge, "unitPrice": 119.0,
             "payload": {"productNumber": produkt}, "price": {"calculatedTaxes": [{"taxRate": 19}]}},
            {"type": "promotion", "label": "Gutschein", "quantity": 1, "unitPrice": -5, "payload": {}}],
        "deliveries": [{"shippingOrderAddress": adresse, "shippingMethod": {"name": "DHL Paket"},
                        "shippingCosts": {"totalPrice": 4.80, "calculatedTaxes": [{"taxRate": 19}]}}],
        "transactions": [{"paymentMethod": {"name": "PayPal"}, "stateMachineState": {"technicalName": "paid" if bezahlt else "open"}}],
    }


class FakeClient(ShopwareClient):
    """Shopware-Antworten aus dem Speicher statt aus dem Netz."""

    def __init__(self, shop, bestellungen=(), produkte=None):
        super().__init__(shop)
        self._bestellungen = list(bestellungen)
        self.produkte = produkte or {}
        self.gesendet = []
        self.seit = None

    def bestellungen(self, seit=None, seite=1, limit=50):
        self.seit = seit
        return {"data": self._bestellungen if seite == 1 else [], "total": len(self._bestellungen)}

    def produkt_ids(self, nummern):
        return {n: self.produkte[n] for n in nummern if n in self.produkte}

    def produkte_aktualisieren(self, payload):
        self.gesendet.extend(payload)


class ShopTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.st = Steuersatz.objects.create(bezeichnung="19", prozent=Decimal("19"))
        self.zb_pp = Zahlungsbedingung.objects.create(nummer=10, bezeichnung="PayPal", druck_text="Betrag per PAYPAL erhalten")
        self.shop = Shop.objects.create(mandant=self.m, name="Webshop", url="https://shop.example.test", client_id="abc",
                                        bestand_puffer=1, zahlungszuordnung={"PayPal": 10})
        self.win = Artikel.objects.create(nummer="WIN11", bezeichnung="Windows 11 Pro", steuersatz=self.st, vk1=Decimal("100"), online=True)

    def test_import_neukunde_auftrag_versand_und_zahlung(self):
        client = FakeClient(self.shop, [bestellung(bezahlt=True)])
        erg = services.bestellungen_abrufen(self.shop, client)
        self.assertEqual((erg["neu"], erg["bekannt"], erg["fehler"]), (1, 0, []))
        b = Beleg.objects.get()
        self.assertEqual((b.art, b.referenz, b.versandart, b.zahlungsbedingung), (Belegart.AUFTRAG, "10001", "DHL Paket", self.zb_pp))
        self.assertEqual(b.belegdatum.isoformat(), "2026-10-20")
        pos = b.positionen.get()  # Gutschein-Position wird nicht übernommen
        self.assertEqual((pos.artikel, pos.menge, pos.einzelpreis, pos.steuer_prozent), (self.win, Decimal("2"), Decimal("100.00"), Decimal("19")))
        self.assertEqual(b.versandkosten, Decimal("4.03"))  # 4,80 brutto
        self.assertEqual(b.summe_brutto, Decimal("242.80"))
        kunde = b.kunde
        self.assertEqual((kunde.email, kunde.ort, kunde.kundenart, kunde.nummer), ("max@example.test", "Solingen", "endkunde", "40001"))
        self.assertEqual(kunde.ansprechpartner.get().nachname, "Muster")
        self.assertIn("Solingen", b.rechnungsanschrift)
        self.assertIn("Kundenkommentar", b.bemerkung)
        self.assertEqual(fin.bezahlt(b), Decimal("242.80"))  # PayPal bereits bezahlt → Anzahlung auf den Auftrag
        self.assertEqual(ShopBestellung.objects.get().hinweis, "Neukunde 40001 angelegt")
        self.assertIsNotNone(Shop.objects.get().letzter_bestellimport)

    def test_zweiter_abruf_importiert_nicht_doppelt_und_erkennt_bestandskunden(self):
        client = FakeClient(self.shop, [bestellung(), bestellung(nr="10002", id_="o2", mail="MAX@example.test")])
        services.bestellungen_abrufen(self.shop, client)
        self.assertEqual(Kunde.objects.count(), 1)  # zweite Bestellung: gleiche E-Mail (Groß-/Kleinschreibung egal)
        erg = services.bestellungen_abrufen(self.shop, client)
        self.assertEqual((erg["neu"], erg["bekannt"]), (0, 2))
        self.assertEqual(Beleg.objects.count(), 2)
        self.assertIsNotNone(client.seit)  # späterer Abruf fragt nur ab dem letzten Import

    def test_unbekannter_artikel_und_firmenkunde(self):
        client = FakeClient(self.shop, [bestellung(produkt="UNBEKANNT", firma="Muster GmbH", mail="info@muster.test")])
        services.bestellungen_abrufen(self.shop, client)
        b = Beleg.objects.get()
        pos = b.positionen.get()
        self.assertEqual((pos.artikel, pos.typ), (None, "L"))
        self.assertIn("UNBEKANNT", pos.zusatztext)
        self.assertEqual((b.kunde.name1, b.kunde.name2, b.kunde.kundenart), ("Muster GmbH", "Max Muster", "gewerblich"))
        self.assertIn("nicht im ERP", ShopBestellung.objects.get().hinweis)

    def test_fehlerhafte_bestellung_wird_gemeldet_und_stoppt_den_zeitpunkt_nicht_falsch(self):
        kaputt = bestellung(nr="10003", id_="o3")
        kaputt["lineItems"] = [{"type": "promotion", "label": "nur Gutschein"}]
        erg = services.bestellungen_abrufen(self.shop, FakeClient(self.shop, [kaputt, bestellung()]))
        self.assertEqual((erg["neu"], len(erg["fehler"])), (1, 1))
        self.assertIsNone(Shop.objects.get().letzter_bestellimport)  # wird beim nächsten Mal erneut versucht
        self.assertEqual(ShopBestellung.objects.count(), 1)

    def test_bestandsabgleich_nur_verfuegbar_und_nur_aenderungen(self):
        buche_manuell(self.m, self.win, Bewegungsart.WARENEINGANG, Decimal("10"))
        client = FakeClient(self.shop, produkte={"WIN11": "prod-1"})
        erg = services.abgleichen(self.shop, client)
        self.assertEqual((erg["gesendet"], erg["nicht_im_shop"]), (1, 0))
        eintrag = client.gesendet[0]
        self.assertEqual((eintrag["id"], eintrag["stock"]), ("prod-1", 9))  # 10 − 1 Sicherheitsbestand
        self.assertEqual((eintrag["price"][0]["gross"], eintrag["price"][0]["net"]), (119.0, 100.0))
        zweiter = FakeClient(self.shop, produkte={"WIN11": "prod-1"})
        self.assertEqual(services.abgleichen(self.shop, zweiter)["gesendet"], 0)  # nichts geändert
        # Reservierung durch einen Auftrag senkt den gemeldeten Bestand
        kunde = Kunde.objects.create(nummer="1", name1="K")
        from apps.verkauf.models import Position
        ab = Beleg.objects.create(mandant=self.m, art=Belegart.AUFTRAG, kunde=kunde)
        Position.objects.create(beleg=ab, artikel=self.win, bezeichnung="x", menge=Decimal("4"), einzelpreis=Decimal("1"))
        dritter = FakeClient(self.shop, produkte={"WIN11": "prod-1"})
        services.abgleichen(self.shop, dritter)
        self.assertEqual(dritter.gesendet[0]["stock"], 5)
        self.assertEqual(ShopArtikel.objects.get().gemeldeter_bestand, 5)

    def test_artikel_ohne_online_haken_oder_nicht_im_shop(self):
        offline = Artikel.objects.create(nummer="OFF1", bezeichnung="x", steuersatz=self.st, vk1=Decimal("5"))
        fehlt = Artikel.objects.create(nummer="FEHLT", bezeichnung="y", steuersatz=self.st, vk1=Decimal("5"), online=True)
        client = FakeClient(self.shop, produkte={"WIN11": "p1", "OFF1": "p2"})
        erg = services.abgleichen(self.shop, client, alle=True)
        self.assertEqual((erg["gesendet"], erg["nicht_im_shop"]), (1, 1))
        self.assertNotIn("p2", [e["id"] for e in client.gesendet])

    def test_client_token_und_fehler(self):
        shop = self.shop
        client = ShopwareClient(shop)
        with self.assertRaises(ShopFehler):  # Geheimnis fehlt in der Umgebung
            client.api("GET", "/api/_info/version")
        with mock.patch.dict("os.environ", {shop.secret_variable: "geheim"}), \
                mock.patch.object(ShopwareClient, "_anfrage", side_effect=[{"access_token": "T", "expires_in": 600}, {"ok": 1}, {"ok": 2}]) as m:
            self.assertEqual(client.api("GET", "/api/x"), {"ok": 1})
            self.assertEqual(m.call_args_list[0].args[:2], ("POST", "/api/oauth/token"))
            self.assertEqual(m.call_args_list[1].args[3], {"Authorization": "Bearer T"})
            client.api("GET", "/api/y")  # Token wird wiederverwendet
            self.assertEqual(m.call_count, 3)


@override_settings(SECURE_SSL_REDIRECT=False)
class ShopMaskeTest(TestCase):
    def test_maske_und_befehl(self):
        from django.core.management import call_command
        m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        shop = Shop.objects.create(mandant=m, name="Webshop", url="https://shop.example.test", client_id="abc")
        self.client.force_login(get_user_model().objects.create_user("t", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = m.pk
        s.save()
        self.assertContains(self.client.get(reverse("shop:uebersicht")), "Webshop")
        r = self.client.post(reverse("shop:bestellungen", args=[shop.pk]), follow=True)  # ohne Geheimnis → klare Meldung
        self.assertContains(r, "SHOP_SECRET_")
        with mock.patch("apps.shop.services.ShopwareClient", lambda s: FakeClient(s, [])):
            self.client.post(reverse("shop:abgleich", args=[shop.pk]))
            call_command("shop_sync", stdout=mock.MagicMock())
        self.assertEqual(shop.protokolle.filter(art="abgleich").count(), 2)
