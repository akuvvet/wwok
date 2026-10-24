import io
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from pypdf import PdfReader

from apps.einkauf import services as einkauf
from apps.kern.models import Mandant
from apps.lager.models import BestandReichtNicht, Bewegungsart, bestand
from apps.lager.services import buche_manuell
from apps.stammdaten.models import (Artikel, Kategorie, Kunde, Lieferant, Preislistenartikel, PreisVorgabe, Steuersatz)
from apps.werkstatt.models import Reparatur

from .models import Beleg, Belegart, Position
from .pdf import beleg_pdf


@override_settings(SECURE_SSL_REDIRECT=False)
class UmbauTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.api = Lieferant.objects.create(nummer="70272", name1="API GmbH", kurzname="API")
        self.kosatec = Lieferant.objects.create(nummer="70300", name1="Kosatec Computer GmbH")
        self.kunde = Kunde.objects.create(nummer="1", name1="Kunde GmbH")
        self.st = Steuersatz.objects.get(nummer=1)
        self.netzteil = Preislistenartikel.objects.create(
            lieferant=self.api, bestellnummer="500", herstellernummer="PSAZ-650", bezeichnung="Netzteil 650W AZZA PSAZ 12cm",
            hersteller="Azza", ek=Decimal("40.0000"), bestand=12)
        self.gehaeuse = Preislistenartikel.objects.create(
            lieferant=self.kosatec, bestellnummer="K-7", herstellernummer="CS-1", bezeichnung="Gehäuse AZZA Apollo ohne Netzteil",
            hersteller="Azza", ek=Decimal("55.0000"), bestand=3)
        self.maus = Artikel.objects.create(nummer="M1", bezeichnung="Maus", steuersatz=self.st, vk1=Decimal("20"))
        self.client.force_login(get_user_model().objects.create_user("t", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    def beleg(self, art=Belegart.ANGEBOT):
        return Beleg.objects.create(mandant=self.m, art=art, kunde=self.kunde)

    # ---- Preislistenartikel im Verkauf ---------------------------------------------------------------
    def test_suche_zeigt_preislistenartikel_mit_lieferant_und_mehrwortsuche(self):
        PreisVorgabe.objects.create(aufschlag_vk1=Decimal("25"))
        r = self.client.get(reverse("verkauf:api_artikel"), {"q": "netz azza", "kunde": self.kunde.pk}).json()["treffer"]
        self.assertEqual({t["nummer"] for t in r}, {"PSAZ-650", "CS-1"})  # beide enthalten „netz“ und „azza“
        t = next(t for t in r if t["nummer"] == "PSAZ-650")
        self.assertEqual((t["typ"], t["lieferant"], t["preis"], t["pl"]), ("P", "API", "50.00", self.netzteil.pk))  # 40 € + 25 %
        verknuepft = Artikel.objects.create(nummer="PSAZ-650", bezeichnung="Netzteil 650W", herstellernummer="PSAZ-650", steuersatz=self.st)
        Preislistenartikel.objects.filter(pk=self.netzteil.pk).update(artikel=verknuepft)
        nummern = [t["nummer"] for t in self.client.get(reverse("verkauf:api_artikel"), {"q": "psaz netz"}).json()["treffer"]]
        self.assertEqual(nummern, ["PSAZ-650"])  # nur noch der eigene Artikel, nicht doppelt

    def test_position_aus_preisliste_ohne_artikelanlage(self):
        b = self.beleg()
        url = reverse("verkauf:position_neu", args=[b.pk])
        self.client.post(url, {"pl": self.netzteil.pk, "artikel": "PSAZ-650", "menge": "2"})
        self.assertEqual(b.positionen.count(), 0)  # ohne Aufschlag kein Preisvorschlag → Fehlermeldung statt falschem Preis
        PreisVorgabe.objects.create(aufschlag_vk1=Decimal("25"))
        self.client.post(url, {"pl": self.netzteil.pk, "artikel": "PSAZ-650", "menge": "2"})
        p = b.positionen.get()
        self.assertEqual((p.typ, p.artikel, p.preisliste_artikel, p.einzelpreis, p.artikelnummer), ("P", None, self.netzteil, Decimal("50.00"), "PSAZ-650"))
        b.refresh_from_db()
        self.assertEqual(b.summe_netto, Decimal("100.00"))
        self.assertEqual(Artikel.objects.filter(nummer="PSAZ-650").count(), 0)  # kein eigener Artikel entstanden
        seite = self.client.get(reverse("verkauf:beleg", args=[b.pk]))
        self.assertContains(seite, "PSAZ-650")
        self.assertContains(seite, ">API<")  # Lieferant als Kurzname neben dem Artikel
        text = " ".join(PdfReader(io.BytesIO(beleg_pdf(b))).pages[0].extract_text().split())
        self.assertIn("PSAZ-650", text)
        self.assertNotIn("API", text)  # beim Kunden erscheint der Lieferant nicht
        auftrag = b.folgebeleg_erzeugen()
        self.assertEqual(auftrag.positionen.get().preisliste_artikel, self.netzteil)
        ls = auftrag.folgebeleg_erzeugen()
        ls.abschliessen()  # Preislistenartikel haben keinen Lagerbestand und blockieren den Lieferschein nicht
        self.assertTrue(ls.abgeschlossen)

    def test_preis_von_hand_ueberschreibt_vorschlag(self):
        b = self.beleg()
        self.client.post(reverse("verkauf:position_neu", args=[b.pk]), {"pl": self.gehaeuse.pk, "menge": "1", "preis": "99,90"})
        self.assertEqual(b.positionen.get().einzelpreis, Decimal("99.90"))

    # ---- Lieferantenbestellung -------------------------------------------------------------------------
    def test_bestellung_suche_und_preislistenposition_wird_beim_wareneingang_zum_artikel(self):
        b = einkauf.bestellung_anlegen(self.m, self.api)
        treffer = self.client.get(reverse("einkauf:bestellartikel_suche", args=[b.pk]), {"q": "azza netz"}).json()["treffer"]
        self.assertEqual([t["nummer"] for t in treffer], ["PSAZ-650"])  # Gehäuse gehört zu Kosatec, nicht zu dieser Bestellung
        self.assertEqual((treffer[0]["ek"], treffer[0]["lieferant"]), ("40.0000", "API"))
        eigene = self.client.get(reverse("einkauf:bestellartikel_suche", args=[b.pk]), {"q": "maus"}).json()["treffer"]
        self.assertEqual([t["nummer"] for t in eigene], ["M1"])
        self.client.post(reverse("einkauf:bestellung_position", args=[b.pk]), {"pl": self.netzteil.pk, "menge": "4"})
        pos = b.positionen.get()
        self.assertEqual((pos.artikel, pos.preisliste_artikel, pos.ek, pos.bestellnummer, pos.nummer), (None, self.netzteil, Decimal("40.0000"), "500", "PSAZ-650"))
        self.client.post(reverse("einkauf:bestellung_bestellen", args=[b.pk]))
        we = einkauf.wareneingang_anlegen(self.m, self.api, b)
        einkauf.wareneingang_aus_bestellung(we)  # hier entsteht der eigene Artikel
        a = Artikel.objects.get(nummer="PSAZ-650")
        self.assertEqual((a.typ, a.hersteller.name, a.herstellernummer), ("A", "Azza", "PSAZ-650"))
        einkauf.wareneingang_buchen(we)
        self.assertEqual(bestand(self.m, a), Decimal("4"))
        self.netzteil.refresh_from_db()
        self.assertEqual(self.netzteil.artikel, a)
        pos.refresh_from_db()
        self.assertEqual((pos.artikel, pos.geliefert), (a, Decimal("4")))
        self.assertContains(self.client.get(reverse("einkauf:bestellung", args=[b.pk])), "PSAZ-650")

    def test_scan_im_wareneingang_findet_preislistenartikel(self):
        we = einkauf.wareneingang_anlegen(self.m, self.api)
        a = einkauf.artikel_per_code("PSAZ-650", self.api)
        self.assertEqual((a.nummer, a.typ), ("PSAZ-650", "A"))
        self.assertIsNone(einkauf.artikel_per_code("CS-1", self.api))  # gehört zu einem anderen Lieferanten

    def test_bestellung_falscher_lieferant_wird_abgelehnt(self):
        b = einkauf.bestellung_anlegen(self.m, self.api)
        with self.assertRaises(ValueError):
            einkauf.bestellposition(b, None, Decimal("1"), preisliste_artikel=self.gehaeuse)

    # ---- Lagerbestand nie unter 0 ------------------------------------------------------------------------
    def test_lagerbestand_nie_unter_null(self):
        buche_manuell(self.m, self.maus, Bewegungsart.WARENEINGANG, Decimal("3"))
        with self.assertRaises(BestandReichtNicht):
            buche_manuell(self.m, self.maus, Bewegungsart.EIGENVERBRAUCH, Decimal("4"))
        with self.assertRaises(ValueError):  # BestandReichtNicht ist ein ValueError – die Masken zeigen die Meldung
            buche_manuell(self.m, self.maus, Bewegungsart.KORREKTUR, Decimal("-5"))
        self.assertEqual(bestand(self.m, self.maus), Decimal("3"))
        buche_manuell(self.m, self.maus, Bewegungsart.EIGENVERBRAUCH, Decimal("3"))  # genau auf 0 ist erlaubt
        self.assertEqual(bestand(self.m, self.maus), Decimal("0"))

    def test_rechnung_nur_mit_bestand(self):
        re = self.beleg(Belegart.RECHNUNG)
        Position.objects.create(beleg=re, artikel=self.maus, bezeichnung="Maus", menge=Decimal("5"), einzelpreis=Decimal("20"))
        buche_manuell(self.m, self.maus, Bewegungsart.WARENEINGANG, Decimal("2"))
        with self.assertRaisesMessage(BestandReichtNicht, "benötigt 5"):
            re.abschliessen()
        re.refresh_from_db()
        self.assertEqual((re.abgeschlossen, re.nummer, bestand(self.m, self.maus)), (False, None, Decimal("2")))  # nichts halb gebucht
        antwort = self.client.post(reverse("verkauf:beleg_abschliessen", args=[re.pk]), follow=True)
        self.assertContains(antwort, "Lagerbestand reicht nicht")  # Meldung in der Maske statt Fehlerseite
        buche_manuell(self.m, self.maus, Bewegungsart.WARENEINGANG, Decimal("3"))
        re.abschliessen()
        self.assertEqual((re.abgeschlossen, bestand(self.m, self.maus)), (True, Decimal("0")))

    def test_lieferschein_prueft_bestand_rechnung_danach_nicht_erneut(self):
        buche_manuell(self.m, self.maus, Bewegungsart.WARENEINGANG, Decimal("2"))
        ls = self.beleg(Belegart.LIEFERSCHEIN)
        Position.objects.create(beleg=ls, artikel=self.maus, bezeichnung="Maus", menge=Decimal("2"), einzelpreis=Decimal("20"))
        ls.abschliessen()
        re = ls.folgebeleg_erzeugen()
        re.abschliessen()  # Bestand ist 0, aber der Lieferschein hat bereits abgebucht
        self.assertTrue(re.abgeschlossen)

    # ---- Sammelrechnung nur bei mehreren offenen Lieferscheinen ----------------------------------------------
    def test_sammelrechnung_auswahl_nur_bei_mehreren_lieferscheinen(self):
        buche_manuell(self.m, self.maus, Bewegungsart.WARENEINGANG, Decimal("10"))

        def lieferschein():
            ls = self.beleg(Belegart.LIEFERSCHEIN)
            Position.objects.create(beleg=ls, artikel=self.maus, bezeichnung="Maus", menge=Decimal("1"), einzelpreis=Decimal("20"))
            ls.abschliessen()
            return ls
        url = reverse("verkauf:kunde", args=[self.kunde.pk])
        lieferschein()
        seite = self.client.get(url)
        self.assertNotContains(seite, 'name="auswahl"')
        self.assertNotContains(seite, "in Rechnung umwandeln")
        lieferschein()
        seite = self.client.get(url)
        self.assertContains(seite, 'name="auswahl"')
        self.assertContains(seite, "in Rechnung umwandeln")

    # ---- Reklamation ------------------------------------------------------------------------------------
    def test_reklamation_legt_werkstattauftrag_mit_garantie_an(self):
        buche_manuell(self.m, self.maus, Bewegungsart.WARENEINGANG, Decimal("1"))
        re = Beleg.objects.create(mandant=self.m, art=Belegart.RECHNUNG, kunde=self.kunde, belegdatum=date.today() - timedelta(days=100))
        Position.objects.create(beleg=re, artikel=self.maus, bezeichnung="Maus", einzelpreis=Decimal("20"))
        re.abschliessen()
        seite = self.client.get(reverse("werkstatt:reklamation", args=[re.pk]))
        self.assertContains(seite, "Maus")
        self.assertTrue(seite.context["zeilen"][0]["in_garantie"])  # 24 Monate Standard
        pos = re.positionen.get()
        r = self.client.post(reverse("werkstatt:reklamation", args=[re.pk]), {
            "position": pos.pk, "fehler": "Taste klemmt", "seriennummer": "SN-1", "garantie": "on"})
        rep = Reparatur.objects.get()
        self.assertRedirects(r, reverse("werkstatt:detail", args=[rep.pk]))
        self.assertEqual((rep.kunde, rep.artikel, rep.garantie, rep.seriennummer, rep.fehler), (self.kunde, self.maus, True, "SN-1", "Taste klemmt"))
        self.assertIn("Reklamation zu", rep.interne_notiz)
        leer = self.client.post(reverse("werkstatt:reklamation", args=[re.pk]), {"position": pos.pk, "fehler": ""})
        self.assertContains(leer, "beschreiben")
        self.assertEqual(Reparatur.objects.count(), 1)


@override_settings(SECURE_SSL_REDIRECT=False)
class EsdSoftwareTest(TestCase):
    def test_keys_nur_fuer_software_artikel(self):
        from apps.esd.models import ESDKey  # noqa: F401
        m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        st = Steuersatz.objects.get(nummer=1)
        software = Kategorie.objects.create(name="Software", nummer="108")
        office = Kategorie.objects.create(name="Officesoftware", nummer="108107", eltern=software)
        hardware = Kategorie.objects.create(name="Hardware", nummer="1")
        win = Artikel.objects.create(nummer="WIN11", bezeichnung="Windows 11 Pro", steuersatz=st, kategorie=office)
        maus = Artikel.objects.create(nummer="M1", bezeichnung="Windows Maus", steuersatz=st, kategorie=hardware)
        self.client.force_login(get_user_model().objects.create_user("t", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = m.pk
        s.save()
        seite = self.client.get(reverse("esd:uebersicht"), {"q": "windows"})
        self.assertEqual([a.nummer for a in seite.context["kandidaten"]], ["WIN11"])  # Maus (Hardware) wird nicht angeboten
        self.client.post(reverse("esd:aktivieren", args=[maus.pk]))
        maus.refresh_from_db()
        self.assertFalse(maus.esd_key)  # auch per Direktaufruf nicht möglich
        r = self.client.post(reverse("esd:aktivieren", args=[win.pk]))
        win.refresh_from_db()
        self.assertTrue(win.esd_key)
        self.assertRedirects(r, reverse("esd:artikel", args=[win.pk]))
