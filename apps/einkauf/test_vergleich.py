"""Lieferantenvergleich: Gesamtkosten mit Versand, Mindestbestellwert, Bestand; Entscheidung und Verschieben von Hand."""
from decimal import Decimal as D

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.kern.models import Mandant
from apps.stammdaten.models import Artikel, ArtikelLieferant, Lieferant, Preislistenartikel, Steuersatz

from . import services, vergleich
from .models import BestellStatus, Bestellung


@override_settings(SECURE_SSL_REDIRECT=False)
class VergleichTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.api = Lieferant.objects.create(nummer="L1", name1="API GmbH", kurzname="API", versandkosten=D("10"), versandkostenfrei_ab=D("500"))
        self.kosa = Lieferant.objects.create(nummer="L2", name1="Kosatec", kurzname="Kosatec", versandkosten=D("0"), mindestbestellwert=D("100"))
        self.ingram = Lieferant.objects.create(nummer="L3", name1="Ingram", kurzname="Ingram")
        st = Steuersatz.objects.create(bezeichnung="19", prozent=D("19"))
        self.a = Artikel.objects.create(nummer="A1", bezeichnung="Maus", steuersatz=st, ean="4000000000001", herstellernummer="M-1")
        for l, ek, bestand, nr in ((self.api, "20", 50, "API-1"), (self.kosa, "25", 50, "KOS-1")):
            ArtikelLieferant.objects.create(artikel=self.a, lieferant=l, bestellnummer=nr, ek=D(ek), bestand=bestand)
        self.user = get_user_model().objects.create_user("t", password="x" * 14)
        self.client.force_login(self.user)
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()
        self.url = reverse("einkauf:preisvergleich")

    def namen(self, zeilen):
        return [z["lieferant"].kurz for z in zeilen]

    def test_versand_macht_den_billigeren_ek_teurer(self):
        zeilen = vergleich.vergleich(self.m, D("6"), self.a)  # API 120 + 10 = 130, Kosatec 150 + 0
        self.assertEqual(self.namen(zeilen), ["API", "Kosatec"])
        self.assertEqual((zeilen[0]["ware"], zeilen[0]["versand"], zeilen[0]["gesamt"]), (D("120.00"), D("10"), D("130.00")))
        self.a.lieferanten.filter(lieferant=self.api).update(ek=D("24"))  # API 144 + 10 = 154, Kosatec 150
        self.assertEqual(self.namen(vergleich.vergleich(self.m, D("6"), self.a)), ["Kosatec", "API"])

    def test_versandkostenfrei_ab_grenze(self):
        zeile = vergleich.vergleich(self.m, D("25"), self.a)[0]  # 500 € erreicht
        self.assertEqual((zeile["lieferant"], zeile["versand"]), (self.api, D("0")))

    def test_mindestbestellwert_und_bestand_ranken_nach_hinten(self):
        self.a.lieferanten.filter(lieferant=self.kosa).update(ek=D("10"))
        zeilen = vergleich.vergleich(self.m, D("2"), self.a)  # Kosatec billiger, aber 20 € < 100 € Mindestwert
        self.assertEqual(self.namen(zeilen), ["API", "Kosatec"])
        self.assertIn("noch 80,00 €", zeilen[1]["hinweise"][0])
        self.a.lieferanten.filter(lieferant=self.api).update(bestand=1)
        zeilen = vergleich.vergleich(self.m, D("2"), self.a)
        self.assertFalse(next(z for z in zeilen if z["lieferant"] == self.api)["lieferbar"])

    def test_offener_entwurf_zaehlt_fuer_mindestwert_und_versand(self):
        self.a.lieferanten.filter(lieferant=self.kosa).update(ek=D("10"))
        b = services.bestellung_anlegen(self.m, self.kosa)
        services.bestellposition(b, self.a, D("10"))  # Entwurf 100 €
        zeile = next(z for z in vergleich.vergleich(self.m, D("1"), self.a) if z["lieferant"] == self.kosa)
        self.assertEqual(zeile["fehlt"], D("0"))
        api = services.bestellung_anlegen(self.m, self.api)
        services.bestellposition(api, self.a, D("24"))  # Entwurf 480 € zahlt 10 € Versand; +1 Stück erreicht die 500 €
        zeile = next(z for z in vergleich.vergleich(self.m, D("1"), self.a) if z["lieferant"] == self.api)
        self.assertEqual((zeile["versand"], zeile["gesamt"]), (D("-10"), D("10.00")))

    def test_preislisten_ueber_ean_und_herstellernummer_und_kosten_unbekannt(self):
        Preislistenartikel.objects.create(lieferant=self.ingram, bestellnummer="ING-9", bezeichnung="Mouse", ean="4000000000001", ek=D("15"))
        zeile = next(z for z in vergleich.vergleich(self.m, D("1"), self.a) if z["lieferant"] == self.ingram)
        self.assertTrue(zeile["versand_offen"])
        self.assertIn("Versand nicht hinterlegt", zeile["hinweise"])
        self.ingram.gesperrt = True
        self.ingram.save()
        self.assertNotIn("Ingram", self.namen(vergleich.vergleich(self.m, D("1"), self.a)))

    def test_seite_und_bestellen_legt_in_entwurf(self):
        r = self.client.get(self.url, {"artikel": "A1", "menge": "6"})
        self.assertContains(r, "günstigste Wahl")
        self.assertContains(self.client.get(self.url, {"q": "maus"}), "?artikel=A1")
        self.assertNotContains(self.client.get(self.url, {"artikel": "A1", "teil": "1"}), "<html")
        r = self.client.post(reverse("einkauf:preisvergleich_bestellen"), {"artikel": "A1", "menge": "6", "lieferant": self.kosa.pk}, follow=True)
        self.assertContains(r, "Aufgenommen in Bestellung")
        b = Bestellung.objects.get(lieferant=self.kosa)
        p = b.positionen.get()
        self.assertEqual((b.status, p.ek, p.menge, p.bestellnummer), (BestellStatus.ENTWURF, D("25.0000"), D("6"), "KOS-1"))
        self.client.post(reverse("einkauf:preisvergleich_bestellen"), {"artikel": "A1", "menge": "1", "lieferant": self.kosa.pk})
        self.assertEqual(Bestellung.objects.filter(lieferant=self.kosa).count(), 1)  # derselbe Entwurf
        self.assertEqual(b.positionen.get().menge, D("7"))

    def test_preislistenartikel_vom_anderen_lieferanten_bestellen(self):
        e1 = Preislistenartikel.objects.create(lieferant=self.api, bestellnummer="X1", herstellernummer="Z-1", bezeichnung="Kabel", ek=D("5"))
        Preislistenartikel.objects.create(lieferant=self.ingram, bestellnummer="X2", herstellernummer="z-1", bezeichnung="Kabel", ek=D("4"))
        self.client.post(reverse("einkauf:preisvergleich_bestellen"), {"pl": e1.pk, "menge": "1", "lieferant": self.ingram.pk})
        p = Bestellung.objects.get(lieferant=self.ingram).positionen.get()
        self.assertEqual((p.artikel, p.preisliste_artikel.bestellnummer, p.ek), (None, "X2", D("4.0000")))

    def test_hinweis_und_verschieben_der_entwurfsposition(self):
        self.a.lieferanten.filter(lieferant=self.api).update(ek=D("30"))  # 6 × 30 + 10 = 190 gegen Kosatec 150
        b = services.bestellung_anlegen(self.m, self.api)
        p = services.bestellposition(b, self.a, D("6"))
        r = self.client.get(reverse("einkauf:bestellung", args=[b.pk]))
        self.assertContains(r, "Günstiger bei Kosatec: 150,00 €")
        self.assertContains(r, "bis versandfrei")
        self.assertContains(self.client.get(self.url, {"position": p.pk}), "Hierhin verschieben")
        self.client.post(reverse("einkauf:preisvergleich_bestellen"), {"position": p.pk, "lieferant": self.kosa.pk})
        self.assertFalse(b.positionen.exists())
        self.assertEqual(Bestellung.objects.get(lieferant=self.kosa).positionen.get().menge, D("6"))

    def test_bestellte_position_bleibt_und_fremder_mandant_geht_nicht(self):
        b = services.bestellung_anlegen(self.m, self.api)
        p = services.bestellposition(b, self.a, D("1"))
        services.bestellen(b)
        r = self.client.post(reverse("einkauf:preisvergleich_bestellen"), {"position": p.pk, "lieferant": self.kosa.pk}, follow=True)
        self.assertContains(r, "Nur Positionen aus Entwürfen")
        self.assertTrue(b.positionen.exists())
        zone = Mandant.objects.create(kurzname="ZONE", firmenname="ZONE Handel")
        fremd = services.bestellposition(services.bestellung_anlegen(zone, self.api), self.a, D("1"))
        r = self.client.post(reverse("einkauf:preisvergleich_bestellen"), {"position": fremd.pk, "lieferant": self.kosa.pk})
        self.assertEqual(r.status_code, 404)

    def test_gleiche_herstellernummer_anderes_produkt_zaehlt_nicht(self):
        anderer = Artikel.objects.create(nummer="B1", bezeichnung="Monitor", steuersatz=self.a.steuersatz, herstellernummer="M-1")
        Preislistenartikel.objects.create(lieferant=self.ingram, bestellnummer="I1", herstellernummer="M-1", bezeichnung="Monitor", ek=D("3"), artikel=anderer)
        Preislistenartikel.objects.create(lieferant=self.ingram, bestellnummer="I2", herstellernummer="M-1", ean="4111111111111", bezeichnung="Kabel", ek=D("3"))
        self.assertNotIn("Ingram", self.namen(vergleich.vergleich(self.m, D("1"), self.a)))

    def test_hinweis_rechnet_mit_echtem_ek_der_position(self):
        b = services.bestellung_anlegen(self.m, self.ingram)
        p = services.bestellposition(b, self.a, D("1"), ek=D("5"))  # kein Listenpreis, von Hand 5 €: Kosatec mit 25 € ist teurer
        p.bestellung = b
        self.assertIsNone(vergleich.alternative(p))

    def test_mehrere_entwuerfe_bezieht_sich_auf_die_eigene_bestellung(self):
        alt = services.bestellung_anlegen(self.m, self.api)
        p = services.bestellposition(alt, self.a, D("5"))  # 100 €
        neu = services.bestellung_anlegen(self.m, self.api)
        services.bestellposition(neu, self.a, D("24"))  # 480 €
        zeile = next(z for z in vergleich.vergleich(self.m, D("5"), self.a, ohne_position=p) if z["lieferant"] == self.api)
        self.assertEqual((zeile["entwurf"], zeile["versand"], zeile["gesamt"]), (alt, D("10"), D("110.00")))
        self.client.post(reverse("einkauf:preisvergleich_bestellen"), {"artikel": "A1", "menge": "1", "lieferant": self.api.pk, "bestellung": alt.pk})
        self.assertEqual(alt.positionen.get().menge, D("6"))  # in die gerade bearbeitete, nicht die neueste

    def test_anderer_preis_bleibt_eigene_zeile(self):
        b = services.bestellung_anlegen(self.m, self.kosa)
        services.bestellposition(b, self.a, D("1"), ek=D("40"))
        self.client.post(reverse("einkauf:preisvergleich_bestellen"), {"artikel": "A1", "menge": "5", "lieferant": self.kosa.pk})
        self.assertEqual(sorted((p.menge, p.ek) for p in b.positionen.all()), [(D("1.000"), D("40.0000")), (D("5.000"), D("25.0000"))])
        self.assertEqual(b.summe_netto, D("165.00"))

    def test_unbekannter_versand_ist_nicht_am_guenstigsten(self):
        ArtikelLieferant.objects.create(artikel=self.a, lieferant=self.ingram, bestellnummer="I", ek=D("19"), bestand=50)
        r = self.client.get(self.url, {"artikel": "A1", "menge": "6"})
        self.assertContains(r, "+ ?")
        self.assertNotContains(r, "günstigste Wahl")

    def test_lieferbares_angebot_schlaegt_billigeres_ohne_bestand(self):
        ArtikelLieferant.objects.create(artikel=self.a, lieferant=self.api, bestellnummer="API-2", ek=D("15"), bestand=0)
        zeile = next(z for z in vergleich.vergleich(self.m, D("1"), self.a) if z["lieferant"] == self.api)
        self.assertEqual((zeile["ek"], zeile["bestellnummer"], zeile["lieferbar"]), (D("20.0000"), "API-1", True))

    def test_menge_aendern_behaelt_das_produkt(self):
        r = self.client.get(self.url, {"artikel": "A1", "menge": "1"})
        self.assertContains(r, '<input type="hidden" name="artikel" value="A1">')
        r = self.client.get(self.url, {"artikel": "A1", "q": "", "menge": "6"})
        self.assertContains(r, "Wo bestellen? 6 ×")
        self.assertContains(self.client.get(self.url, {"artikel": "A1", "q": "maus", "menge": "6"}), "?artikel=A1")  # neue Suche

    def test_extreme_mengen(self):
        for menge in ("1e30", "1e-9", "99999999"):
            self.assertEqual(self.client.get(self.url, {"artikel": "A1", "menge": menge}).status_code, 200, menge)
            r = self.client.post(reverse("einkauf:preisvergleich_bestellen"), {"artikel": "A1", "menge": menge, "lieferant": self.api.pk})
            self.assertEqual(r.status_code, 302, menge)
        self.assertFalse(Bestellung.objects.exists())

    def test_position_menge_und_preis_nachtraeglich_aendern(self):
        b = services.bestellung_anlegen(self.m, self.kosa)
        p = services.bestellposition(b, self.a, D("1"), ek=D("15.6543"))
        self.assertEqual(p.ek, D("15.65"))  # EK × Menge geht sichtbar auf
        url = reverse("einkauf:bestellung_position_aendern", args=[b.pk, p.pk])
        self.assertContains(self.client.get(reverse("einkauf:bestellung", args=[b.pk])), 'value="15,65"')
        self.client.post(url, {"menge": "2,5", "ek": "12,346"})
        p.refresh_from_db()
        self.assertEqual((p.menge, p.ek, p.netto), (D("2.500"), D("12.35"), D("30.88")))
        self.client.post(url, {"menge": "", "ek": ""})  # geleerte Felder behalten den Wert
        p.refresh_from_db()
        self.assertEqual((p.menge, p.ek), (D("2.500"), D("12.35")))
        for daten in ({"menge": "0", "ek": "1"}, {"menge": "1", "ek": "-1"}, {"menge": "abc", "ek": "1"}, {"menge": "1", "ek": "NaN"}):
            self.assertEqual(self.client.post(url, daten).status_code, 302, daten)
        p.refresh_from_db()
        self.assertEqual((p.menge, p.ek), (D("2.500"), D("12.35")))

    def test_position_im_offenen_wareneingang_bleibt_fest(self):
        b = services.bestellung_anlegen(self.m, self.kosa)
        p = services.bestellposition(b, self.a, D("10"), ek=D("5"))
        services.bestellen(b)
        self.client.post(reverse("einkauf:wareneingang_neu"), {"bestellung": b.pk})
        r = self.client.post(reverse("einkauf:bestellung_position_aendern", args=[b.pk, p.pk]), {"menge": "6", "ek": "4"}, follow=True)
        self.assertContains(r, "offenen Wareneingang")
        p.refresh_from_db()
        self.assertEqual(p.menge, D("10.000"))
        self.assertNotContains(self.client.get(reverse("einkauf:bestellung", args=[b.pk])), f'id="pos{p.pk}"')

    def test_versand_im_entwurf_ist_nicht_frei(self):
        l = Lieferant.objects.create(nummer="L4", name1="Fix", kurzname="Fix", versandkosten=D("5"))
        ArtikelLieferant.objects.create(artikel=self.a, lieferant=l, bestellnummer="F", ek=D("10"), bestand=50)
        services.bestellposition(services.bestellung_anlegen(self.m, l), self.a, D("1"))
        zeile = next(z for z in vergleich.vergleich(self.m, D("1"), self.a) if z["lieferant"] == l)
        self.assertEqual((zeile["versand"], zeile["versandfrei"]), (D("0"), False))
        self.assertIn("Versand schon im Entwurf", zeile["hinweise"])
        self.assertTrue(next(z for z in vergleich.vergleich(self.m, D("1"), self.a) if z["lieferant"] == self.kosa)["versandfrei"])

    def test_eingetragener_preis_gilt_beim_uebernehmen(self):
        b = services.bestellung_anlegen(self.m, self.kosa)
        self.client.post(reverse("einkauf:preisvergleich_bestellen"), {"artikel": "A1", "menge": "1", "lieferant": self.kosa.pk, "bestellung": b.pk, "ek": "9,99"})
        self.assertEqual(b.positionen.get().ek, D("9.99"))
        self.assertEqual(self.client.post(reverse("einkauf:preisvergleich_bestellen"), {"artikel": "A1", "menge": "1", "lieferant": self.kosa.pk, "ek": "-1"}).status_code, 302)
        self.assertEqual(b.positionen.count(), 1)

    def test_gelieferte_position_und_stornierte_bestellung_bleiben_fest(self):
        b = services.bestellung_anlegen(self.m, self.kosa)
        p = services.bestellposition(b, self.a, D("2"))
        p.geliefert = D("1")
        p.save()
        url = reverse("einkauf:bestellung_position_aendern", args=[b.pk, p.pk])
        self.assertNotContains(self.client.get(reverse("einkauf:bestellung", args=[b.pk])), f'id="pos{p.pk}"')
        self.client.post(url, {"menge": "9", "ek": "1"})
        p.refresh_from_db()
        self.assertEqual(p.menge, D("2.000"))
        p.geliefert = D("0")
        p.save()
        b.status = BestellStatus.STORNIERT
        b.save()
        self.client.post(url, {"menge": "9", "ek": "1"})
        p.refresh_from_db()
        self.assertEqual(p.menge, D("2.000"))

    def test_preise_mit_zwei_stellen(self):
        ArtikelLieferant.objects.filter(artikel=self.a, lieferant=self.api).update(ek=D("15.6500"))
        r = self.client.get(self.url, {"artikel": "A1"})
        self.assertContains(r, "15,65")
        self.assertNotContains(r, "15,6500")
        b = services.bestellung_anlegen(self.m, self.api)
        d = self.client.get(reverse("einkauf:bestellartikel_suche", args=[b.pk]), {"q": "maus"}).json()
        self.assertEqual(d["treffer"][0]["ek"], "15,65")

    def test_ungueltige_eingaben_ohne_500(self):
        self.assertEqual(self.client.get(self.url, {"menge": "abc", "position": "x", "pl": "y"}).status_code, 200)
        self.assertEqual(self.client.get(self.url, {"artikel": "A1", "menge": "-3"}).status_code, 200)
        for daten in ({"artikel": "A1", "menge": "1", "lieferant": "x"}, {"artikel": "A1", "menge": "0", "lieferant": self.api.pk},
                      {"menge": "1", "lieferant": self.api.pk}, {"artikel": "A1", "menge": "1", "lieferant": self.ingram.pk}):
            self.assertEqual(self.client.post(reverse("einkauf:preisvergleich_bestellen"), daten).status_code, 302, daten)
        self.assertFalse(Bestellung.objects.exists())
