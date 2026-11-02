"""Laufende Nummern: durchlaufende Belegnummern, Einstellen in der Verwaltung, Zähler der Stammdaten."""
import html
import importlib
import json
import re
from datetime import date
from decimal import Decimal
from unittest import mock

from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.kern.models import AenderungsProtokoll, Mandant
from apps.stammdaten.models import Artikel, Artikeltyp, Kunde, Lieferant, Stammnummer, Steuersatz

from .models import Beleg, Belegart, Nummernkreis

URL = "admin:verkauf_nummernkreis_changelist"


class Basis(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.m2 = Mandant.objects.create(kurzname="ZONE", firmenname="ZONE Handel")
        self.kunde = Kunde.objects.create(nummer="1000", name1="Muster GmbH")

    def beleg(self, art, nummer, m=None):
        return Beleg.objects.bulk_create([Beleg(mandant=m or self.m, art=art, kunde=self.kunde, nummer=nummer)])[0]


class ZaehlerTest(Basis):
    def test_laeuft_ueber_den_jahreswechsel_weiter(self):
        self.assertEqual(Nummernkreis.naechste(self.m, "RE", 2026), "RE2026-00001")
        self.assertEqual(Nummernkreis.naechste(self.m, "RE", 2026), "RE2026-00002")
        self.assertEqual(Nummernkreis.naechste(self.m, "RE", 2027), "RE2027-00003")

    def test_mandanten_und_arten_sind_getrennt(self):
        Nummernkreis.naechste(self.m, "RE", 2026)
        self.assertEqual(Nummernkreis.naechste(self.m2, "RE", 2026), "RE2026-00001")
        self.assertEqual(Nummernkreis.naechste(self.m, "GS", 2026), "GS2026-00001")

    def test_freigeben_nur_die_letzte(self):
        Nummernkreis.naechste(self.m, "RE", 2026)
        n = Nummernkreis.naechste(self.m, "RE", 2026)
        Nummernkreis.freigeben(self.m, "RE", "RE2026-00001")
        self.assertEqual(Nummernkreis.objects.get().letzte_nummer, 2)
        Nummernkreis.freigeben(self.m, "RE", n)
        self.assertEqual(Nummernkreis.objects.get().letzte_nummer, 1)
        Nummernkreis.freigeben(self.m, "RE", "Unsinn")
        self.assertEqual(Nummernkreis.objects.get().letzte_nummer, 1)

    def test_setzen_nie_unter_die_hoechste_vergebene(self):
        self.beleg("LS", "LS2025-00040")
        with self.assertRaisesMessage(ValueError, "höchste vergebene (40)"):
            Nummernkreis.naechste_setzen(self.m, "LS", 40)
        Nummernkreis.naechste_setzen(self.m, "LS", 41)
        self.assertEqual(Nummernkreis.naechste(self.m, "LS", 2026), "LS2026-00041")

    def test_zu_hoch_gesetzter_zaehler_laesst_sich_korrigieren(self):
        Nummernkreis.naechste_setzen(self.m, "AN", 5000)
        Nummernkreis.naechste_setzen(self.m, "AN", 10)
        self.assertEqual(Nummernkreis.naechste(self.m, "AN", 2026), "AN2026-00010")

    def test_rechnung_nur_vor_dem_start(self):
        Nummernkreis.naechste_setzen(self.m, "RE", 500)
        self.beleg("RE", f"RE{date.today().year}-00500")
        self.assertTrue(Nummernkreis.gesperrt(self.m, "RE"))
        with self.assertRaisesMessage(ValueError, "lückenlos"):
            Nummernkreis.naechste_setzen(self.m, "RE", 900)
        self.assertFalse(Nummernkreis.gesperrt(self.m2, "RE"))
        self.assertFalse(Nummernkreis.gesperrt(self.m, "GS"))

    def test_neues_jahr_gibt_die_rechnung_wieder_frei_aber_nicht_nach_unten(self):
        self.beleg("GS", "GS2020-00007")
        self.assertFalse(Nummernkreis.gesperrt(self.m, "GS"))
        with self.assertRaisesMessage(ValueError, "höchste vergebene (7)"):
            Nummernkreis.naechste_setzen(self.m, "GS", 3)
        Nummernkreis.naechste_setzen(self.m, "GS", 8)

    def test_sperre_gilt_auch_fuer_vorjahresrechnung_im_neuen_jahr(self):
        Nummernkreis.naechste(self.m, "RE", date.today().year - 1)  # Dezember-Entwurf, im Januar abgeschlossen
        self.assertTrue(Nummernkreis.gesperrt(self.m, "RE"))
        self.assertFalse(Nummernkreis.gesperrt(self.m, "RE", date.today().year + 1))

    def test_freigeben_schont_gleichen_zaehler_aus_dem_anderen_jahr(self):
        self.beleg("AN", "AN2026-00001")
        Nummernkreis.objects.create(mandant=self.m, art="AN", letzte_nummer=1)
        Nummernkreis.freigeben(self.m, "AN", "AN2025-00001")
        self.assertEqual(Nummernkreis.objects.get().letzte_nummer, 1)

    def test_bank_findet_sechsstellige_rechnungsnummern(self):
        from apps.finanzen.bank import RECHNUNGSNR
        self.assertEqual(RECHNUNGSNR.findall("Rechnung RE2026-100123"), [("2026", "100123")])

    def test_gleicher_wert_aendert_nichts_auch_wenn_gesperrt(self):
        self.beleg("RE", f"RE{date.today().year}-00001")
        Nummernkreis.objects.create(mandant=self.m, art="RE", letzte_nummer=1)
        Nummernkreis.naechste_setzen(self.m, "RE", 2)

    def test_grenzen(self):
        for wert in (0, -1, 10 ** 9):
            with self.assertRaisesMessage(ValueError, "zwischen 1"):
                Nummernkreis.naechste_setzen(self.m, "AN", wert)

    def test_alle_quellen_sind_lesbar(self):
        for art in Nummernkreis.ARTEN:
            self.assertEqual(Nummernkreis.vergebene(self.m, art), [])

    def test_fremder_mandant_zaehlt_nicht(self):
        self.beleg("LS", "LS2026-00900", self.m2)
        Nummernkreis.naechste_setzen(self.m, "LS", 2)

    def test_migration_behaelt_hoechsten_stand(self):
        modul = importlib.import_module("apps.verkauf.migrations.0027_nummernkreis_durchlaufend")

        class AlteZeile:
            def __init__(self, liste, mandant_id, art, stand):
                self.liste, self.mandant_id, self.art, self.letzte_nummer = liste, mandant_id, art, stand

            def delete(self):
                self.liste.remove(self)

        zeilen = []
        zeilen.extend(AlteZeile(zeilen, *w) for w in ((1, "RE", 9), (1, "RE", 3), (1, "GS", 2), (2, "RE", 1)))
        manager = mock.Mock()
        manager.objects.order_by.return_value = sorted(zeilen, key=lambda z: -z.letzte_nummer)
        modul.zusammenfuehren(mock.Mock(get_model=lambda *a: manager), None)
        manager.objects.order_by.assert_called_once_with("-letzte_nummer", "-jahr", "pk")
        self.assertEqual(sorted((z.mandant_id, z.art, z.letzte_nummer) for z in zeilen), [(1, "GS", 2), (1, "RE", 9), (2, "RE", 1)])


@override_settings(SECURE_SSL_REDIRECT=False)
class VerwaltungTest(Basis):
    def setUp(self):
        super().setUp()
        self.chef = get_user_model().objects.create_superuser("chef", password="x" * 14)
        self.client.force_login(self.chef)

    def senden(self, **felder):
        seite = self.client.get(reverse(URL))
        daten = {}
        for gruppe in seite.context["belege"]:
            for z in gruppe["zellen"]:
                daten[f"alt-{z['name']}"] = z["wert"]
                if not z["gesperrt"]:
                    daten[z["name"]] = z["wert"]
        for z in seite.context["stamm"]:
            daten.update({z["name"]: z["wert"], f"alt-{z['name']}": z["wert"]})
        daten.update(felder)
        return self.client.post(reverse(URL), daten, follow=True)

    def test_seite_zeigt_alle_gewuenschten_nummern(self):
        seite = self.client.get(reverse(URL))
        for titel in ("Rechnungen", "Gutschriften", "Lieferscheine", "Angebote", "Proforma-Rechnungen", "Werkstattaufträge", "Artikel (Typ A)",
                      "Leistungen (Typ L)", "Gruppenartikel (Typ G)", "Kunden", "Lieferanten", "OKAY", "ZONE"):
            self.assertContains(seite, titel)

    def test_einstellen_je_mandant_und_protokoll(self):
        r = self.senden(**{f"beleg-{self.m.pk}-RE": "2001", "stamm-K": "70000"})
        self.assertContains(r, "2 laufende Nummer(n) eingestellt")
        self.assertEqual(Nummernkreis.naechste(self.m, "RE", 2026), "RE2026-02001")
        self.assertEqual(Nummernkreis.naechste(self.m2, "RE", 2026), "RE2026-00001")
        self.assertEqual(Stammnummer.vorschlag("K"), "70000")
        self.assertTrue(AenderungsProtokoll.objects.filter(tabelle="stammdaten.Stammnummer", benutzer=self.chef).exists())

    def test_ohne_aenderung_keine_meldung(self):
        self.assertNotContains(self.senden(), "eingestellt")
        self.assertFalse(Nummernkreis.objects.exists())

    def test_fehler_stehen_ueber_der_seite_und_der_rest_gilt(self):
        self.beleg("LS", "LS2026-00040")
        r = self.senden(**{f"beleg-{self.m.pk}-LS": "12", f"beleg-{self.m.pk}-AN": "abc", f"beleg-{self.m.pk}-PF": "77"})
        self.assertContains(r, "Lieferscheine OKAY: Die Nummer muss größer sein")
        self.assertContains(r, "ist keine Nummer")
        self.assertContains(r, "1 laufende Nummer(n) eingestellt")
        self.assertEqual(Nummernkreis.objects.get(art="PF").letzte_nummer, 76)

    def test_gesperrte_rechnung_ist_ohne_eingabefeld_und_post_wirkt_nicht(self):
        self.beleg("RE", f"RE{date.today().year}-00003")
        Nummernkreis.objects.create(mandant=self.m, art="RE", letzte_nummer=3)
        seite = self.client.get(reverse(URL))
        self.assertContains(seite, "gesperrt, Nummern laufen lückenlos")
        r = self.client.post(reverse(URL), {f"beleg-{self.m.pk}-RE": "900", f"alt-beleg-{self.m.pk}-RE": "4"}, follow=True)
        self.assertContains(r, "lückenlos weiterlaufen")
        self.assertEqual(Nummernkreis.objects.get(art="RE").letzte_nummer, 3)

    def test_veraltete_seite_stellt_nichts_zurueck(self):
        Stammnummer.setzen("K", 100)
        alt = {"stamm-K": "100", "alt-stamm-K": "100"}
        Stammnummer.setzen("K", 102)
        self.assertNotContains(self.client.post(reverse(URL), alt, follow=True), "eingestellt")
        self.assertEqual(Stammnummer.vorschlag("K"), "102")
        r = self.client.post(reverse(URL), {"stamm-K": "500", "alt-stamm-K": "100"}, follow=True)
        self.assertContains(r, "inzwischen geändert (jetzt 102)")
        self.assertEqual(Stammnummer.vorschlag("K"), "102")

    def test_sondereingaben_geben_eine_meldung(self):
        for eingabe in ("²", "1_000", "-5", "１２", "9" * 5000):
            r = self.client.post(reverse(URL), {"stamm-K": eingabe, "alt-stamm-K": Stammnummer.vorschlag("K")}, follow=True)
            self.assertEqual(r.status_code, 200)
            self.assertContains(r, "ist keine Nummer")

    def test_fuehrende_null_ist_keine_aenderung(self):
        Stammnummer.setzen("K", 12)
        self.assertNotContains(self.client.post(reverse(URL), {"stamm-K": "0012", "alt-stamm-K": "12"}, follow=True), "eingestellt")

    def test_kein_freies_bearbeiten(self):
        k = Nummernkreis.objects.create(mandant=self.m, art="LS", letzte_nummer=5)
        self.assertRedirects(self.client.post(reverse("admin:verkauf_nummernkreis_change", args=[k.pk]), {"letzte_nummer": "1"}), reverse(URL))
        self.assertEqual(self.client.get(reverse("admin:verkauf_nummernkreis_add")).status_code, 403)
        self.assertEqual(self.client.get(reverse("admin:verkauf_nummernkreis_delete", args=[k.pk])).status_code, 403)
        k.refresh_from_db()
        self.assertEqual(k.letzte_nummer, 5)

    def test_nur_mit_recht(self):
        mitarbeiter = get_user_model().objects.create_user("m", password="x" * 14, is_staff=True)
        self.client.force_login(mitarbeiter)
        self.assertEqual(self.client.get(reverse(URL)).status_code, 403)
        self.assertEqual(self.client.post(reverse(URL), {"stamm-K": "5"}).status_code, 403)
        self.assertFalse(Stammnummer.objects.filter(art="K", letzte_nummer=4).exists())


@override_settings(SECURE_SSL_REDIRECT=False)
class StammnummerTest(Basis):
    def setUp(self):
        super().setUp()
        self.st = Steuersatz.objects.create(bezeichnung="19", prozent=Decimal("19"))
        self.client.force_login(get_user_model().objects.create_user("t", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    def artikel(self, nummer, typ="A"):
        return Artikel.objects.create(nummer=nummer, typ=typ, bezeichnung="X", steuersatz=self.st)

    def test_jede_art_hat_ihren_zaehler_und_springt_ueber_belegte(self):
        Stammnummer.setzen("A", 100)
        Stammnummer.setzen("L", 100)
        self.artikel("100")
        self.artikel("101", "L")
        self.assertEqual(Stammnummer.vorschlag("A"), "102")
        self.assertEqual(Stammnummer.vorschlag("L"), "102")
        Stammnummer.setzen("G", 900)
        self.assertEqual(Stammnummer.vorschlag("G"), "900")

    def test_vorschlag_verbraucht_nichts(self):
        Stammnummer.vorschlag("LF")
        self.assertEqual(Stammnummer.vorschlag("LF"), "1")

    def test_kunde_anlegen_rueckt_vor_und_geloeschte_nummer_kommt_nicht_wieder(self):
        Stammnummer.setzen("K", 5000)
        k = Kunde.objects.create(nummer=Stammnummer.naechste("K"), name1="A")
        self.assertEqual(k.nummer, "5000")
        k.delete()
        self.assertEqual(Stammnummer.vorschlag("K"), "5001")

    def test_eingetippte_nummer_laesst_den_zaehler_stehen(self):
        Stammnummer.setzen("LF", 10)
        Stammnummer.belegen("LF", "999999")
        self.assertEqual(Stammnummer.vorschlag("LF"), "10")

    def test_setzen_grenzen(self):
        for wert in (0, 10 ** 18):
            with self.assertRaises(ValueError):
                Stammnummer.setzen("K", wert)

    def test_maske_neuer_artikel_zeigt_die_zaehler_und_vergibt_nach_typ(self):
        Stammnummer.setzen("A", 3000)
        Stammnummer.setzen("L", 8000)
        Stammnummer.setzen("G", 9000)
        seite = self.client.get(reverse("stammdaten:artikel_neu"))
        self.assertContains(seite, 'value="3000"')
        roh = re.search(r'data-vorschlaege="([^"]+)"', seite.content.decode()).group(1)
        self.assertEqual(json.loads(html.unescape(roh)), {"A": "3000", "L": "8000", "G": "9000"})
        daten = {"nummer": "8000", "typ": Artikeltyp.L, "bezeichnung": "Stunde", "steuersatz": self.st.pk, "mengeneinheit": "Std", "md-mindestbestand": "", "md-maximalbestand": "", "md-lagerplatz": ""}
        r = self.client.post(reverse("stammdaten:artikel_neu"), daten)
        self.assertEqual(r.status_code, 200, r.content[:400])
        self.assertEqual(Stammnummer.objects.get(art="L").letzte_nummer, 8000)
        self.assertEqual(Stammnummer.objects.get(art="A").letzte_nummer, 2999)

    def test_bestehender_artikel_ohne_vorschlaege(self):
        a = self.artikel("555")
        self.assertNotContains(self.client.get(reverse("stammdaten:artikel_bearbeiten", args=[a.pk])), "data-vorschlaege")

    def test_abgebrochene_maske_kostet_keine_nummer(self):
        Stammnummer.setzen("K", 700)
        self.client.get(reverse("stammdaten:kunde_neu"))
        self.client.get(reverse("stammdaten:kunde_neu"))
        self.assertContains(self.client.get(reverse("stammdaten:kunde_neu")), 'value="700"')

    def test_lieferant_anlegen_rueckt_vor(self):
        Stammnummer.setzen("LF", 300)
        daten = {"nummer": "300", "name1": "API", "land": "DE", "ap-TOTAL_FORMS": "0", "ap-INITIAL_FORMS": "0", "ap-MIN_NUM_FORMS": "0", "ap-MAX_NUM_FORMS": "1000",
                 "mindestbestellwert": "0", "versandkosten": "0", "lieferzeit_tage": "1"}
        r = self.client.post(reverse("stammdaten:lieferant_neu"), daten)
        self.assertEqual(r.status_code, 200, r.content[:600])
        self.assertTrue(Lieferant.objects.filter(nummer="300").exists())
        self.assertEqual(Stammnummer.vorschlag("LF"), "301")

    def test_shopkunde_nutzt_den_kundenzaehler(self):
        from apps.shop.services import kunde_finden_oder_anlegen
        Stammnummer.setzen("K", 40500)
        shop = mock.Mock(mandant=self.m, standard_zahlungsbedingung=None)
        kunde, neu = kunde_finden_oder_anlegen(shop, {"orderCustomer": {"email": "neu@example.test", "firstName": "Eva", "lastName": "Muster"}}, {}, {})
        self.assertTrue(neu)
        self.assertEqual(kunde.nummer, "40500")
        self.assertEqual(Stammnummer.vorschlag("K"), "40501")

    def test_migration_startet_beim_bisherigen_stand(self):
        modul = importlib.import_module("apps.stammdaten.migrations.0020_stammnummer")
        self.artikel("42")
        self.artikel("7", "L")
        self.artikel("EAN-1", "G")
        Artikel.objects.create(nummer="4003994155486123456789"[:20], typ="A", bezeichnung="lang", steuersatz=self.st)
        Stammnummer.objects.all().delete()
        modul.vorbelegen(django_apps, connection.schema_editor)
        stand = dict(Stammnummer.objects.values_list("art", "letzte_nummer"))
        self.assertEqual((stand["A"], stand["L"], stand["G"], stand["K"], stand["LF"]), (42, 7, 0, 1000, 0))
