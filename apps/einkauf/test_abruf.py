import base64
import io
import urllib.error
from unittest import mock

from django.core.exceptions import ValidationError
from django.test import TestCase

from apps.stammdaten.models import Lieferant

from . import preislisten
from .models import Preislistenprofil

URL = "https://pricelist.api.de/pricelist/?cid=129017&shop=API&action=FULL&opt[]=SPEAKING_AVAILABILITY_TAGS"


class AntwortAttrappe(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class UrlAbrufTest(TestCase):
    def profil(self, **kw):
        lf = Lieferant.objects.create(nummer="L1", name1="API GmbH")
        daten = dict(lieferant=lf, name="Api", quelle="url", adresse=URL, benutzer="129017", sp_bestellnummer="1", sp_ek="2")
        daten.update(kw)
        return Preislistenprofil.objects.create(**daten)

    def test_abruf_mit_basic_auth(self):
        p = self.profil()
        with mock.patch.dict("os.environ", {p.passwort_variable: "geheim"}), \
                mock.patch("urllib.request.urlopen", return_value=AntwortAttrappe(b"a;b\n1;2\n")) as m:
            name, inhalt = preislisten.abrufen(p)
        anfrage = m.call_args.args[0]
        self.assertEqual(anfrage.full_url, URL)
        self.assertEqual(anfrage.get_header("Authorization"), "Basic " + base64.b64encode(b"129017:geheim").decode())
        self.assertEqual((inhalt, name), (b"a;b\n1;2\n", "pricelist"))

    def test_adresse_im_pfadfeld_wird_toleriert(self):
        p = self.profil(adresse="", pfad=URL)
        with mock.patch.dict("os.environ", {p.passwort_variable: "x"}), \
                mock.patch("urllib.request.urlopen", return_value=AntwortAttrappe(b"x")) as m:
            preislisten.abrufen(p)
        self.assertEqual(m.call_args.args[0].full_url, URL)

    def test_adresse_mit_schluessel_aus_env(self):
        p = self.profil(adresse="", benutzer="")
        geheim = "https://data.kosatec.de/26463/abc123/preisliste.csv"
        with mock.patch.dict("os.environ", {p.url_variable: geheim}),                 mock.patch("urllib.request.urlopen", return_value=AntwortAttrappe(b"x")) as m:
            p.clean()  # Formularprüfung akzeptiert die Adresse aus der .env
            name, _ = preislisten.abrufen(p)
        self.assertEqual((m.call_args.args[0].full_url, name), (geheim, "preisliste.csv"))

    def test_klare_fehlermeldungen(self):
        p = self.profil()
        with self.assertRaisesMessage(preislisten.PreislistenFehler, p.passwort_variable):  # Passwort fehlt
            preislisten.abrufen(p)
        with mock.patch.dict("os.environ", {p.passwort_variable: "falsch"}), \
                mock.patch("urllib.request.urlopen", side_effect=urllib.error.HTTPError(URL, 401, "Unauthorized", {}, None)):
            with self.assertRaisesMessage(preislisten.PreislistenFehler, "401"):
                preislisten.abrufen(p)
        p.adresse = p.pfad = ""
        with self.assertRaisesMessage(preislisten.PreislistenFehler, "vollständige Adresse"):
            preislisten.abrufen(p)

    def test_formularpruefung(self):
        p = self.profil(adresse="", pfad="")
        with self.assertRaises(ValidationError):
            p.full_clean()
        p.pfad = URL
        p.clean()  # kein Fehler
