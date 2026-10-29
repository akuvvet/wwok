import base64
import json
import urllib.error
from decimal import Decimal
from unittest import mock

from django.test import TestCase

from apps.kern.models import Mandant
from apps.stammdaten.models import Kunde

from . import dhl
from .models import Beleg, Belegart, Versand

ENV = {"DHL_API_KEY_OKAY": "k", "DHL_BENUTZER_OKAY": "u", "DHL_PASSWORT_OKAY": "p", "DHL_ABRECHNUNG_OKAY": "33333333330101"}


class Antwort:
    def __init__(self, daten):
        self.daten = json.dumps(daten).encode()

    def read(self):
        return self.daten

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class DhlTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer", strasse="Hauptstr. 12a", plz="42651", ort="Solingen")
        self.kunde = Kunde.objects.create(nummer="1", name1="Muster GmbH", strasse="Weg 5", plz="10115", ort="Berlin")
        self.ls = Beleg.objects.create(mandant=self.m, art=Belegart.LIEFERSCHEIN, kunde=self.kunde,
                                       versandanschrift="Muster GmbH\nz. H. Frau Meier\nMusterweg 5 b\n10115 Berlin")
        self.v = Versand.objects.create(beleg=self.ls, gewicht_kg=Decimal("2.5"))

    def test_anschrift_zerlegen(self):
        a = dhl.anschrift_zerlegen("Muster GmbH\nz. H. Frau Meier\nMusterweg 5 b\n10115 Berlin")
        self.assertEqual((a["name1"], a["name2"], a["addressStreet"], a["addressHouse"], a["postalCode"], a["city"], a["country"]),
                         ("Muster GmbH", "z. H. Frau Meier", "Musterweg", "5b", "10115", "Berlin", "DEU"))
        self.assertEqual(dhl.anschrift_zerlegen("Hans\nRue 3\n1000 Bruxelles\nBE")["country"], "BEL")
        with self.assertRaises(dhl.DhlFehler):
            dhl.anschrift_zerlegen("nur eine Zeile")

    def test_hausnummer_ohne_leerzeichen_und_keine_leeren_felder(self):
        a = dhl.anschrift_zerlegen("KOK Metallbearbeitungs GmbH\nPfaltzstr.2\n42781 Haan")
        self.assertEqual((a["addressStreet"], a["addressHouse"]), ("Pfaltzstr.", "2"))
        self.assertNotIn("name2", a)  # DHL lehnt leere Texte ab
        self.assertNotIn("name3", a)
        for strasse, erwartet in (("Hauptstraße 5a", ("Hauptstraße", "5a")), ("Am Ring 3-5", ("Am Ring", "3-5")),
                                  ("Straße des 17. Juni 5", ("Straße des 17. Juni", "5")), ("Musterweg,12", ("Musterweg", "12")),
                                  ("Postfach", ("Postfach", ""))):
            self.assertEqual(dhl.strasse_hausnummer(strasse), erwartet, strasse)
        ohne = dhl.anschrift_zerlegen("Firma\nPostfach\n10115 Berlin")
        self.assertNotIn("addressHouse", ohne)

    def test_zugang_unvollstaendig(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesMessage(dhl.DhlFehler, "DHL_API_KEY_OKAY"):
                dhl.label_erzeugen(self.v)

    def test_label_erzeugen_und_stornieren(self):
        pdf = b"%PDF-1.4 label"
        antwort = {"items": [{"shipmentNo": "340434161094042557", "label": {"b64": base64.b64encode(pdf).decode()},
                              "sstatus": {"statusCode": 200}}]}
        with mock.patch.dict("os.environ", ENV), mock.patch("urllib.request.urlopen", return_value=Antwort(antwort)) as m:
            dhl.label_erzeugen(self.v)
        anfrage = m.call_args.args[0]
        self.assertTrue(anfrage.full_url.startswith("https://api-eu.dhl.com/parcel/de/shipping/v2/orders"))
        self.assertEqual(anfrage.get_header("Dhl-api-key"), "k")
        koerper = json.loads(anfrage.data)["shipments"][0]
        self.assertEqual((koerper["product"], koerper["billingNumber"], koerper["shipper"]["addressHouse"]), ("V01PAK", "33333333330101", "12a"))
        self.v.refresh_from_db()
        self.assertEqual((self.v.sendungsnummer, bytes(self.v.label), self.v.dhl_erzeugt), ("340434161094042557", pdf, True))
        with mock.patch.dict("os.environ", ENV), mock.patch("urllib.request.urlopen", return_value=Antwort({"items": [{"sstatus": {"statusCode": 200}}]})) as m:
            dhl.stornieren(self.v)
        self.assertEqual(m.call_args.args[0].method, "DELETE")

    def test_fehler_401_ohne_absturz(self):
        fehler = urllib.error.HTTPError("u", 401, "Unauthorized", {}, None)
        fehler.read = lambda: json.dumps({"status": 401, "title": "Unauthorized", "detail": "Invalid credentials"}).encode()
        with mock.patch.dict("os.environ", ENV), mock.patch("urllib.request.urlopen", side_effect=fehler):
            with self.assertRaisesMessage(dhl.DhlFehler, "DHL meldet 401: Unauthorized; Invalid credentials"):
                dhl.label_erzeugen(self.v)

    def test_fehlermeldung_von_dhl(self):
        fehler = urllib.error.HTTPError("u", 400, "Bad", {}, None)
        fehler.read = lambda: json.dumps({"items": [{"validationMessages": [{"property": "consignee.postalCode",
                                                                              "validationMessage": "ungültig"}]}]}).encode()
        with mock.patch.dict("os.environ", ENV), mock.patch("urllib.request.urlopen", side_effect=fehler):
            with self.assertRaisesMessage(dhl.DhlFehler, "consignee.postalCode: ungültig"):
                dhl.label_erzeugen(self.v)
