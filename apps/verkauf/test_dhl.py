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


class GewichtUndProduktTest(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model
        from apps.stammdaten.models import Artikel, Steuersatz
        from .models import Position
        self.Position = Position
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer", strasse="Hauptstr. 12a", plz="42651", ort="Solingen")
        self.kunde = Kunde.objects.create(nummer="1", name1="Muster GmbH", strasse="Weg 5", plz="10115", ort="Berlin")
        st = Steuersatz.objects.create(bezeichnung="19", prozent=Decimal("19"))
        self.maus = Artikel.objects.create(nummer="M", bezeichnung="Maus", steuersatz=st, gewicht_kg=Decimal("0.120"))
        self.ohne = Artikel.objects.create(nummer="K", bezeichnung="Kabel", steuersatz=st)
        self.esd = Artikel.objects.create(nummer="W", bezeichnung="Windows", steuersatz=st, esd_key=True)
        self.b = Beleg.objects.create(mandant=self.m, art=Belegart.LIEFERSCHEIN, kunde=self.kunde,
                                      versandanschrift="Muster GmbH\nWeg 5\n10115 Berlin")
        for artikel, menge in ((self.maus, "3"), (self.ohne, "1"), (self.esd, "1")):
            Position.objects.create(beleg=self.b, artikel=artikel, bezeichnung=artikel.bezeichnung, menge=Decimal(menge), einzelpreis=Decimal("1"))
        Position.objects.create(beleg=self.b, typ="L", bezeichnung="Einrichtung", einzelpreis=Decimal("50"))
        self.user = get_user_model().objects.create_user("t", password="x" * 14, is_staff=True)
        self.client.force_login(self.user)
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    def test_gewicht_aus_artikeln(self):
        self.assertEqual(self.b.artikelgewicht(), (Decimal("0.36"), ["Kabel"]))  # ESD und Leistung zählen nicht

    def test_gruppe_mit_eigenem_gewicht_zaehlt_statt_unterpositionen(self):
        from apps.stammdaten.models import Artikel
        pc = Artikel.objects.create(nummer="PC", typ="G", bezeichnung="Komplett-PC", steuersatz=self.maus.steuersatz,
                                    gewicht_kg=Decimal("8"))
        b = Beleg.objects.create(mandant=self.m, art=Belegart.LIEFERSCHEIN, kunde=self.kunde)
        kopf = self.Position.objects.create(beleg=b, typ="G", artikel=pc, bezeichnung="Komplett-PC", einzelpreis=Decimal("1"))
        self.Position.objects.create(beleg=b, gruppe=kopf, artikel=self.maus, bezeichnung="Maus", einzelpreis=Decimal("0"))
        self.Position.objects.create(beleg=b, gruppe=kopf, artikel=self.ohne, bezeichnung="Kabel", einzelpreis=Decimal("0"))
        self.assertEqual(b.artikelgewicht(), (Decimal("8.00"), []))
        pc.gewicht_kg = None  # ohne Gruppengewicht: Unterpositionen zählen
        pc.save()
        self.assertEqual(b.artikelgewicht(), (Decimal("0.12"), ["Kabel"]))
        kopf.unterpositionen.all().delete()  # Gruppe ohne Gewicht und ohne Teile: als fehlend melden
        self.assertEqual(b.artikelgewicht(), (Decimal("0.00"), ["Komplett-PC"]))

    def test_versandgewicht_regeln(self):
        self.assertEqual(Versand.versandgewicht(Versand.PAKET, Decimal("0.36")), Decimal("1"))  # Paket ab 1 kg
        self.assertEqual(Versand.versandgewicht(Versand.PAKET, None), Decimal("1"))
        self.assertEqual(Versand.versandgewicht(Versand.PAKET, Decimal("2.5")), Decimal("2.5"))
        self.assertEqual(Versand.versandgewicht(Versand.KLEINPAKET, Decimal("1")), Decimal("1"))
        with self.assertRaises(ValueError):
            Versand.versandgewicht(Versand.KLEINPAKET, Decimal("1.01"))  # Warenpost höchstens 1 kg
        with self.assertRaises(ValueError):
            Versand.versandgewicht(Versand.KLEINPAKET, None)
        self.assertEqual(Versand.produkt_vorschlag(Decimal("0.36")), Versand.KLEINPAKET)
        self.assertEqual(Versand.produkt_vorschlag(Decimal("1.2")), Versand.PAKET)
        self.assertEqual(Versand.produkt_vorschlag(None), Versand.PAKET)

    def test_kleinpaket_an_dhl(self):
        antwort = {"items": [{"shipmentNo": "1", "sstatus": {"statusCode": 200}}]}
        v = Versand.objects.create(beleg=self.b, gewicht_kg=Decimal("0.36"), produkt=Versand.KLEINPAKET)
        with mock.patch.dict("os.environ", ENV), mock.patch("urllib.request.urlopen", return_value=Antwort(antwort)) as m:
            dhl.label_erzeugen(v)
        koerper = json.loads(m.call_args.args[0].data)["shipments"][0]
        self.assertEqual((koerper["product"], koerper["billingNumber"], koerper["details"]["weight"]["value"]),
                         ("V62KP", "33333333336201", 0.36))

    def test_paket_unter_1kg_wird_mit_1kg_angemeldet(self):
        antwort = {"items": [{"shipmentNo": "1", "sstatus": {"statusCode": 200}}]}
        v = Versand.objects.create(beleg=self.b, gewicht_kg=Decimal("0.36"), produkt=Versand.PAKET)
        with mock.patch.dict("os.environ", ENV), mock.patch("urllib.request.urlopen", return_value=Antwort(antwort)) as m:
            dhl.label_erzeugen(v)
        koerper = json.loads(m.call_args.args[0].data)["shipments"][0]
        self.assertEqual((koerper["product"], koerper["details"]["weight"]["value"]), ("V01PAK", 1.0))
        v.refresh_from_db()
        self.assertEqual(v.gewicht_kg, Decimal("1.00"))

    def test_kleinpaket_zu_schwer_oder_ausland_ohne_anfrage(self):
        v = Versand.objects.create(beleg=self.b, gewicht_kg=Decimal("1.5"), produkt=Versand.KLEINPAKET)
        with mock.patch.dict("os.environ", ENV), mock.patch("urllib.request.urlopen") as m:
            with self.assertRaisesMessage(dhl.DhlFehler, "höchstens 1 kg"):
                dhl.label_erzeugen(v)
            self.b.versandanschrift = "Hans\nRue 3\n1000 Bruxelles\nBE"
            self.b.save()
            v.gewicht_kg = Decimal("0.5")
            with self.assertRaisesMessage(dhl.DhlFehler, "nur innerhalb Deutschlands"):
                dhl.label_erzeugen(v)
        m.assert_not_called()

    def test_maske_schlaegt_gewicht_und_produkt_vor(self):
        from django.urls import reverse
        url = reverse("verkauf:beleg_versand", args=[self.b.pk])
        r = self.client.get(url)
        self.assertContains(r, 'value="0,36"')
        self.assertContains(r, "Kabel")  # Hinweis: Artikel ohne Gewicht …
        self.assertContains(r, '<option value="paket" selected>')  # … daher kein Kleinpaket-Vorschlag
        self.ohne.gewicht_kg = Decimal("0.05")
        self.ohne.save()
        self.assertContains(self.client.get(url), '<option value="kleinpaket" selected>')  # 0,41 kg, alles gewogen, Inland
        self.b.versandanschrift = "Hans\nRue 3\n1000 Bruxelles\nBE"
        self.b.save()
        self.assertContains(self.client.get(url), '<option value="paket" selected>')  # Ausland: nie Kleinpaket
        self.b.versandanschrift = "Muster GmbH\nWeg 5\n10115 Berlin"
        self.b.save()
        antwort = {"items": [{"shipmentNo": "9", "sstatus": {"statusCode": 200}}]}
        with mock.patch.dict("os.environ", ENV), mock.patch("urllib.request.urlopen", return_value=Antwort(antwort)):
            self.client.post(reverse("verkauf:beleg_versand", args=[self.b.pk]), {"aktion": "dhl", "gewicht": "", "produkt": "kleinpaket"})
        v = self.b.versendungen.get()
        self.assertEqual((v.produkt, v.gewicht_kg, v.sendungsnummer), ("kleinpaket", Decimal("0.41"), "9"))
