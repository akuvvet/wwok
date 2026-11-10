"""Inventur/Zählliste: Artikelsuche und Kategoriefilter (mit Unterkategorien); Buchen auch für gerade nicht angezeigte Artikel."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.kern.models import Mandant
from apps.stammdaten.models import Artikel, Kategorie, Steuersatz

from .models import Bewegungsart, bestand
from .services import buche_manuell


@override_settings(SECURE_SSL_REDIRECT=False)
class InventurFilterTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.m2 = Mandant.objects.create(kurzname="ZONE", firmenname="ZONE Handel")
        st = Steuersatz.objects.create(bezeichnung="19", prozent=Decimal("19"))
        self.haupt = Kategorie.objects.create(name="Hardware")
        self.unter = Kategorie.objects.create(name="Mäuse", eltern=self.haupt)
        self.maus = Artikel.objects.create(nummer="M100", bezeichnung="Funkmaus schwarz", ean="4001234567890",
                                           steuersatz=st, kategorie=self.unter)
        self.kabel = Artikel.objects.create(nummer="K200", bezeichnung="HDMI Kabel 2m", steuersatz=st, kategorie=self.haupt)
        self.alt = Artikel.objects.create(nummer="X300", bezeichnung="Alte Tastatur", steuersatz=st, nicht_aktiv=True)
        self.leer = Artikel.objects.create(nummer="L400", bezeichnung="Maus ohne Bestand", steuersatz=st)
        for a in (self.maus, self.kabel, self.alt):
            buche_manuell(self.m, a, Bewegungsart.WARENEINGANG, Decimal("5"))
        self.zone = Artikel.objects.create(nummer="Z500", bezeichnung="Maus bei ZONE", steuersatz=st)
        buche_manuell(self.m2, self.zone, Bewegungsart.WARENEINGANG, Decimal("3"))
        self.client.force_login(get_user_model().objects.create_superuser("chef", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    def nummern(self, **filter_):
        r = self.client.get(reverse("lager:inventur_liste"), filter_)
        self.assertEqual(r.status_code, 200)
        return [z["artikel"].nummer for z in r.context["zeilen"]]

    def test_ohne_filter_nur_aktive_artikel_mit_bestand_des_mandanten(self):
        self.assertEqual(sorted(self.nummern()), ["K200", "M100"])  # nicht aktiv, ohne Bestand und fremder Mandant fehlen

    def test_suche_mehrere_woerter_in_beliebiger_reihenfolge(self):
        self.assertEqual(self.nummern(q="schwarz funk"), ["M100"])
        self.assertEqual(self.nummern(q="k200"), ["K200"])
        self.assertEqual(self.nummern(q="4001234567890"), ["M100"])  # EAN (Scanner)
        self.assertEqual(self.nummern(q="maus"), ["M100"])  # nicht: ohne Bestand, anderer Mandant

    def test_haken_nicht_aktiv_zeigt_nur_nicht_aktive(self):
        self.assertEqual(self.nummern(q="tastatur"), [])
        self.assertEqual(self.nummern(nicht_aktiv="1"), ["X300"])
        self.assertEqual(self.nummern(nicht_aktiv="1", q="tastatur"), ["X300"])
        self.assertEqual(self.nummern(nicht_aktiv="1", q="maus"), [])
        r = self.client.get(reverse("lager:inventur_liste"), {"nicht_aktiv": "1"})
        self.assertContains(r, 'name="nicht_aktiv" value="1" checked')
        self.assertNotContains(self.client.get(reverse("lager:inventur_liste")), 'value="1" checked')

    def test_nicht_aktive_lassen_sich_buchen(self):
        self.client.post(reverse("lager:inventur_liste") + "?nicht_aktiv=1", {f"gezaehlt_{self.alt.pk}": "0"})
        self.assertEqual(bestand(self.m, self.alt), Decimal("0"))

    def test_kategorie_mit_unterkategorien_und_kombiniert_mit_suche(self):
        self.assertEqual(sorted(self.nummern(kategorie=self.haupt.pk)), ["K200", "M100"])
        self.assertEqual(self.nummern(kategorie=self.unter.pk), ["M100"])
        self.assertEqual(self.nummern(kategorie=self.haupt.pk, q="hdmi"), ["K200"])
        self.assertEqual(sorted(self.nummern(kategorie="abc")), ["K200", "M100"])

    def test_maske_hat_suchfeld_livefilter_und_unterkategorien(self):
        r = self.client.get(reverse("lager:inventur_liste"), {"q": "hdmi"})
        self.assertContains(r, 'data-livefilter')
        self.assertContains(r, 'name="q" value="hdmi"')
        self.assertContains(r, "data-ergebnis")
        self.assertContains(r, "– Mäuse</option>")
        self.assertContains(r, 'name="gezaehlt_%d"' % self.kabel.pk)
        self.assertNotContains(r, 'name="gezaehlt_%d"' % self.maus.pk)

    def test_keine_treffer_mit_filter_zuruecksetzen(self):
        r = self.client.get(reverse("lager:inventur_liste"), {"q": "gibtsnicht"})
        self.assertContains(r, "Filter zurücksetzen")
        self.assertContains(r, "Differenzen buchen")  # Knopf bleibt: erfasste Werte aus einer früheren Suche lassen sich buchen

    def test_buchen_auch_fuer_artikel_ausserhalb_der_aktuellen_suche(self):
        url = reverse("lager:inventur_liste") + "?q=hdmi"
        r = self.client.post(url, {f"gezaehlt_{self.kabel.pk}": "4", f"gezaehlt_{self.maus.pk}": "7"})
        self.assertRedirects(r, url, fetch_redirect_response=False)  # Filter bleibt erhalten
        self.assertEqual((bestand(self.m, self.kabel), bestand(self.m, self.maus)), (Decimal("4"), Decimal("7")))

    def test_kategoriefilter_schraenkt_buchung_nicht_ein(self):
        url = reverse("lager:inventur_liste") + f"?kategorie={self.unter.pk}"
        self.client.post(url, {f"gezaehlt_{self.kabel.pk}": "2"})  # Kabel liegt nicht in „Mäuse“
        self.assertEqual(bestand(self.m, self.kabel), Decimal("2"))

    def test_ungueltiger_wert_meldet_fehler_gueltige_werden_gebucht(self):
        r = self.client.post(reverse("lager:inventur_liste"),
                             {f"gezaehlt_{self.kabel.pk}": "abc", f"gezaehlt_{self.maus.pk}": "3"}, follow=True)
        self.assertContains(r, "K200")
        self.assertEqual((bestand(self.m, self.kabel), bestand(self.m, self.maus)), (Decimal("5"), Decimal("3")))

    def test_doppelt_gesendet_bucht_nur_einmal(self):
        daten = {f"gezaehlt_{self.maus.pk}": "4"}
        self.client.post(reverse("lager:inventur_liste"), daten)
        self.client.post(reverse("lager:inventur_liste"), daten)
        self.assertEqual(bestand(self.m, self.maus), Decimal("4"))

    def test_maske_merkt_werte_und_sperrt_doppelklick(self):
        r = self.client.get(reverse("lager:inventur_liste"))
        self.assertContains(r, "sessionStorage")
        self.assertContains(r, "form.dataset.gesendet")

    def test_artikel_und_bestand_haken_nicht_aktiv(self):
        url = reverse("lager:uebersicht")
        nummern = lambda **f: [z["artikel"].nummer for z in self.client.get(url, f).context["zeilen"]]
        self.assertNotIn("X300", nummern())  # Standard: nur aktive
        self.assertEqual(nummern(nicht_aktiv="1"), ["X300"])
        self.assertEqual(nummern(nicht_aktiv="1", q="tastatur"), ["X300"])
        self.assertEqual(nummern(nicht_aktiv="1", q="maus"), [])
        self.assertEqual(nummern(nicht_aktiv="1", bestand="1"), ["X300"])
        self.assertContains(self.client.get(url), 'name="nicht_aktiv" value="1" >')
        self.assertContains(self.client.get(url, {"nicht_aktiv": "1"}), 'name="nicht_aktiv" value="1" checked')

    def test_buchen_ignoriert_artikel_ohne_bestand_und_fremden_mandanten(self):
        self.client.post(reverse("lager:inventur_liste"),
                         {f"gezaehlt_{self.leer.pk}": "2", f"gezaehlt_{self.zone.pk}": "9"})
        self.assertEqual((bestand(self.m, self.leer), bestand(self.m, self.zone), bestand(self.m2, self.zone)),
                         (Decimal("0"), Decimal("0"), Decimal("3")))
