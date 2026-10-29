"""Briefversand über die INTERNETMARKE – mit nachgebildeter Schnittstelle, es wird nichts gekauft."""
import json
import urllib.error
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.kern.models import Mandant
from apps.stammdaten.models import Kunde

from . import internetmarke as im
from .models import Beleg, Belegart, Briefprodukt, Versand

ENV = {"DHL_API_KEY_OKAY": "k", "DHL_API_SECRET_OKAY": "s", "DP_PORTOKASSE_BENUTZER_OKAY": "post@okay.test",
       "DP_PORTOKASSE_PASSWORT_OKAY": "geheim"}
PDF = b"%PDF-1.4 marke"


class Antwort:
    def __init__(self, inhalt):
        self.inhalt = inhalt if isinstance(inhalt, bytes) else json.dumps(inhalt).encode()

    def read(self):
        return self.inhalt

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class PostNachbildung:
    """Antwortet wie die INTERNETMARKE-Schnittstelle und merkt sich die Anfragen."""

    def __init__(self, formate=None, kauf_fehler=None, pdf=PDF):
        self.anfragen = []
        self.formate = formate if formate is not None else [
            {"id": 7, "name": "Etikett A4", "pageType": "LABELPAGE", "pageLayout": {"size": {"x": 210, "y": 297}}},
            {"id": 42, "name": "DYMO LabelWriter 99014", "pageType": "LABELPRINTER", "pageLayout": {"size": {"x": 101, "y": 54}}}]
        self.kauf_fehler, self.pdf = kauf_fehler, pdf

    def __call__(self, anfrage, timeout=None):
        self.anfragen.append(anfrage)
        url = anfrage.full_url
        if url.endswith("/user"):
            return Antwort({"access_token": "TOKEN", "walletBalance": 10000})
        if "/app/catalog" in url:
            return Antwort({"pageFormats": self.formate})
        if "/app/shoppingcart/png?validate=true" in url:
            return Antwort({"link": "https://internetmarke.example/vorschau.png"})
        if "/app/shoppingcart/pdf" in url:
            if self.kauf_fehler:
                raise self.kauf_fehler
            return Antwort({"link": "https://internetmarke.example/marke.pdf", "walletBallance": 9905,
                            "shoppingCart": {"shopOrderId": "4711", "voucherList": [{"voucherId": "A0011B2C3D"}]}})
        if url.endswith("/app/retoure"):
            return Antwort({"shopRetoureId": "R1"})
        if url == "https://internetmarke.example/marke.pdf":
            return Antwort(self.pdf)
        raise AssertionError(f"unerwartete Anfrage {url}")

    def koerper(self, teil):
        return json.loads(next(a for a in self.anfragen if teil in a.full_url).data)


@override_settings(SECURE_SSL_REDIRECT=False)
class InternetmarkeTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer", strasse="Hauptstr. 12a", plz="42651", ort="Solingen")
        self.kunde = Kunde.objects.create(nummer="1", name1="Muster GmbH", strasse="Weg 5", plz="10115", ort="Berlin")
        self.b = Beleg.objects.create(mandant=self.m, art=Belegart.RECHNUNG, kunde=self.kunde,
                                      versandanschrift="Muster GmbH\nz. H. Frau Meier\nWeg 5\n10115 Berlin")
        self.standard = Briefprodukt.objects.get(produktcode=1)  # aus der Migration
        self.gross = Briefprodukt.objects.get(produktcode=21)
        self.user = get_user_model().objects.create_user("t", password="x" * 14, is_staff=True)
        self.client.force_login(self.user)
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()
        self.url = reverse("verkauf:beleg_versand", args=[self.b.pk])

    def kaufen(self, post, produkt=None):
        with mock.patch.dict("os.environ", ENV), mock.patch("urllib.request.urlopen", side_effect=post):
            return self.client.post(self.url, {"aktion": "brief", "briefprodukt": (produkt or self.standard).pk}, follow=True)

    def test_standardprodukte_aus_migration(self):
        self.assertEqual((self.standard.name, self.standard.preis, self.standard.max_gewicht_g), ("Standardbrief", Decimal("0.95"), 20))
        self.assertEqual((self.gross.name, self.gross.preis, self.gross.max_gewicht_g), ("Großbrief", Decimal("1.80"), 500))

    def test_marke_kaufen_mit_dymo_format_und_adressen(self):
        post = PostNachbildung()
        r = self.kaufen(post)
        self.assertContains(r, "Briefmarke gekauft")
        token = next(a for a in post.anfragen if a.full_url.endswith("/user"))
        self.assertIn(b"grant_type=client_credentials", token.data)
        kauf = post.koerper("/app/shoppingcart/pdf")
        position = kauf["positions"][0]
        self.assertEqual((kauf["total"], kauf["pageFormatId"], position["productCode"], position["voucherLayout"]),
                         (95, 42, 1, "ADDRESS_ZONE"))
        self.assertEqual(position["address"]["receiver"], {"name": "Muster GmbH", "additionalName": "z. H. Frau Meier",
                                                           "addressLine1": "Weg 5", "postalCode": "10115", "city": "Berlin", "country": "DEU"})
        self.assertEqual(position["address"]["sender"]["addressLine1"], "Hauptstr. 12a")
        v = self.b.versendungen.get()
        self.assertEqual((v.dienst, v.porto, v.shop_order_id, v.sendungsnummer, bytes(v.label)),
                         ("dp", Decimal("0.95"), "4711", "A0011B2C3D", PDF))
        self.assertEqual(v.bezeichnung, "Standardbrief")
        self.assertEqual(v.tracking_url, "")  # Brief ohne Sendungsverfolgung
        self.assertEqual(self.client.get(reverse("verkauf:versand_aufkleber", args=[self.b.pk, v.pk])).content, PDF)

    def test_doppelklick_kauft_nicht_zweimal(self):
        self.kaufen(PostNachbildung())
        r = self.kaufen(PostNachbildung())
        self.assertContains(r, "Doppelklick")
        self.assertEqual(self.b.versendungen.count(), 1)

    def test_kauf_abgelehnt_legt_nichts_an(self):
        fehler = urllib.error.HTTPError("u", 400, "Bad", {}, None)
        fehler.read = lambda: json.dumps({"title": "invalidTotal", "detail": "total does not match"}).encode()
        r = self.kaufen(PostNachbildung(kauf_fehler=fehler))
        self.assertContains(r, "Verwaltung → Briefprodukte")  # Hinweis aufs Porto
        self.assertEqual(self.b.versendungen.count(), 0)

    def test_etikett_fehlt_marke_bleibt_erhalten_und_wird_nachgeladen(self):
        r = self.kaufen(PostNachbildung(pdf=b"<html>Fehler</html>"))
        self.assertContains(r, "Etikett noch nicht geladen")
        v = self.b.versendungen.get()
        self.assertEqual((v.shop_order_id, v.label), ("4711", None))
        with mock.patch.dict("os.environ", ENV), mock.patch("urllib.request.urlopen", side_effect=PostNachbildung()):
            antwort = self.client.get(reverse("verkauf:versand_aufkleber", args=[self.b.pk, v.pk]))
        self.assertEqual(antwort.content, PDF)

    def test_erstatten_statt_loeschen(self):
        self.kaufen(PostNachbildung())
        v = self.b.versendungen.get()
        self.client.post(self.url, {"aktion": "loeschen", "versand": v.pk})
        self.assertTrue(Versand.objects.filter(pk=v.pk).exists())  # gekaufte Marke wird nicht gelöscht
        post = PostNachbildung()
        with mock.patch.dict("os.environ", ENV), mock.patch("urllib.request.urlopen", side_effect=post):
            self.client.post(self.url, {"aktion": "erstatten", "versand": v.pk})
        self.assertEqual(post.koerper("/app/retoure"), {"shoppingCart": {"shopOrderId": "4711", "voucherList": [{"voucherId": "A0011B2C3D"}]}})
        v.refresh_from_db()
        self.assertIsNotNone(v.erstattung_beantragt)

    def test_ausland_und_fehlende_zugangsdaten(self):
        self.b.versandanschrift = "Hans\nRue 3\n1000 Bruxelles\nBE"
        self.b.save()
        post = PostNachbildung()
        r = self.kaufen(post)
        self.assertContains(r, "nur innerhalb Deutschlands")
        self.assertEqual(post.anfragen, [])  # nichts an die Post geschickt
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesMessage(im.PostFehler, "DP_PORTOKASSE_BENUTZER_OKAY"):
                im.zugang(self.m)

    def test_format_nicht_gefunden_nennt_etikettenformate(self):
        post = PostNachbildung(formate=[{"id": 3, "name": "Brother 62 mm", "pageType": "LABELPRINTER",
                                         "pageLayout": {"size": {"x": 62, "y": 100}}}])
        r = self.kaufen(post)
        self.assertContains(r, "3: Brother 62 mm")
        self.assertEqual(self.b.versendungen.count(), 0)

    def test_antwort_verloren_kauf_unklar_sperrt_weiteren_kauf(self):
        r = self.kaufen(PostNachbildung(kauf_fehler=TimeoutError("timed out")))
        self.assertContains(r, "in der Portokasse prüfen")
        v = self.b.versendungen.get()
        self.assertEqual(v.kauf_status, Versand.UNKLAR)  # Eintrag bleibt – evtl. wurde bezahlt
        Versand.objects.filter(pk=v.pk).update(angelegt_am=v.angelegt_am - timedelta(minutes=10))
        post = PostNachbildung()
        r = self.kaufen(post)
        self.assertContains(r, "früherer Briefmarkenkauf zu diesem Beleg ist unklar")
        self.assertFalse(any("shoppingcart" in a.full_url for a in post.anfragen))  # kein zweiter Kauf
        self.client.post(self.url, {"aktion": "loeschen", "versand": v.pk})  # nach Prüfung in der Portokasse löschbar
        self.assertFalse(Versand.objects.filter(pk=v.pk).exists())

    def test_laufender_kauf_sperrt_parallele_anfrage(self):
        Versand.objects.create(beleg=self.b, dienst="dp", briefprodukt=self.standard, kauf_status=Versand.LAEUFT)
        Versand.objects.update(angelegt_am=timezone.now() - timedelta(minutes=5))
        post = PostNachbildung()
        r = self.kaufen(post)
        self.assertContains(r, "läuft gerade ein Briefmarkenkauf")
        self.assertEqual(post.anfragen, [])

    def test_beleg_mit_briefmarke_nicht_loeschbar(self):
        entwurf = Beleg.objects.create(mandant=self.m, art=Belegart.ANGEBOT, kunde=self.kunde,
                                       versandanschrift="Muster GmbH\nWeg 5\n10115 Berlin")
        self.assertIsNone(entwurf.loeschhindernis())
        Versand.objects.create(beleg=entwurf, dienst="dp", briefprodukt=self.standard, shop_order_id="1")
        self.assertIn("Briefmarke", entwurf.loeschhindernis())
        Beleg.objects.filter(pk=entwurf.pk).update(angelegt_am=timezone.now() - timedelta(hours=1))
        Beleg.leere_entwuerfe_entfernen(self.m)  # leer, aber mit Versand: bleibt
        self.assertTrue(Beleg.objects.filter(pk=entwurf.pk).exists())

    def test_anschriften_grenzfaelle(self):
        self.b.versandanschrift = "nur eine Zeile"
        self.b.save()
        with self.assertRaises(im.PostFehler):  # kein 500
            im.adressen(self.b)
        self.b.versandanschrift = "Firma\nz. H. Herrn Müller\nEinkauf\n" + "Sehr-lange-Straßenbezeichnung-" * 2 + " 12b\n10115 Berlin"
        self.b.save()
        empf = im.adressen(self.b)["receiver"]
        self.assertTrue(empf["addressLine1"].endswith(" 12b"))  # Hausnummer bleibt trotz Kürzung
        self.assertLessEqual(len(empf["addressLine1"]), 50)
        self.assertEqual(empf["additionalName"], "z. H. Herrn Müller, Einkauf")
        self.m.plz = "4265"
        self.m.save()
        with self.assertRaisesMessage(im.PostFehler, "Absender-PLZ"):
            im.adressen(self.b)

    def test_versandmail_brief_ohne_sendungsnummer(self):
        from .versand import versandmail_text
        self.kaufen(PostNachbildung())
        _, text = versandmail_text(self.b.versendungen.get())
        self.assertNotIn("Sendungsnummer", text)
        self.assertIn("Deutsche Post Brief", text)

    def test_pruefen_kauft_nichts(self):
        post = PostNachbildung()
        with mock.patch.dict("os.environ", ENV), mock.patch("urllib.request.urlopen", side_effect=post):
            zeilen = im.pruefen(self.m)
        self.assertEqual(zeilen[0], "Anmeldung ok, Guthaben Portokasse: 100.00 €")
        self.assertIn("Druckformat für DYMO 99014: ID 42", zeilen)
        vorschau = [a for a in post.anfragen if "shoppingcart" in a.full_url]
        self.assertTrue(vorschau and all("validate=true" in a.full_url for a in vorschau))  # nur Vorschau
        self.assertFalse(any("directCheckout" in a.full_url for a in post.anfragen))

    def test_vorschlag_nach_gewicht(self):
        self.assertEqual(Briefprodukt.vorschlag(Decimal("0.02")), self.standard)
        self.assertEqual(Briefprodukt.vorschlag(Decimal("0.30")), self.gross)
        self.assertIsNone(Briefprodukt.vorschlag(Decimal("0.80")))  # zu schwer für einen Brief
        self.assertEqual(Briefprodukt.vorschlag(None), self.standard)
        self.assertEqual(str(self.gross), "Großbrief (1,80 €)")
