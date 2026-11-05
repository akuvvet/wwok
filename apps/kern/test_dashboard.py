from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.kern import dashboard
from apps.kern.models import Mandant
from apps.stammdaten.models import Artikel, Kunde, Steuersatz, Zahlungsbedingung
from apps.verkauf.models import Beleg, Belegart, Position

HEUTE = date(2026, 3, 31)


@override_settings(SECURE_SSL_REDIRECT=False)
class DashboardTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.fremd = Mandant.objects.create(kurzname="ZONE", firmenname="ZONE Handel")
        self.kunde = Kunde.objects.create(nummer="1", name1="Kunde GmbH")
        self.user = get_user_model().objects.create_user("a", "a@a.de", "x" * 14)
        self.client.force_login(self.user)

    def mit_mandant(self, mandant=None):
        s = self.client.session
        s["mandant_id"] = (mandant or self.m).pk
        s.save()

    def beleg(self, art=Belegart.RECHNUNG, netto="100", tag=HEUTE, abgeschlossen=True, mandant=None, **extra):
        """Beleg mit festen Summen; abgeschlossen wird am Ende gesetzt, damit Positionen noch anlegbar sind."""
        b = Beleg.objects.create(mandant=mandant or self.m, art=art, kunde=self.kunde, belegdatum=tag, **extra)
        netto = Decimal(netto)
        Beleg.objects.filter(pk=b.pk).update(abgeschlossen=abgeschlossen, summe_netto=netto, summe_brutto=netto * Decimal("1.19"))
        b.refresh_from_db()
        return b

    # --- Seite ---
    def test_seite_laedt_ohne_daten_und_mit_daten(self):
        self.mit_mandant()
        antwort = self.client.get(reverse("dashboard"))
        self.assertEqual(antwort.status_code, 200)
        self.assertContains(antwort, "Dashboard")
        self.beleg(tag=timezone.localdate(), bezugnahme="Shop")
        self.beleg(Belegart.AUFTRAG, abgeschlossen=False)
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 200)
        for tage in ("7", "90", "365"):
            self.assertEqual(self.client.get(reverse("dashboard"), {"tage": tage}).status_code, 200)

    def test_ohne_login_zur_anmeldung(self):
        self.client.logout()
        antwort = self.client.get(reverse("dashboard"))
        self.assertEqual(antwort.status_code, 302)
        self.assertIn("login", antwort["Location"])

    def test_ohne_mandant_weiterleitung(self):
        antwort = self.client.get(reverse("dashboard"))  # zwei aktive Mandanten, keiner gewählt
        self.assertRedirects(antwort, reverse("verkauf:start"), fetch_redirect_response=False)

    def test_ungueltiges_tage(self):
        for wert in ("abc", "5", "-30", "", "1e3", None):
            self.assertEqual(dashboard.zeitraum_pruefen(wert), 30, wert)
        self.assertEqual(dashboard.zeitraum_pruefen("90"), 90)
        self.mit_mandant()
        antwort = self.client.get(reverse("dashboard"), {"tage": "abc"})
        self.assertEqual(antwort.status_code, 200)
        self.assertEqual(antwort.context["tage"], 30)

    def test_start_ist_dashboard(self):
        self.assertEqual(reverse("dashboard"), "/")

    # --- Umsatz ---
    def test_umsatz_rechnungen_minus_gutschriften(self):
        self.beleg(netto="100")
        self.beleg(netto="50", tag=HEUTE - timedelta(days=2))
        self.beleg(Belegart.GUTSCHRIFT, netto="30")
        erg = dashboard.umsatz(self.m, HEUTE - timedelta(days=5), HEUTE)
        self.assertEqual(erg["netto"], Decimal("120.00"))
        self.assertEqual(erg["anzahl"], 2)  # Gutschrift ist keine Rechnung

    def test_proforma_angebot_lieferschein_zaehlen_nicht(self):
        for art in (Belegart.PROFORMA, Belegart.ANGEBOT, Belegart.LIEFERSCHEIN, Belegart.AUFTRAG, Belegart.NACHLIEFERSCHEIN):
            self.beleg(art, netto="999")
        erg = dashboard.umsatz(self.m, HEUTE, HEUTE)
        self.assertEqual((erg["netto"], erg["anzahl"]), (Decimal("0"), 0))

    def test_nicht_abgeschlossene_rechnung_zaehlt_nicht(self):
        self.beleg(netto="100", abgeschlossen=False)
        self.assertEqual(dashboard.umsatz(self.m, HEUTE, HEUTE)["netto"], Decimal("0"))

    def test_zeitraumgrenzen_einschliesslich(self):
        self.beleg(netto="10", tag=HEUTE - timedelta(days=6))
        self.beleg(netto="20", tag=HEUTE - timedelta(days=7))  # außerhalb von 7 Tagen
        von, bis, _, _ = dashboard.zeitraeume(HEUTE, 7)
        self.assertEqual(dashboard.umsatz(self.m, von, bis)["netto"], Decimal("10.00"))

    def test_mandantentrennung(self):
        self.beleg(netto="100", mandant=self.fremd)
        self.beleg(Belegart.AUFTRAG, netto="500", mandant=self.fremd, abgeschlossen=False)
        self.assertEqual(dashboard.umsatz(self.m, HEUTE, HEUTE)["netto"], Decimal("0"))
        self.assertEqual(dashboard.offene_kundenbestellungen(self.m)["anzahl"], 0)
        self.assertEqual(dashboard.kanaele(self.m, HEUTE, HEUTE), [])
        self.assertEqual(dashboard.top_kunden(self.m, HEUTE, HEUTE), [])
        self.assertEqual(dashboard.offene_posten(self.m, HEUTE)["anzahl"], 0)
        self.assertEqual(dashboard.umsatz(self.fremd, HEUTE, HEUTE)["netto"], Decimal("100.00"))

    # --- Prozent ---
    def test_veraenderung(self):
        self.assertEqual(dashboard.veraenderung(110, 100), {"art": "prozent", "prozent": Decimal("10.0"), "richtung": "auf"})
        self.assertEqual(dashboard.veraenderung(75, 100)["richtung"], "ab")
        self.assertEqual(dashboard.veraenderung(75, 100)["prozent"], Decimal("25.0"))
        self.assertEqual(dashboard.veraenderung(100, 100)["richtung"], "gleich")

    def test_veraenderung_vorzeitraum_null(self):
        self.assertEqual(dashboard.veraenderung(50, 0)["art"], "neu")
        self.assertIsNone(dashboard.veraenderung(50, 0)["prozent"])
        self.assertEqual(dashboard.veraenderung(0, 0)["art"], "keine")

    # --- Monatsvergleich ---
    def test_monatsvergleich_am_31_gegen_februar(self):
        self.beleg(netto="100", tag=date(2026, 3, 31))
        self.beleg(netto="40", tag=date(2026, 2, 28))  # letzter Februartag: gehört zum Vergleich (gekappt)
        self.beleg(netto="60", tag=date(2026, 2, 10))
        self.beleg(netto="7", tag=date(2026, 1, 15))
        vergleiche = {v["titel"]: v for v in dashboard.zeitraumvergleiche(self.m, HEUTE)}
        monat = vergleiche["Dieser Monat"]
        self.assertEqual(monat["a"]["netto"], Decimal("100.00"))
        self.assertEqual(monat["b"]["netto"], Decimal("100.00"))  # 1.-28.02.
        self.assertEqual(monat["veraenderung"]["richtung"], "gleich")
        letzter = vergleiche["Letzter Monat"]  # Februar gegen Januar
        self.assertEqual((letzter["a"]["netto"], letzter["b"]["netto"]), (Decimal("100.00"), Decimal("7.00")))

    def test_monatsvergleich_am_1_im_januar(self):
        heute = date(2026, 1, 1)
        self.beleg(netto="10", tag=date(2026, 1, 1))
        self.beleg(netto="20", tag=date(2025, 12, 1))
        monat = {v["titel"]: v for v in dashboard.zeitraumvergleiche(self.m, heute)}["Dieser Monat"]
        self.assertEqual((monat["a"]["netto"], monat["b"]["netto"]), (Decimal("10.00"), Decimal("20.00")))

    def test_tagesvergleiche(self):
        self.beleg(netto="30", tag=HEUTE)
        self.beleg(netto="20", tag=HEUTE - timedelta(days=1))
        self.beleg(netto="10", tag=HEUTE - timedelta(days=2))
        v = {x["titel"]: x for x in dashboard.zeitraumvergleiche(self.m, HEUTE)}
        self.assertEqual(v["Heute"]["veraenderung"]["prozent"], Decimal("50.0"))
        self.assertEqual(v["Gestern"]["veraenderung"]["prozent"], Decimal("100.0"))

    # --- Offene Bestellungen / Posten ---
    def test_offene_kundenbestellungen(self):
        offen = self.beleg(Belegart.AUFTRAG, netto="200", abgeschlossen=True)
        Position.objects.create(beleg=offen, bezeichnung="x", einzelpreis=Decimal("1"))
        entwurf = self.beleg(Belegart.AUFTRAG, netto="50", abgeschlossen=False)
        Position.objects.create(beleg=entwurf, bezeichnung="x", einzelpreis=Decimal("1"))
        geliefert = self.beleg(Belegart.AUFTRAG, netto="900")
        gp = Position.objects.create(beleg=geliefert, bezeichnung="x", einzelpreis=Decimal("1"))
        ls = self.beleg(Belegart.LIEFERSCHEIN, vorgaenger=geliefert, abgeschlossen=False)
        Position.objects.create(beleg=ls, bezeichnung="x", einzelpreis=Decimal("1"), quellposition=gp)  # voll geliefert
        Beleg.objects.filter(pk=ls.pk).update(abgeschlossen=True)
        teil = self.beleg(Belegart.AUFTRAG, netto="300")  # Teillieferung: 4 von 10 geliefert, bleibt offen
        tp = Position.objects.create(beleg=teil, bezeichnung="x", einzelpreis=Decimal("1"), menge=Decimal("10"))
        tls = self.beleg(Belegart.LIEFERSCHEIN, vorgaenger=teil, abgeschlossen=False)
        Position.objects.create(beleg=tls, bezeichnung="x", einzelpreis=Decimal("1"), menge=Decimal("4"), quellposition=tp)
        Beleg.objects.filter(pk=tls.pk).update(abgeschlossen=True)
        leer = self.beleg(Belegart.AUFTRAG, netto="5")  # ohne Positionen zählt nicht
        erg = dashboard.offene_kundenbestellungen(self.m)
        self.assertEqual((erg["anzahl"], erg["netto"]), (3, Decimal("550.00")))
        self.assertNotEqual(leer.pk, offen.pk)

    def test_kachelzahlen_gleich_zeilen_der_verlinkten_listen(self):
        from apps.einkauf.models import Bestellung
        from apps.stammdaten.models import Lieferant
        heute = timezone.localdate()
        self.mit_mandant()
        # Kundenbestellungen: 2 offen, 1 geliefert, 1 Entwurf zählt mit, 1 ohne Position zählt nicht
        for netto in ("10", "20"):
            b = self.beleg(Belegart.AUFTRAG, netto=netto, tag=heute)
            Position.objects.create(beleg=b, bezeichnung="x", einzelpreis=Decimal("1"))
        gel = self.beleg(Belegart.AUFTRAG, tag=heute)
        gp = Position.objects.create(beleg=gel, bezeichnung="x", einzelpreis=Decimal("1"))
        ls = self.beleg(Belegart.LIEFERSCHEIN, vorgaenger=gel, abgeschlossen=False, tag=heute)
        Position.objects.create(beleg=ls, bezeichnung="x", einzelpreis=Decimal("1"), quellposition=gp)
        Beleg.objects.filter(pk=ls.pk).update(abgeschlossen=True)
        self.beleg(Belegart.AUFTRAG, tag=heute)
        # Angebote: gültig, abgelaufen, mit Folgebeleg
        gueltig = self.beleg(Belegart.ANGEBOT, tag=heute, gueltig_bis=heute + timedelta(days=5))
        abgelaufen = self.beleg(Belegart.ANGEBOT, tag=heute, gueltig_bis=heute - timedelta(days=1))
        folge = self.beleg(Belegart.ANGEBOT, tag=heute)
        self.beleg(Belegart.AUFTRAG, tag=heute, vorgaenger=folge, abgeschlossen=False)
        for b in (gueltig, abgelaufen, folge):
            Position.objects.create(beleg=b, bezeichnung="x", einzelpreis=Decimal("1"))
        # Lieferantenbestellungen
        lief = Lieferant.objects.create(nummer="L1", name1="Lieferant AG")
        for i, st in enumerate(("entwurf", "bestellt", "teil", "geliefert", "storniert")):
            Bestellung.objects.create(mandant=self.m, lieferant=lief, nummer=f"B{i}", status=st, datum=heute)

        k = dashboard.kacheln(self.m, 30, heute)
        ab = self.client.get(reverse("verkauf:belege"), {"art": "AB", "status": "dashboard"})
        self.assertEqual(k["bestellungen"]["anzahl"], ab.context["anzahl"])
        self.assertEqual(ab.context["anzahl"], 2)
        an = self.client.get(reverse("verkauf:belege"), {"art": "AN", "status": "dashboard"})
        self.assertEqual(dashboard.offene_angebote(self.m, heute), an.context["anzahl"])
        self.assertEqual(an.context["anzahl"], 1)
        eb = self.client.get(reverse("einkauf:bestellungen"), {"status": "bestellt"})
        self.assertEqual(k["lieferanten"]["bestellungen_anzahl"], len(eb.context["bestellungen"]))
        self.assertEqual(len(eb.context["bestellungen"]), 2)
        # Links im Dashboard
        html = self.client.get(reverse("dashboard")).content.decode()
        for teil in ("art=AB&amp;status=dashboard", "art=AN&amp;status=dashboard", "status=bestellt"):
            self.assertTrue(teil in html or teil.replace("&amp;", "&") in html, teil)

    def test_top_artikel_und_lieferanten_sind_verlinkt(self):
        from apps.einkauf.models import Lieferantenrechnung
        from apps.stammdaten.models import Lieferant
        self.mit_mandant()
        art = Artikel.objects.create(nummer="A1", bezeichnung="Teil", steuersatz=Steuersatz.objects.get_or_create(bezeichnung="19", defaults={"prozent": Decimal("19")})[0])
        heute = timezone.localdate()
        b = self.beleg(Belegart.RECHNUNG, netto="100", tag=heute, abgeschlossen=False)  # Position erst, dann abschließen
        Position.objects.create(beleg=b, artikel=art, bezeichnung="Teil", einzelpreis=Decimal("100"))
        Beleg.objects.filter(pk=b.pk).update(abgeschlossen=True)
        lief = Lieferant.objects.create(nummer="L1", name1="Lieferant AG")
        Lieferantenrechnung.objects.create(mandant=self.m, lieferant=lief, rechnungsnummer="R1", datum=heute,
                                           faellig=heute, netto=Decimal("50"), brutto=Decimal("59.5"))
        html = self.client.get(reverse("dashboard")).content.decode()
        self.assertIn(reverse("lager:artikel", args=[art.pk]), html)
        self.assertIn(reverse("stammdaten:lieferant_bearbeiten", args=[lief.pk]), html)

    def test_offene_posten_mit_zahlung_und_ueberfaellig(self):
        from apps.finanzen.models import Zahlung
        zb = Zahlungsbedingung.objects.create(bezeichnung="10 Tage", tage_netto=10, tage_mahnung=20)
        alt = self.beleg(netto="100", tag=HEUTE - timedelta(days=30), zahlungsbedingung=zb)
        self.beleg(netto="100", tag=HEUTE - timedelta(days=3), zahlungsbedingung=zb)  # noch nicht fällig
        bezahlt = self.beleg(netto="100", tag=HEUTE - timedelta(days=30), zahlungsbedingung=zb)
        Zahlung.objects.create(mandant=self.m, beleg=bezahlt, betrag=bezahlt.summe_brutto, datum=HEUTE)
        Zahlung.objects.create(mandant=self.m, beleg=alt, betrag=Decimal("19"), datum=HEUTE)
        erg = dashboard.offene_posten(self.m, HEUTE)
        self.assertEqual(erg["anzahl"], 2)
        self.assertEqual(erg["betrag"], Decimal("119.00") - Decimal("19") + Decimal("119.00"))
        self.assertEqual((erg["ueberfaellig_anzahl"], erg["ueberfaellig_betrag"]), (1, Decimal("100.00")))

    # --- Listen ---
    def test_top_kunden_und_kanaele(self):
        zweiter = Kunde.objects.create(nummer="2", name1="Zweiter AG")
        self.beleg(netto="100", bezugnahme="Shop")
        b = self.beleg(netto="300", bezugnahme="shop ")
        Beleg.objects.filter(pk=b.pk).update(kunde=zweiter)
        self.beleg(netto="100")
        self.beleg(Belegart.GUTSCHRIFT, netto="40")
        kunden = dashboard.top_kunden(self.m, HEUTE, HEUTE)
        self.assertEqual([k["kunde__name1"] for k in kunden], ["Zweiter AG", "Kunde GmbH"])
        self.assertEqual(kunden[1]["umsatz"], Decimal("160.00"))  # 200 − 40
        kanaele = dashboard.kanaele(self.m, HEUTE, HEUTE)
        self.assertEqual(kanaele[0]["name"], "Shop")
        self.assertEqual(kanaele[0]["umsatz"], Decimal("400.00"))
        self.assertEqual(kanaele[0]["prozent"], Decimal("80.0"))
        self.assertEqual(kanaele[1]["name"], "ohne Angabe")

    def test_top_artikel_mit_gutschrift(self):
        artikel = Artikel.objects.create(nummer="A1", bezeichnung="Maus", steuersatz=Steuersatz.objects.get_or_create(bezeichnung="19", defaults={"prozent": Decimal("19")})[0])
        re = self.beleg(abgeschlossen=False)
        Position.objects.create(beleg=re, artikel=artikel, bezeichnung="Maus", menge=Decimal("3"), einzelpreis=Decimal("10"), rabatt_prozent=Decimal("10"))
        gs = self.beleg(Belegart.GUTSCHRIFT, abgeschlossen=False)
        Position.objects.create(beleg=gs, artikel=artikel, bezeichnung="Maus", menge=Decimal("1"), einzelpreis=Decimal("10"))
        Beleg.objects.filter(pk__in=[re.pk, gs.pk]).update(abgeschlossen=True)
        liste = dashboard.top_artikel(self.m, HEUTE, HEUTE)
        self.assertEqual(len(liste), 1)
        self.assertEqual(liste[0]["umsatz"], Decimal("17.00"))  # 27 − 10
        self.assertEqual(liste[0]["stueck"], Decimal("2"))

    def test_leere_datenbank_alles_null(self):
        k = dashboard.kacheln(self.m, 30, HEUTE)
        self.assertEqual(k["umsatz"]["netto"], Decimal("0"))
        self.assertEqual(k["lieferanten"]["bestellungen_anzahl"], 0)
        self.assertEqual(dashboard.arbeitsliste(self.m, HEUTE, HEUTE)["mahnungen"], 0)
        v = dashboard.verlauf(self.m, 365, HEUTE)
        self.assertEqual(len(v["punkte"]), 53)
        self.assertEqual(len(dashboard.verlauf(self.m, 7, HEUTE)["punkte"]), 7)

    def test_verlauf_summe_gleich_umsatz(self):
        self.beleg(netto="100", tag=HEUTE)
        self.beleg(netto="25", tag=HEUTE - timedelta(days=40))
        self.assertEqual(dashboard.verlauf(self.m, 90, HEUTE)["summe"], Decimal("125.00"))
        self.assertEqual(dashboard.verlauf(self.m, 30, HEUTE)["summe"], Decimal("100.00"))

    def test_lieferanten_kennzahlen(self):
        from apps.einkauf.models import BestellPosition, Bestellung, Lieferantenrechnung
        from apps.stammdaten.models import Lieferant
        lief = Lieferant.objects.create(nummer="L1", name1="Lieferant AG")
        b = Bestellung.objects.create(mandant=self.m, lieferant=lief, nummer="B1", status="teil", datum=HEUTE)
        BestellPosition.objects.create(bestellung=b, bezeichnung="x", menge=Decimal("10"), ek=Decimal("5"), geliefert=Decimal("4"))
        Bestellung.objects.create(mandant=self.m, lieferant=lief, nummer="B2", status="entwurf", datum=HEUTE)
        Bestellung.objects.create(mandant=self.fremd, lieferant=lief, nummer="B3", status="bestellt", datum=HEUTE)
        Lieferantenrechnung.objects.create(mandant=self.m, lieferant=lief, rechnungsnummer="R1", datum=HEUTE, faellig=HEUTE - timedelta(days=1),
                                           netto=Decimal("100"), brutto=Decimal("119"))
        Lieferantenrechnung.objects.create(mandant=self.m, lieferant=lief, rechnungsnummer="R2", datum=HEUTE, faellig=HEUTE,
                                           netto=Decimal("10"), brutto=Decimal("11.9"), status="bezahlt")
        erg = dashboard.lieferanten(self.m, HEUTE)
        self.assertEqual((erg["bestellungen_anzahl"], erg["bestellungen_netto"]), (1, Decimal("30.00")))
        self.assertEqual((erg["rechnungen_anzahl"], erg["rechnungen_brutto"], erg["rechnungen_ueberfaellig"]), (1, Decimal("119.00"), 1))
        top = dashboard.top_lieferanten(self.m, HEUTE, HEUTE)
        self.assertEqual(top[0]["volumen"], Decimal("110.00"))

    def test_lieferanten_ueberlieferte_position_kappt_nur_sich_selbst(self):
        from apps.einkauf.models import BestellPosition, Bestellung
        from apps.stammdaten.models import Lieferant
        lief = Lieferant.objects.create(nummer="L2", name1="Lieferant 2")
        b = Bestellung.objects.create(mandant=self.m, lieferant=lief, nummer="B9", status="teil", datum=HEUTE)
        BestellPosition.objects.create(bestellung=b, bezeichnung="a", menge=Decimal("10"), ek=Decimal("100"), geliefert=Decimal("15"))
        BestellPosition.objects.create(bestellung=b, bezeichnung="b", menge=Decimal("10"), ek=Decimal("100"), geliefert=Decimal("0"))
        self.assertEqual(dashboard.lieferanten(self.m, HEUTE)["bestellungen_netto"], Decimal("1000.00"))

    def test_arbeitsliste_wiedervorlagen_nur_faellige_des_eigenen_mandanten(self):
        from apps.shop.models import Shop, ShopBestellung
        from apps.verkauf.models import Wiedervorlage
        Wiedervorlage.objects.create(mandant=self.m, kunde=self.kunde, faellig=HEUTE, text="heute")
        Wiedervorlage.objects.create(mandant=self.m, kunde=self.kunde, faellig=HEUTE + timedelta(days=1), text="morgen")
        Wiedervorlage.objects.create(mandant=self.m, kunde=self.kunde, faellig=HEUTE, text="erledigt", erledigt=True)
        Wiedervorlage.objects.create(mandant=self.fremd, kunde=self.kunde, faellig=HEUTE, text="fremd")
        shop = Shop.objects.create(mandant=self.m, name="S", url="https://s.example", client_id="x")
        ShopBestellung.objects.create(shop=shop, shop_id_extern="1", bestellnummer="1")
        erg = dashboard.arbeitsliste(self.m, HEUTE - timedelta(days=1), HEUTE)
        self.assertEqual(erg["wiedervorlagen"], 1)
        self.assertEqual(erg["shop_bestellungen"], 1)
        self.assertEqual(dashboard.arbeitsliste(self.m, timezone.localdate() + timedelta(days=1), HEUTE)["shop_bestellungen"], 0)

    def test_mandantenwechsel_leitet_auf_weiter_oder_dashboard(self):
        url = reverse("verkauf:start")
        a = self.client.post(url, {"mandant": self.fremd.pk, "weiter": reverse("verkauf:belege")})
        self.assertRedirects(a, reverse("verkauf:belege"), fetch_redirect_response=False)
        self.assertEqual(self.client.session["mandant_id"], self.fremd.pk)
        for boese in ("https://boese.example/x", "//boese.example/x", "javascript:alert(1)", ""):
            a = self.client.post(url, {"mandant": self.m.pk, "weiter": boese})
            self.assertRedirects(a, reverse("dashboard"), fetch_redirect_response=False, msg_prefix=boese)
        a = self.client.post(url, {"mandant": self.m.pk})
        self.assertRedirects(a, url, fetch_redirect_response=False)

    def test_seitenleiste_genau_ein_aria_current_auf_gleichnamigen_seiten(self):
        self.mit_mandant()
        b = self.beleg(Belegart.AUFTRAG, abgeschlossen=False)
        for name in ("verkauf:beleg", "esd:beleg"):
            html = self.client.get(reverse(name, args=[b.pk])).content.decode()
            self.assertEqual(html.count('aria-current="page"'), 1, name)
        self.assertNotIn('aria-current="page" href="' + reverse("finanzen:offene_posten"),
                         self.client.get(reverse("verkauf:beleg", args=[b.pk])).content.decode())
