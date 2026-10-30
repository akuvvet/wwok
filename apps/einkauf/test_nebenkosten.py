"""Nebenkosten im Wareneingang: Kalkulations-EK als eigentlicher EK, Abgleich zur Rechnung, Lieferanten-Suchfeld."""
from datetime import date
from decimal import Decimal as D

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.kern.models import Mandant
from apps.lager.models import bestand
from apps.stammdaten.models import Artikel, ArtikelMandant, Kunde, Lieferant, Steuersatz
from apps.verkauf.models import Beleg, Belegart

from . import services
from .models import Lieferantenrechnung


@override_settings(SECURE_SSL_REDIRECT=False)
class NebenkostenTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.lf = Lieferant.objects.create(nummer="L1", name1="API GmbH", kurzname="API")
        st = Steuersatz.objects.create(bezeichnung="19", prozent=D("19"))
        self.a = Artikel.objects.create(nummer="A1", bezeichnung="Maus", steuersatz=st)
        self.b = Artikel.objects.create(nummer="B1", bezeichnung="Monitor", steuersatz=st)
        self.user = get_user_model().objects.create_user("t", password="x" * 14, is_staff=True)
        self.client.force_login(self.user)
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    def we(self, *zeilen, versand="0", versicherung="0", verpackung="0"):
        we = services.wareneingang_anlegen(self.m, self.lf)
        for artikel, menge, ek in zeilen:
            services.wareneingang_erfassen(we, artikel, D(menge), D(ek))
        we.versandkosten, we.versicherung, we.verpackung = D(versand), D(versicherung), D(verpackung)
        we.save()
        return we

    def test_verteilung_nach_warenwert(self):
        we = self.we((self.a, "10", "10"), (self.b, "1", "100"), versand="15", versicherung="3", verpackung="2")  # 200 € Ware + 20 €
        self.assertEqual(we.nebenkosten, D("20"))
        kalk = we.kalkulation()
        ek = {p.artikel: kalk[p.pk] for p in we.positionen.all()}
        self.assertEqual((ek[self.a], ek[self.b]), (D("11.0000"), D("110.0000")))  # je 10 % Aufschlag
        services.wareneingang_buchen(we)
        self.assertEqual(sum(p.kalk_ek * p.menge for p in we.positionen.all()), D("220.0000"))

    def test_durchschnitts_ek_ist_der_kalkulations_ek_letzter_ek_bleibt_lieferantenpreis(self):
        services.wareneingang_buchen(self.we((self.a, "10", "10"), versand="10"))
        self.a.refresh_from_db()
        self.assertEqual(self.a.durchschnitts_ek, D("11.0000"))
        self.assertEqual(self.a.ek_errechnet, D("11.0000"))  # Grundlage für Marge und VK
        self.assertEqual(ArtikelMandant.objects.get(artikel=self.a, mandant=self.m).letzter_ek, D("10.0000"))
        self.assertEqual(services.ek_vorschlag(self.m, self.lf, self.a)[0], D("10.0000"))  # nächste Bestellung ohne Versand
        services.wareneingang_buchen(self.we((self.a, "10", "20")))  # gleitend: (10 × 11 + 10 × 20) / 20
        self.a.refresh_from_db()
        self.assertEqual(self.a.durchschnitts_ek, D("15.5000"))
        self.assertEqual(bestand(self.m, self.a), D("20"))

    def test_anderer_mandant_bekommt_lieferantenpreis_ohne_fremde_nebenkosten(self):
        services.wareneingang_buchen(self.we((self.a, "10", "10"), versand="10"))
        zone = Mandant.objects.create(kurzname="ZONE", firmenname="ZONE Handel")
        self.assertEqual(services.ek_vorschlag(zone, self.lf, self.a)[0], D("10.0000"))
        self.assertEqual(services.ek_vorschlag(zone, self.lf, self.b)[0], D("0"))

    def test_veralteter_tab_setzt_gebucht_nicht_zurueck(self):
        we = self.we((self.a, "2", "10"))
        url = reverse("einkauf:wareneingang_buchen", args=[we.pk])
        self.client.post(url, {"versandkosten": "0"})
        self.client.post(url, {"aktion": "kosten", "versandkosten": "5"})  # zweiter Tab mit altem Stand
        we.refresh_from_db()
        self.assertEqual((we.gebucht, we.versandkosten), (True, D("0.00")))
        self.assertEqual(bestand(self.m, self.a), D("2"))

    def test_rechnungsmaske_zeigt_wareneingang_mit_nebenkosten(self):
        we = self.we((self.a, "10", "10"), versand="10")
        services.wareneingang_buchen(we)
        r = services.rechnung_erfassen(self.m, self.lf, "R-9", date.today(), D("110"), D("130.90"))
        self.assertContains(self.client.get(reverse("einkauf:rechnung", args=[r.pk])), "110,00")

    def test_ohne_nebenkosten_aendert_sich_nichts_und_ohne_warenwert_nach_menge(self):
        we = self.we((self.a, "3", "7.1234"))  # EK im Wareneingang hat 2 Stellen
        self.assertEqual(list(we.kalkulation().values()), [D("7.1200")])
        kostenlos = self.we((self.a, "2", "0"), (self.b, "3", "0"), versand="10")
        self.assertEqual(set(kostenlos.kalkulation().values()), {D("2.0000")})

    def test_rechnungsabgleich_rechnet_nebenkosten_mit(self):
        we = self.we((self.a, "10", "10"), versand="10")
        services.wareneingang_buchen(we)
        r = services.rechnung_erfassen(self.m, self.lf, "R-1", date.today(), D("110"), D("130.90"), wareneingaenge=[we])
        a = r.abgleich()
        self.assertEqual((a["wareneingang_netto"], a["ok"]), (D("110.00"), True))
        r2 = services.rechnung_erfassen(self.m, self.lf, "R-2", date.today(), D("100"), D("119"))
        we2 = self.we((self.a, "10", "10"), versand="10")
        services.wareneingang_buchen(we2)
        r2.wareneingaenge.add(we2)
        self.assertEqual(r2.abgleich()["differenz"], D("-10.00"))

    def test_nebenkosten_auf_eigener_rechnung_zaehlen_nur_im_ek(self):
        we = self.we((self.a, "10", "10"), versand="10")
        url = reverse("einkauf:wareneingang_buchen", args=[we.pk])
        self.client.post(url, {"aktion": "kosten", "versandkosten": "10", "nebenkosten_extern": "on"})
        we.refresh_from_db()
        self.assertEqual((we.nebenkosten_extern, we.rechnungswert, we.gesamt_netto), (True, D("100.00"), D("110.00")))
        self.client.post(url, {"versandkosten": "10", "nebenkosten_extern": "on"})
        self.a.refresh_from_db()
        self.assertEqual(self.a.durchschnitts_ek, D("11.0000"))  # eigentlicher EK enthält die Spedition
        r = services.rechnung_erfassen(self.m, self.lf, "R-1", date.today(), D("100"), D("119"), wareneingaenge=[we])
        self.assertTrue(r.abgleich()["ok"])  # Warenrechnung passt ohne Speditionskosten

    def test_maske_speichern_buchen_und_danach_gesperrt(self):
        we = self.we((self.a, "10", "10"))
        url = reverse("einkauf:wareneingang_buchen", args=[we.pk])
        seite = reverse("einkauf:wareneingang", args=[we.pk])
        self.client.post(url, {"aktion": "kosten", "versandkosten": "7,50", "versicherung": "1,50", "verpackung": "1"})
        we.refresh_from_db()
        self.assertEqual((we.nebenkosten, we.gebucht), (D("10.00"), False))
        self.assertContains(self.client.get(seite), "Kalkulations-EK")
        r = self.client.post(url, {"versandkosten": "-1"}, follow=True)
        self.assertContains(r, "dürfen nicht negativ")
        self.assertEqual(self.client.post(url, {"versandkosten": "abc"}).status_code, 302)
        self.assertEqual(self.client.post(url, {"versandkosten": "NaN"}).status_code, 302)
        we.refresh_from_db()
        self.assertFalse(we.gebucht)
        self.client.post(url, {"versandkosten": "10", "versicherung": "0", "verpackung": "0"})
        we.refresh_from_db()
        self.assertTrue(we.gebucht)
        self.assertContains(self.client.get(seite), "Kalk. 11")
        self.client.post(url, {"versandkosten": "99"})  # nach dem Buchen unveränderlich
        we.refresh_from_db()
        self.assertEqual(we.versandkosten, D("10.00"))
        self.assertEqual(self.a.__class__.objects.get(pk=self.a.pk).durchschnitts_ek, D("11.0000"))
        self.assertEqual(bestand(self.m, self.a), D("10"))

    def test_lieferantenrechnung_mit_suchfeld(self):
        seite = self.client.get(reverse("einkauf:rechnungen"))
        self.assertContains(seite, 'list="lieferantenliste"')
        self.assertContains(seite, f'data-pk="{self.lf.pk}"')
        self.assertContains(seite, "L1 – API GmbH (API)")
        url = reverse("einkauf:rechnung_neu")
        daten = {"rechnungsnummer": "R-1", "brutto": "119", "netto": "100", "datum": date.today().isoformat()}
        for lieferant in ("", "abc", "999999"):
            r = self.client.post(url, {**daten, "lieferant": lieferant}, follow=True)
            self.assertContains(r, "Bitte einen Lieferanten aus der Liste wählen.")
        self.assertFalse(Lieferantenrechnung.objects.exists())
        self.client.post(url, {**daten, "lieferant": self.lf.pk})
        self.assertEqual(Lieferantenrechnung.objects.get().lieferant, self.lf)


@override_settings(SECURE_SSL_REDIRECT=False)
class KopfUndKundenmenueTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        Mandant.objects.create(kurzname="ZONE", firmenname="ZONE Handel")
        self.kunde = Kunde.objects.create(nummer="1", name1="Muster GmbH")
        self.client.force_login(get_user_model().objects.create_user("t", password="x" * 14))

    def wahl(self):
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    def test_mandant_ersetzt_wwok_im_kopf(self):
        start = reverse("verkauf:start")
        r = self.client.get(start)
        self.assertContains(r, "<strong>wwok</strong>")
        self.assertContains(r, "Kein Mandant gewählt")
        self.wahl()
        r = self.client.get(start)
        self.assertNotContains(r, "<strong>wwok</strong>")
        self.assertContains(r, 'class="firma"')
        self.assertContains(r, 'title="OKAY Computer – Mandant wechseln">OKAY')

    def test_kundenmenue_mit_neuem_beleg_als_untermenue(self):
        self.wahl()
        r = self.client.get(reverse("verkauf:start"))
        self.assertContains(r, "<span>Kunde</span>")
        self.assertContains(r, "erst Kunde wählen")
        r = self.client.get(reverse("verkauf:kunde", args=[self.kunde.pk]))
        self.assertNotContains(r, "Neuen Beleg anlegen")  # Karte entfällt
        self.assertContains(r, "Neuer Beleg für Muster GmbH")
        self.assertContains(r, reverse("verkauf:beleg_neu", args=[self.kunde.pk]))
        self.assertNotContains(r, 'name="art" value="LS"')
        self.assertContains(r, 'class="leise">Angebot</button>')
        b = Beleg.objects.create(mandant=self.m, art=Belegart.ANGEBOT, kunde=self.kunde)
        self.assertContains(self.client.get(reverse("verkauf:beleg", args=[b.pk])), "Neuer Beleg für Muster GmbH")
        self.kunde.gesperrt = True
        self.kunde.save()
        r = self.client.get(reverse("verkauf:kunde", args=[self.kunde.pk]))
        self.assertContains(r, "Kunde gesperrt")
        self.assertNotContains(r, "Neuer Beleg für")
