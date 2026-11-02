"""USt-ID online prüfen: Wahl des Dienstes, Antworten von BZSt und VIES, Nachweis, Maske."""
import io
import json
import urllib.error
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.kern.models import AenderungsProtokoll, Mandant

from . import ustid
from .models import Kunde, Lieferant, UstIdPruefung


class Antwort(io.BytesIO):
    def __init__(self, inhalt):
        super().__init__(json.dumps(inhalt).encode())


def http_fehler(code, inhalt):
    return urllib.error.HTTPError("https://x.test", code, "Fehler", {}, io.BytesIO(json.dumps(inhalt).encode()))


def anfrage_von(aufruf) -> tuple:
    r = aufruf.call_args_list[-1].args[0]
    return r.full_url, json.loads(r.data)


def m_mit(ust_id="DE123456789"):
    return Mandant(kurzname="OKAY", firmenname="OKAY Computer", ust_id=ust_id)


class NormalisierenTest(SimpleTestCase):
    def test_schreibweisen(self):
        for eingabe, land, soll in (("de 123.456-789", "", "DE123456789"), ("123456789", "de", "DE123456789"), ("U12345678", "AT", "ATU12345678"),
                                    ("GR123456789", "", "EL123456789"), ("123456789", "GR", "EL123456789"), ("nl123456789b01", "", "NL123456789B01"),
                                    ("", "DE", ""), (None, "DE", "")):
            self.assertEqual(ustid.normalisieren(eingabe, land), soll)


class PruefenTest(SimpleTestCase):
    def test_ausland_geht_qualifiziert_ans_bzst(self):
        antwort = {"id": "abc", "status": "evatr-0000", "ergFirmenname": "A", "ergStrasse": "B", "ergPlz": "C", "ergOrt": "D"}
        with mock.patch("urllib.request.urlopen", return_value=Antwort(antwort)) as aufruf:
            e = ustid.pruefen(m_mit(), "ATU12345678", "AT", "Muster GmbH", "Weg 1", "", "Wien")
        url, daten = anfrage_von(aufruf)
        self.assertEqual(url, ustid.BZST)
        self.assertEqual(daten, {"anfragendeUstid": "DE123456789", "angefragteUstid": "ATU12345678", "firmenname": "Muster GmbH", "ort": "Wien", "strasse": "Weg 1"})
        self.assertTrue(e["gueltig"])
        self.assertEqual((e["dienst"], e["referenz"], e["erg_name"], e["erg_strasse"], e["erg_plz"], e["erg_ort"]), ("bzst", "abc", "A", "B", "C", "D"))

    def test_ohne_name_oder_ort_einfache_abfrage(self):
        for name, ort in (("Muster GmbH", ""), ("", "Wien")):
            with mock.patch("urllib.request.urlopen", return_value=Antwort({"status": "evatr-0000"})) as aufruf:
                ustid.pruefen(m_mit(), "ATU12345678", firmenname=name, strasse="Weg 1", ort=ort)
            self.assertEqual(anfrage_von(aufruf)[1], {"anfragendeUstid": "DE123456789", "angefragteUstid": "ATU12345678"})

    def test_bzst_ungueltig_auch_mit_http_fehler(self):
        for code, inhalt, text in ((404, {"status": "evatr-2001"}, "nicht vergeben"), (400, {"status": "evatr-0012"}, "falsches Format"),
                                   (400, {"status": "evatr-2003"}, "falsches Format"),
                                   (200, {"status": "evatr-2002", "gueltigAb": "2027-01-15"}, "erst ab 15.01.2027"),
                                   (200, {"status": "evatr-2006", "gueltigAb": "2020-01-01", "gueltigBis": "2025-06-30"}, "von 01.01.2020 bis 30.06.2025")):
            nebenwirkung = http_fehler(code, inhalt) if code != 200 else None
            rueckgabe = Antwort(inhalt) if code == 200 else None
            with mock.patch("urllib.request.urlopen", side_effect=nebenwirkung, return_value=rueckgabe):
                e = ustid.pruefen(m_mit(), "ATU12345678")
            self.assertFalse(e["gueltig"])
            self.assertIn(text, e["meldung"])

    def test_gueltig_mit_besonderheit(self):
        with mock.patch("urllib.request.urlopen", return_value=Antwort({"status": "evatr-2008"})):
            e = ustid.pruefen(m_mit(), "ATU12345678")
        self.assertTrue(e["gueltig"])
        self.assertIn("Besonderheit", e["meldung"])

    def test_grenze_qualifizierter_abfragen_fragt_einfach_nach(self):
        folge = [http_fehler(403, {"status": "evatr-0008"}), Antwort({"status": "evatr-0000"})]
        with mock.patch("urllib.request.urlopen", side_effect=folge) as aufruf:
            e = ustid.pruefen(m_mit(), "ATU12345678", firmenname="Muster GmbH", ort="Wien")
        self.assertEqual(aufruf.call_count, 2)
        self.assertNotIn("firmenname", anfrage_von(aufruf)[1])
        self.assertTrue(e["gueltig"])
        self.assertIn("nicht abgeglichen", e["meldung"])
        self.assertEqual((e["firmenname"], e["ort"]), ("", ""))

    def test_keine_aussage_bei_bzst_stoerung(self):
        for fehler in (http_fehler(503, {"status": "evatr-1001"}), http_fehler(500, {"status": "evatr-2011"}), http_fehler(403, {"status": "evatr-0007"}),
                       http_fehler(502, {}), urllib.error.URLError("weg"), TimeoutError("zu lange")):
            with mock.patch("urllib.request.urlopen", side_effect=fehler):
                with self.assertRaisesMessage(ustid.UstIdFehler, "nicht erreichbar"):
                    ustid.pruefen(m_mit(), "ATU12345678")

    def test_kaputte_antwort(self):
        for kaputt in (io.BytesIO(b"<html>"), io.BytesIO(b"[1]")):
            with mock.patch("urllib.request.urlopen", return_value=kaputt):
                with self.assertRaisesMessage(ustid.UstIdFehler, "nicht erreichbar"):
                    ustid.pruefen(m_mit(), "ATU12345678")

    def test_eigene_nummer_abgelehnt(self):
        for status in ("evatr-0004", "evatr-0006", "evatr-2005"):
            with mock.patch("urllib.request.urlopen", side_effect=http_fehler(404, {"status": status})):
                with self.assertRaisesMessage(ustid.UstIdFehler, "eigene USt-ID"):
                    ustid.pruefen(m_mit(), "ATU12345678")

    def test_deutsch_geht_an_vies(self):
        antwort = {"valid": True, "requestIdentifier": "WAPI1", "name": "---", "address": "---"}
        with mock.patch("urllib.request.urlopen", return_value=Antwort(antwort)) as aufruf:
            e = ustid.pruefen(m_mit(), "de 987654321", firmenname="Muster GmbH", ort="Solingen")
        url, daten = anfrage_von(aufruf)
        self.assertEqual(url, ustid.VIES)
        self.assertEqual(daten, {"countryCode": "DE", "vatNumber": "987654321", "requesterMemberStateCode": "DE", "requesterNumber": "123456789"})
        self.assertEqual((e["ust_id"], e["dienst"], e["gueltig"], e["referenz"], e["auskunft"], e["anfragende_ust_id"]),
                         ("DE987654321", "vies", True, "WAPI1", "", "DE123456789"))

    def test_deutsch_geht_auch_ohne_eigene_nummer(self):
        with mock.patch("urllib.request.urlopen", return_value=Antwort({"valid": False, "name": "---", "address": "---"})) as aufruf:
            e = ustid.pruefen(m_mit(""), "DE987654321")
        self.assertEqual(anfrage_von(aufruf)[1], {"countryCode": "DE", "vatNumber": "987654321"})
        self.assertFalse(e["gueltig"])
        self.assertEqual(e["anfragende_ust_id"], "")

    def test_vies_stoerungen(self):
        for inhalt, text in (({"actionSucceed": False, "errorWrappers": [{"error": "MS_UNAVAILABLE"}]}, "nicht erreichbar"),
                             ({"actionSucceed": False, "errorWrappers": [{"error": "INVALID_INPUT"}]}, "Schreibweise"),
                             ({"actionSucceed": False, "errorWrappers": [{"error": "INVALID_REQUESTER_INFO"}]}, "eigene USt-ID"),
                             ({"userError": "MS_UNAVAILABLE"}, "nicht erreichbar"), ({"valid": "true"}, "nicht erreichbar")):
            with mock.patch("urllib.request.urlopen", return_value=Antwort(inhalt)):
                with self.assertRaisesMessage(ustid.UstIdFehler, text):
                    ustid.pruefen(m_mit(), "DE987654321")

    def test_vor_der_abfrage_abgelehnt(self):
        with mock.patch("urllib.request.urlopen") as aufruf:
            for ust, land, m, text in (("", "DE", m_mit(), "zuerst"), ("123", "", m_mit(), "Ländercode"), ("CHE123456789", "", m_mit(), "EU"),
                                       ("GB123456789", "", m_mit(), "EU"), ("ATU12345678", "", m_mit(""), "fehlt die eigene"),
                                       ("ATU12345678", "", m_mit("12345"), "fehlt die eigene"), ("AT<script>", "", m_mit(), "Ländercode")):
                with self.assertRaisesMessage(ustid.UstIdFehler, text):
                    ustid.pruefen(m, ust, land)
        aufruf.assert_not_called()

    def test_griechenland_und_nordirland(self):
        for eingabe, soll in (("GR123456789", "EL123456789"), ("XI123456789", "XI123456789")):
            with mock.patch("urllib.request.urlopen", return_value=Antwort({"status": "evatr-0000"})) as aufruf:
                ustid.pruefen(m_mit(), eingabe)
            self.assertEqual(anfrage_von(aufruf)[1]["angefragteUstid"], soll)


@override_settings(SECURE_SSL_REDIRECT=False)
class MaskeTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer", ust_id="DE123456789")
        self.m2 = Mandant.objects.create(kurzname="ZONE", firmenname="ZONE Handel", ust_id="DE111111111")
        self.kunde = Kunde.objects.create(nummer="1000", name1="Muster GmbH", ort="Wien", land="AT")
        self.lieferant = Lieferant.objects.create(nummer="L1", name1="API GmbH", kurzname="API")
        self.user = get_user_model().objects.create_user("t", password="x" * 14)
        self.client.force_login(self.user)
        self.mandant_setzen(self.m)

    def mandant_setzen(self, m):
        s = self.client.session
        s["mandant_id"] = m.pk
        s.save()

    def url(self, art="kunde", pk=None):
        return reverse(f"stammdaten:{art}_ust_id_pruefen", args=[pk or (self.kunde if art == "kunde" else self.lieferant).pk])

    def pruefen(self, antwort, art="kunde", **felder):
        daten = {"ust_id": "atu12345678", "land": "AT", "name1": "Geänderter Name", "strasse": "Weg 1", "plz": "1010", "ort": "Wien", **felder}
        with mock.patch("urllib.request.urlopen", return_value=Antwort(antwort)) as self.aufruf:
            return self.client.post(self.url(art), daten)

    def test_gueltig_legt_nachweis_an_und_nimmt_die_eingaben_der_maske(self):
        d = self.pruefen({"id": "abc", "status": "evatr-0000", "ergFirmenname": "B", "ergOrt": "A", "ergStrasse": "A", "ergPlz": "C"}).json()
        self.assertTrue(d["ok"] and d["gueltig"] and d["abweichung"])
        self.assertEqual(d["ust_id"], "ATU12345678")
        self.assertEqual(d["datum"], timezone.localdate().isoformat())
        self.assertEqual(d["abgleich"], ["Name: weicht ab", "Straße: stimmt", "Ort: stimmt"])
        p = UstIdPruefung.objects.get()
        self.assertEqual((p.mandant, p.kunde, p.lieferant, p.benutzer, p.firmenname, p.anfragende_ust_id, p.referenz),
                         (self.m, self.kunde, None, self.user, "Geänderter Name", "DE123456789", "abc"))
        self.assertEqual(anfrage_von(self.aufruf)[1]["firmenname"], "Geänderter Name")
        self.assertTrue(AenderungsProtokoll.objects.filter(tabelle="stammdaten.UstIdPruefung", aktion="anlegen").exists())

    def test_das_datum_kommt_erst_mit_speichern(self):
        self.pruefen({"status": "evatr-0000"})
        self.kunde.refresh_from_db()
        self.assertIsNone(self.kunde.ust_id_geprueft_am)
        self.assertEqual(self.kunde.ust_id, "")

    def test_ungueltig_gibt_auch_einen_nachweis(self):
        with mock.patch("urllib.request.urlopen", side_effect=http_fehler(404, {"status": "evatr-2001"})):
            d = self.client.post(self.url(), {"ust_id": "ATU12345678", "name1": "X", "ort": "Y"}).json()
        self.assertTrue(d["ok"])
        self.assertFalse(d["gueltig"])
        self.assertFalse(UstIdPruefung.objects.get().gueltig)

    def test_lieferant(self):
        d = self.pruefen({"valid": True, "name": "---", "address": "---"}, art="lieferant", ust_id="DE987654321").json()
        self.assertTrue(d["gueltig"])
        p = UstIdPruefung.objects.get()
        self.assertEqual((p.lieferant, p.kunde, p.dienst), (self.lieferant, None, "vies"))

    def test_stoerung_ohne_nachweis(self):
        with mock.patch("urllib.request.urlopen", side_effect=TimeoutError("zu lange")):
            r = self.client.post(self.url(), {"ust_id": "ATU12345678"})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["ok"])
        self.assertFalse(UstIdPruefung.objects.exists())

    def test_ohne_ust_id_wird_nichts_gesendet(self):
        with mock.patch("urllib.request.urlopen") as aufruf:
            self.assertIn("zuerst", self.client.post(self.url(), {"ust_id": " "}).json()["text"])
        aufruf.assert_not_called()

    def test_land_kommt_notfalls_vom_partner(self):
        self.pruefen({"status": "evatr-0000"}, ust_id="U12345678", land="")
        self.assertEqual(UstIdPruefung.objects.get().ust_id, "ATU12345678")

    def test_lange_eingaben_passen_in_den_nachweis(self):
        self.pruefen({"status": "evatr-0000"}, name1="x" * 400, strasse="y" * 400, plz="1" * 40, ort="z" * 400)
        p = UstIdPruefung.objects.get()
        self.assertEqual((len(p.firmenname), len(p.strasse), len(p.plz), len(p.ort)), (150, 200, 10, 100))

    def test_name_2_gehoert_zum_firmennamen(self):
        self.pruefen({"status": "evatr-0000"}, name1="Musterhaus GmbH", name2="& Co KG")
        self.assertEqual(anfrage_von(self.aufruf)[1]["firmenname"], "Musterhaus GmbH & Co KG")

    def test_nachweis_enthaelt_nur_gesendete_daten(self):
        self.pruefen({"valid": True, "name": "---", "address": "---"}, ust_id="DE987654321")
        self.pruefen({"status": "evatr-0000"}, ort="")
        for p in UstIdPruefung.objects.all():
            self.assertEqual((p.firmenname, p.strasse, p.plz, p.ort), ("", "", "", ""))

    def test_nachweis_ist_unveraenderlich(self):
        self.pruefen({"status": "evatr-0000"})
        p = UstIdPruefung.objects.get()
        p.gueltig = False
        with self.assertRaises(PermissionError):
            p.save()
        with self.assertRaises(PermissionError):
            p.delete()

    def test_zugriff(self):
        self.assertEqual(self.client.get(self.url()).status_code, 405)
        self.assertEqual(self.client.get(self.url("lieferant")).status_code, 405)
        self.assertEqual(self.client.post(self.url(pk=99999), {"ust_id": "ATU12345678"}).status_code, 404)
        self.assertEqual(self.client.post(self.url("lieferant", 99999), {"ust_id": "ATU12345678"}).status_code, 404)
        self.client.logout()
        with mock.patch("urllib.request.urlopen") as aufruf:
            self.assertEqual(self.client.post(self.url(), {"ust_id": "ATU12345678"}).status_code, 302)
            self.assertEqual(self.client.post(self.url("lieferant"), {"ust_id": "ATU12345678"}).status_code, 302)
        aufruf.assert_not_called()

    def test_csrf_ist_pflicht(self):
        streng = Client(enforce_csrf_checks=True)
        streng.force_login(self.user)
        s = streng.session
        s["mandant_id"] = self.m.pk
        s.save()
        with mock.patch("urllib.request.urlopen") as aufruf:
            self.assertEqual(streng.post(self.url(), {"ust_id": "ATU12345678"}).status_code, 403)
        aufruf.assert_not_called()

    def test_knopf_bleibt_nach_eingabefehler(self):
        r = self.client.post(reverse("stammdaten:lieferant_bearbeiten", args=[self.lieferant.pk]), {"name1": ""})
        self.assertEqual(r.status_code, 422)
        self.assertContains(r, "data-ustpruefen", status_code=422)

    def test_maske_zeigt_knopf_und_nur_prüfungen_des_mandanten(self):
        self.pruefen({"status": "evatr-0000", "ergFirmenname": "A"})
        seite = self.client.get(reverse("stammdaten:kunde_bearbeiten", args=[self.kunde.pk]))
        self.assertContains(seite, f'data-ustpruefen="{self.url()}"')
        self.assertContains(seite, "ATU12345678 · BZSt · gültig · Name: stimmt")
        self.mandant_setzen(self.m2)
        seite = self.client.get(reverse("stammdaten:kunde_bearbeiten", args=[self.kunde.pk]))
        self.assertNotContains(seite, "ATU12345678")

    def test_maske_lieferant_hat_knopf(self):
        self.assertContains(self.client.get(reverse("stammdaten:lieferant_bearbeiten", args=[self.lieferant.pk])), f'data-ustpruefen="{self.url("lieferant")}"')

    def test_neuanlage_ohne_knopf(self):
        for name in ("stammdaten:kunde_neu", "stammdaten:lieferant_neu"):
            seite = self.client.get(reverse(name))
            self.assertNotContains(seite, "data-ustpruefen")
            self.assertContains(seite, "nach dem ersten Speichern")

    def test_nachweis_bleibt_nach_loeschen_des_kunden(self):
        self.pruefen({"status": "evatr-0000"})
        self.kunde.delete()
        p = UstIdPruefung.objects.get()
        self.assertIsNone(p.kunde)
        self.assertEqual(p.firmenname, "Geänderter Name")

    def test_verwaltung_nur_lesen(self):
        self.pruefen({"status": "evatr-0000"})
        chef = get_user_model().objects.create_superuser("chef", password="x" * 14)
        self.client.force_login(chef)
        pk = UstIdPruefung.objects.get().pk
        self.assertEqual(self.client.get(reverse("admin:stammdaten_ustidpruefung_changelist")).status_code, 200)
        self.assertEqual(self.client.get(reverse("admin:stammdaten_ustidpruefung_add")).status_code, 403)
        self.assertEqual(self.client.post(reverse("admin:stammdaten_ustidpruefung_delete", args=[pk]), {"post": "yes"}).status_code, 403)
