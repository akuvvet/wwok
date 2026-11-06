"""Issue 12, Schritt 2: Plausibilität im Wareneingang, Import-Quarantäne und Prüfliste für Einzelpreise."""
from decimal import Decimal as D

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.kern.models import Mandant
from apps.lager.models import bestand
from apps.stammdaten import ekkalk
from apps.stammdaten.models import Artikel, ArtikelLieferant, ArtikelMandant, Lieferant, Preislistenartikel, Steuersatz

from . import preislisten, services
from .models import Preislistenimport, Preispruefung, Preislistenprofil

KOPF = "Best.-Nr.;Herst.-Nr.;EAN;Bezeichnung;Hersteller;EK;Bestand;Lieferzeit\n"


def liste(preise):
    """CSV-Liste: Artikel 0..n-1 mit den angegebenen EK (Text mit Komma)."""
    return (KOPF + "\n".join(f"B{i};H{i};;Artikel {i};Fabrikat;{p};5;1" for i, p in enumerate(preise))).encode()


def steuersatz():
    return Steuersatz.objects.first() or Steuersatz.objects.create(bezeichnung="19", prozent=D("19"))


class RechenregelTest(TestCase):
    def test_plausibilitaet_we(self):
        self.assertIsNone(ekkalk.plausibilitaet_we(D("10"), D("9.2857"), None))
        self.assertIsNone(ekkalk.plausibilitaet_we(D("13.9"), D("9.2857"), None))  # +49,7 %
        text = ekkalk.plausibilitaet_we(D("2"), D("9.2857"), D("8.50"))
        self.assertIn("Preis korrekt?", text)
        self.assertIn("Ø-EK 9.29 €", text)
        self.assertIn("Import 8.50 €", text)
        self.assertIsNone(ekkalk.plausibilitaet_we(D("2"), None, None))  # ohne Vergleichspreis keine Rückfrage

    def test_import_ist_auffaellig(self):
        zehn = [(D("10"), D("10"))] * 10
        drei_stark = [(D("10"), D("14.0001"))] * 3 + zehn[:7]  # 30 % der Artikel um > 30 %
        zwei_stark = [(D("10"), D("14.0001"))] * 2 + zehn[:8]  # genau 20 %: nicht mehr als 20
        self.assertTrue(ekkalk.import_ist_auffaellig(drei_stark))
        self.assertFalse(ekkalk.import_ist_auffaellig(zwei_stark))
        self.assertFalse(ekkalk.import_ist_auffaellig([(D("10"), D("20"))] * 4))  # zu wenig Vergleichsbasis
        self.assertFalse(ekkalk.import_ist_auffaellig([(D("10"), D("13"))] * 10))  # genau 30 %: nicht mehr als 30


class ImportSchutzTest(TestCase):
    def setUp(self):
        self.lf = Lieferant.objects.create(nummer="L1", name1="API GmbH", kurzname="API")
        st = steuersatz()
        for i in range(10):
            Artikel.objects.create(nummer=f"A{i}", bezeichnung=f"Artikel {i}", herstellernummer=f"H{i}", steuersatz=st)
        self.p = Preislistenprofil.objects.create(
            lieferant=self.lf, name="Liste", sp_bestellnummer="Best.-Nr.", sp_herstellernummer="Herst.-Nr.", sp_ean="EAN",
            sp_bezeichnung="Bezeichnung", sp_hersteller="Hersteller", sp_ek="EK", sp_bestand="Bestand", sp_lieferzeit="Lieferzeit")
        erst = preislisten.importieren(self.p, liste(["10,00"] * 10))
        self.assertEqual((erst.neu, erst.status), (10, "uebernommen"))

    def ek(self, nr):
        return Preislistenartikel.objects.get(lieferant=self.lf, bestellnummer=f"B{nr}").ek

    def test_quarantaene_haelt_gesamten_import_zurueck_und_freigabe_uebernimmt(self):
        prot = preislisten.importieren(self.p, liste(["14,00"] * 3 + ["10,00"] * 7))  # 30 % der Artikel um +40 %
        self.assertEqual(prot.status, "zur_pruefung")
        self.assertIn("Zur Prüfung", prot.meldung)
        self.assertEqual(self.ek(0), D("10.0000"))  # nichts übernommen
        self.assertEqual(ArtikelLieferant.objects.get(bestellnummer="B0").ek, D("10.0000"))
        self.assertIsNotNone(prot.datei)
        neu = preislisten.freigeben(Preislistenimport.objects.select_related("profil").get(pk=prot.pk))
        self.assertEqual(neu.geaendert, 3)
        self.assertEqual(self.ek(0), D("14.0000"))
        self.assertEqual(ArtikelLieferant.objects.get(bestellnummer="B0").ek, D("14.0000"))
        prot.refresh_from_db()
        self.assertEqual(prot.status, "uebernommen")
        self.assertIsNone(prot.datei)
        with self.assertRaises(ValueError):
            preislisten.freigeben(prot)  # nicht doppelt

    def test_quarantaene_verwerfen(self):
        prot = preislisten.importieren(self.p, liste(["14,00"] * 3 + ["10,00"] * 7))
        preislisten.verwerfen(prot)
        prot.refresh_from_db()
        self.assertEqual((prot.status, prot.datei), ("verworfen", None))
        self.assertEqual(self.ek(0), D("10.0000"))

    def test_einzelartikel_in_pruefliste_rest_wird_uebernommen(self):
        prot = preislisten.importieren(self.p, liste(["4,00", "10,50"] + ["10,00"] * 8))  # B0: -60 %, B1: +5 %
        self.assertEqual(prot.status, "uebernommen")
        self.assertEqual(prot.zur_pruefung, 1)
        self.assertIn("1 Preise zur Prüfung", prot.meldung)
        self.assertEqual(self.ek(0), D("10.0000"))  # alter Preis bleibt
        self.assertEqual(self.ek(1), D("10.5000"))
        pr = Preispruefung.objects.get()
        self.assertEqual((pr.bestellnummer, pr.ek_alt, pr.ek_neu, pr.status), ("B0", D("10.0000"), D("4.0000"), "offen"))
        self.assertEqual(round(pr.abweichung), 60)
        preislisten.preis_uebernehmen(pr)
        pr.refresh_from_db()
        self.assertEqual(pr.status, "uebernommen")
        self.assertEqual(self.ek(0), D("4.0000"))
        self.assertEqual(ArtikelLieferant.objects.get(bestellnummer="B0").ek, D("4.0000"))
        with self.assertRaises(ValueError):
            preislisten.preis_uebernehmen(pr)

    def test_einzelpreis_verwerfen_laesst_alten_preis(self):
        preislisten.importieren(self.p, liste(["30,00"] + ["10,00"] * 9))
        pr = Preispruefung.objects.get()
        preislisten.preis_verwerfen(pr)
        pr.refresh_from_db()
        self.assertEqual(pr.status, "verworfen")
        self.assertEqual(self.ek(0), D("10.0000"))

    def test_gleicher_import_ohne_aenderung_meldet_nichts(self):
        prot = preislisten.importieren(self.p, liste(["10,00"] * 10))
        self.assertEqual((prot.status, prot.zur_pruefung, Preispruefung.objects.count()), ("uebernommen", 0, 0))

    def test_pruefliste_seite_und_freigabe_per_klick(self):
        prot = preislisten.importieren(self.p, liste(["14,00"] * 3 + ["10,00"] * 7))
        user = get_user_model().objects.create_user("admin", password="x" * 14, is_staff=True, is_superuser=True)
        m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.client.force_login(user)
        s = self.client.session
        s["mandant_id"] = m.pk
        s.save()
        with override_settings(SECURE_SSL_REDIRECT=False):
            seite = self.client.get(reverse("einkauf:preispruefliste"))
            self.assertContains(seite, "Importe zur Prüfung (1)")
            self.client.post(reverse("einkauf:preispruefliste"), {"aktion": "import_freigeben", "id": prot.pk})
        self.assertEqual(self.ek(0), D("14.0000"))


@override_settings(SECURE_SSL_REDIRECT=False)
class WareneingangPlausibilitaetTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.lf = Lieferant.objects.create(nummer="L1", name1="API GmbH", kurzname="API")
        self.a = Artikel.objects.create(nummer="A1", bezeichnung="Maus", steuersatz=steuersatz())
        user = get_user_model().objects.create_user("t", password="x" * 14, is_staff=True)
        self.client.force_login(user)
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    def we(self, menge, preis):
        we = services.wareneingang_anlegen(self.m, self.lf)
        services.wareneingang_erfassen(we, self.a, D(menge), D(preis))
        return we

    def test_rueckfrage_bei_grosser_abweichung_vom_durchschnitts_ek(self):
        services.wareneingang_buchen(self.we("10", "10"))  # erster Wareneingang: kein Vergleichspreis
        we = self.we("10", "2")
        with self.assertRaises(services.PlausibilitaetFehler) as fehler:
            services.wareneingang_buchen(we)
        self.assertIn("Preis korrekt?", str(fehler.exception))
        self.assertIn("Ø-EK 10.00 €", str(fehler.exception))
        we.refresh_from_db()
        self.assertFalse(we.gebucht)
        self.assertEqual(bestand(self.m, self.a), D("10"))
        services.wareneingang_buchen(we, bestaetigt=True)
        self.assertEqual(bestand(self.m, self.a), D("20"))
        self.assertEqual(ArtikelMandant.objects.get(artikel=self.a, mandant=self.m).ek_avg, D("6.0000"))

    def test_kleine_abweichung_ohne_rueckfrage(self):
        services.wareneingang_buchen(self.we("10", "10"))
        services.wareneingang_buchen(self.we("10", "12"))
        self.assertEqual(bestand(self.m, self.a), D("20"))

    def test_ohne_bestand_zaehlt_der_alte_durchschnitt_nicht(self):
        services.wareneingang_buchen(self.we("10", "10"))
        from apps.lager.models import Bewegungsart
        from apps.lager.services import buche_manuell
        buche_manuell(self.m, self.a, Bewegungsart.EIGENVERBRAUCH, D("10"))
        self.assertEqual(bestand(self.m, self.a), D("0"))
        services.wareneingang_buchen(self.we("5", "30"))  # Bestand 0: Ø-EK wird nicht verglichen

    def test_maske_fragt_zurueck_und_bucht_erst_nach_bestaetigung(self):
        services.wareneingang_buchen(self.we("10", "10"))
        we = self.we("10", "2")
        url = reverse("einkauf:wareneingang_buchen", args=[we.pk])
        antwort = self.client.post(url, {"versandkosten": "0", "lieferschein": "LS-1"})
        self.assertEqual(antwort.status_code, 302)
        self.assertIn("rueckfrage=1", antwort["Location"])
        we.refresh_from_db()
        self.assertFalse(we.gebucht)
        self.assertEqual(we.lieferschein, "LS-1")  # Eingaben bleiben gespeichert
        seite = self.client.get(reverse("einkauf:wareneingang", args=[we.pk]) + "?rueckfrage=1")
        self.assertContains(seite, "Angaben geprüft, trotzdem buchen")
        self.client.post(url, {"versandkosten": "0", "preis_bestaetigt": "on"})
        we.refresh_from_db()
        self.assertTrue(we.gebucht)
