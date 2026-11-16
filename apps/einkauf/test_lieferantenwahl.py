"""Lieferantenwahl mit Preisvergleich bei „In Lieferantenbestellung kopieren“ (Beleg mit mehreren Positionen)."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.kern.models import Mandant
from apps.stammdaten.models import Artikel, ArtikelLieferant, Kunde, Lieferant, Preislistenartikel, Steuersatz
from apps.verkauf.models import Beleg, Belegart, Position

from . import services
from .models import Bestellung

D = Decimal


@override_settings(SECURE_SSL_REDIRECT=False)
class LieferantenwahlTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.api = Lieferant.objects.create(nummer="L1", name1="API GmbH", kurzname="API")
        self.kos = Lieferant.objects.create(nummer="L2", name1="Kosatec GmbH", kurzname="KOSATEC")
        st = Steuersatz.objects.create(bezeichnung="19", prozent=D("19"))
        self.maus = Artikel.objects.create(nummer="M1", bezeichnung="Maus", steuersatz=st, vk1=D("15"))
        self.tast = Artikel.objects.create(nummer="T1", bezeichnung="Tastatur", steuersatz=st, vk1=D("25"))
        # Maus: API 5,00 / Kosatec 4,00 – Tastatur: API 10,00 / Kosatec 12,00
        for artikel, lf, ek in ((self.maus, self.api, "5"), (self.maus, self.kos, "4"), (self.tast, self.api, "10"), (self.tast, self.kos, "12")):
            ArtikelLieferant.objects.create(artikel=artikel, lieferant=lf, bestellnummer=f"{lf.kurz}-{artikel.nummer}", ek=D(ek), bestand=10)
        self.kunde = Kunde.objects.create(nummer="1", name1="K")
        self.ab = Beleg.objects.create(mandant=self.m, art=Belegart.AUFTRAG, kunde=self.kunde)
        self.p_maus = self.pos(self.maus, "10")
        self.p_tast = self.pos(self.tast, "2")
        self.user = get_user_model().objects.create_user("t", password="x" * 14)
        self.url = reverse("einkauf:aus_beleg", args=[self.ab.pk])

    def pos(self, artikel, menge, beleg=None):
        return Position.objects.create(beleg=beleg or self.ab, artikel=artikel, bezeichnung=artikel.bezeichnung, menge=D(menge), einzelpreis=D("30"))

    def login(self):
        self.client.force_login(self.user)
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    def spalte(self, vergleich, lieferant):
        return next(s for s in vergleich["spalten"] if s["lieferant"] == lieferant)

    # ---- Vergleich --------------------------------------------------------------------------------

    def test_vergleich_rechnet_je_lieferant_und_guenstigste_aufteilung(self):
        v = services.lieferantenvergleich(self.ab)
        self.assertTrue(v["wahl_noetig"])
        self.assertEqual(v["guenstigste"], D("60.00"))  # 10 × 4 (Kosatec) + 2 × 10 (API)
        self.assertEqual(v["aufteilung"], [("API", 1), ("KOSATEC", 1)])
        api, kos = self.spalte(v, self.api), self.spalte(v, self.kos)
        self.assertEqual((api["gesamt"], api["mehr"], api["fehlt"]), (D("70.00"), D("10.00"), 0))
        self.assertEqual((kos["gesamt"], kos["mehr"], kos["fehlt"]), (D("64.00"), D("4.00"), 0))
        self.assertEqual([s["lieferant"] for s in v["spalten"]], [self.kos, self.api])  # günstigster Komplettlieferant zuerst
        self.assertEqual([z["bester"]["lieferant"] for z in v["zeilen"]], [self.kos, self.api])

    def test_lieferant_ohne_einen_artikel_rest_vom_guenstigsten(self):
        ArtikelLieferant.objects.filter(artikel=self.tast, lieferant=self.kos).delete()
        v = services.lieferantenvergleich(self.ab)
        kos = self.spalte(v, self.kos)
        self.assertEqual((kos["anzahl"], kos["fehlt"], kos["summe"], kos["gesamt"]), (1, 1, D("40.00"), D("60.00")))
        self.assertEqual(v["spalten"][0]["lieferant"], self.api)  # wer alles führt, steht vorn
        self.assertIsNone(v["zeilen"][1]["zellen"][1])

    def test_bestand_null_wird_nicht_vorgeschlagen_und_knapp_markiert(self):
        ArtikelLieferant.objects.filter(artikel=self.maus, lieferant=self.kos).update(bestand=0)
        ArtikelLieferant.objects.filter(artikel=self.tast, lieferant=self.api).update(bestand=1)
        v = services.lieferantenvergleich(self.ab)
        self.assertEqual(v["zeilen"][0]["bester"]["lieferant"], self.api)  # Kosatec billiger, aber nicht auf Lager
        self.assertTrue(v["zeilen"][1]["angebote"][self.api.pk]["knapp"])  # 1 auf Lager, 2 gebraucht
        self.assertTrue(self.spalte(v, self.api)["knapp"])

    def test_gesperrter_lieferant_fehlt(self):
        Lieferant.objects.filter(pk=self.kos.pk).update(gesperrt=True)
        v = services.lieferantenvergleich(self.ab)
        self.assertEqual([s["lieferant"] for s in v["spalten"]], [self.api])
        self.assertFalse(v["wahl_noetig"])

    def test_keine_wahl_bei_einer_position_oder_nur_einem_lieferanten(self):
        self.p_tast.delete()
        self.assertFalse(services.lieferantenvergleich(self.ab)["wahl_noetig"])  # nur eine Position
        self.p_tast = self.pos(self.tast, "2")
        ArtikelLieferant.objects.filter(lieferant=self.kos).delete()
        self.assertFalse(services.lieferantenvergleich(self.ab)["wahl_noetig"])  # nur ein Lieferant

    def test_preislistenartikel_gleiche_herstellernummer_bei_anderem_lieferanten(self):
        an = Beleg.objects.create(mandant=self.m, art=Belegart.ANGEBOT, kunde=self.kunde)
        e_api = Preislistenartikel.objects.create(lieferant=self.api, bestellnummer="500", herstellernummer="PSAZ-650",
                                                  bezeichnung="Netzteil", ek=D("40.0000"), bestand=5)
        e_kos = Preislistenartikel.objects.create(lieferant=self.kos, bestellnummer="K-9", herstellernummer="psaz-650",
                                                  bezeichnung="Netzteil 650W", ek=D("38.0000"), bestand=5)
        p = Position.objects.create(beleg=an, preisliste_artikel=e_api, typ="P", bezeichnung="Netzteil", menge=D("3"), einzelpreis=D("60"))
        self.pos(self.maus, "1", beleg=an)
        v = services.lieferantenvergleich(an)
        self.assertEqual(v["zeilen"][0]["bester"]["lieferant"], self.kos)
        self.assertEqual(v["zeilen"][0]["angebote"][self.kos.pk]["summe"], D("114.00"))
        # ohne Wahl bleibt es beim Lieferanten der Angebotszeile
        self.assertEqual({b.lieferant for b in services.bestellungen_aus_beleg(an)}, {self.api, self.kos})
        Bestellung.objects.all().delete()
        b = services.bestellungen_aus_beleg(an, wahl={p.pk: self.kos.pk})
        self.assertEqual(len(b), 1)
        bp = b[0].positionen.get(preisliste_artikel=e_kos)
        self.assertEqual((bp.menge, bp.ek, bp.bestellnummer, bp.artikel), (D("3"), D("38.00"), "K-9", None))
        services.bestellen(b[0])
        self.assertEqual(services.bestellt_je_position(an)[p.pk], D("3"))  # zählt für die Position, trotz anderer Preislistenzeile

    def p_angebot(self, **kos):
        an = Beleg.objects.create(mandant=self.m, art=Belegart.ANGEBOT, kunde=self.kunde)
        e_api = Preislistenartikel.objects.create(lieferant=self.api, bestellnummer="500", herstellernummer="PSAZ-650",
                                                  bezeichnung="Netzteil", hersteller="AZZA", ek=D("40.0000"), bestand=5)
        e_kos = Preislistenartikel.objects.create(lieferant=self.kos, bestellnummer="K-9", herstellernummer="psaz-650",
                                                  bezeichnung="Netzteil 650W", ek=D("38.0000"), bestand=5, **kos)
        p = Position.objects.create(beleg=an, preisliste_artikel=e_api, typ="P", bezeichnung="Netzteil", menge=D("3"), einzelpreis=D("60"))
        return an, e_api, e_kos, p

    def test_p_wahl_zaehlt_als_bestellt_wenn_eine_zeile_schon_eigener_artikel_ist(self):
        eigen = Artikel.objects.create(nummer="N1", bezeichnung="Netzteil", steuersatz=self.maus.steuersatz)
        an, e_api, e_kos, p = self.p_angebot(artikel=eigen)  # gewählte Zeile ist verknüpft, die der Position nicht
        services.bestellen(services.bestellungen_aus_beleg(an, wahl={p.pk: self.kos.pk})[0])
        self.assertEqual(services.bestellt_je_position(an)[p.pk], D("3"))
        Bestellung.objects.all().delete()
        e_kos.artikel = None
        e_kos.save()
        e_api.artikel = eigen  # umgekehrt: die Zeile der Position ist verknüpft, die gewählte nicht
        e_api.save()
        p.refresh_from_db()
        services.bestellen(services.bestellungen_aus_beleg(an, wahl={p.pk: self.kos.pk})[0])
        self.assertEqual(services.bestellt_je_position(an)[p.pk], D("3"))

    def test_p_gesperrter_eigener_lieferant_und_anderer_hersteller_fehlen(self):
        an, e_api, e_kos, p = self.p_angebot(hersteller="Anderer")
        self.assertEqual(list(services.angebote_je_lieferant(p, p.menge)), [self.api.pk])  # gleiche Nummer, anderer Hersteller
        e_kos.hersteller = "azza"
        e_kos.save()
        Lieferant.objects.filter(pk=self.api.pk).update(gesperrt=True)
        p.refresh_from_db()
        self.assertEqual(list(services.angebote_je_lieferant(p, p.menge)), [self.kos.pk])

    # ---- Anlegen mit Wahl -------------------------------------------------------------------------

    def test_alles_bei_einem_lieferanten(self):
        liste = services.bestellungen_aus_beleg(self.ab, benutzer=self.user, wahl={self.p_maus.pk: self.api.pk, self.p_tast.pk: self.api.pk})
        self.assertEqual([b.lieferant for b in liste], [self.api])
        self.assertEqual(sorted((p.artikel.nummer, p.menge, p.ek, p.bestellnummer) for p in liste[0].positionen.all()),
                         [("M1", D("10"), D("5.00"), "API-M1"), ("T1", D("2"), D("10.00"), "API-T1")])
        self.assertEqual(liste[0].summe_netto, D("70.00"))

    def test_gemischte_wahl_und_ohne_wahl_wie_bisher(self):
        liste = services.bestellungen_aus_beleg(self.ab, wahl={self.p_maus.pk: self.api.pk, self.p_tast.pk: self.kos.pk})
        self.assertEqual({(b.lieferant, b.positionen.get().artikel) for b in liste}, {(self.api, self.maus), (self.kos, self.tast)})
        Bestellung.objects.all().delete()
        liste = services.bestellungen_aus_beleg(self.ab)  # ohne Wahl: je Position der günstigste
        self.assertEqual({(b.lieferant, b.positionen.get().artikel) for b in liste}, {(self.kos, self.maus), (self.api, self.tast)})

    def test_wahl_eines_lieferanten_der_den_artikel_nicht_fuehrt(self):
        fremd = Lieferant.objects.create(nummer="L3", name1="Fremd")
        with self.assertRaisesMessage(ValueError, "neu wählen"):
            services.bestellungen_aus_beleg(self.ab, wahl={self.p_maus.pk: fremd.pk})
        self.assertEqual(Bestellung.objects.count(), 0)

    # ---- Oberfläche -------------------------------------------------------------------------------

    def test_seite_fragt_und_legt_nach_wahl_an(self):
        self.login()
        r = self.client.post(self.url)
        self.assertContains(r, "Bei welchem Lieferanten bestellen?")
        self.assertContains(r, "Alles bei KOSATEC")
        self.assertContains(r, "64,00")
        self.assertContains(r, "+10,00")
        self.assertContains(r, f'name="lf_{self.p_maus.pk}" value="{self.kos.pk}" data-summe="40.00" checked')
        self.assertEqual(Bestellung.objects.count(), 0)  # nur gefragt
        r = self.client.post(self.url, {"wahl": "1", f"lf_{self.p_maus.pk}": self.kos.pk, f"lf_{self.p_tast.pk}": self.kos.pk})
        b = Bestellung.objects.get()
        self.assertRedirects(r, reverse("einkauf:bestellung", args=[b.pk]), fetch_redirect_response=False)
        self.assertEqual((b.lieferant, b.summe_netto, b.aus_beleg, b.angelegt_von), (self.kos, D("64.00"), self.ab, self.user))

    def test_seite_nach_rueckfrage_nur_fehlende_mengen(self):
        self.login()
        wahl = {"wahl": "1", f"lf_{self.p_maus.pk}": self.kos.pk, f"lf_{self.p_tast.pk}": self.api.pk}
        self.client.post(self.url, wahl)
        for b in Bestellung.objects.all():
            services.bestellen(b)
        self.p_maus.menge = D("14")
        self.p_maus.save()
        self.pos(self.tast, "1")
        self.assertContains(self.client.post(self.url), "schon beim Lieferanten bestellt")
        r = self.client.post(self.url, {"modus": "fehlend"})
        self.assertContains(r, "Bei welchem Lieferanten bestellen?")
        self.assertContains(r, 'name="modus" value="fehlend"')
        self.assertContains(r, "16,00")  # 4 Mäuse × 4,00 bei Kosatec
        self.assertEqual(self.spalte(services.lieferantenvergleich(self.ab, modus="fehlend"), self.kos)["gesamt"], D("28.00"))
        r = self.client.post(self.url, {"modus": "komplett", "grund": ""}, follow=True)
        self.assertContains(r, "Grund")  # komplett ohne Grund: Meldung statt Lieferantenwahl
        self.assertEqual(Bestellung.objects.count(), 2)

    def test_anderer_mandant_sieht_den_beleg_nicht(self):
        m2 = Mandant.objects.create(kurzname="ZONE", firmenname="ZONE")
        self.client.force_login(self.user)
        s = self.client.session
        s["mandant_id"] = m2.pk
        s.save()
        self.assertEqual(self.client.post(self.url).status_code, 404)
