from datetime import date, timedelta
from decimal import Decimal as D

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.kern.models import Mandant
from apps.stammdaten.models import Kunde, Zahlungsbedingung
from apps.verkauf.models import Beleg, Belegart, Position

from . import services
from .models import Gutschriftverrechnung


@override_settings(SECURE_SSL_REDIRECT=False)
class GutschriftVerrechnungTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.kunde = Kunde.objects.create(nummer="1", name1="Kunde GmbH")
        self.zb = Zahlungsbedingung.objects.create(bezeichnung="10 Tage", tage_netto=10, tage_mahnung=11)
        self.chef = get_user_model().objects.create_superuser("chef", password="x" * 14)
        self.client.force_login(self.chef)
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()
        self.r = self.rechnung()
        self.g = self.gutschrift(self.r)

    def rechnung(self, kunde=None, mandant=None, tage_alt=0):
        b = Beleg.objects.create(mandant=mandant or self.m, art=Belegart.RECHNUNG, kunde=kunde or self.kunde,
                                 zahlungsbedingung=self.zb, belegdatum=date.today() - timedelta(days=tage_alt))
        Position.objects.create(beleg=b, bezeichnung="Dienst", einzelpreis=D("100"))
        b.abschliessen()
        b.refresh_from_db()
        return b

    def gutschrift(self, rechnung):
        g = rechnung.gutschrift_erzeugen()
        g.abschliessen()
        g.refresh_from_db()
        return g

    def test_gutschrift_mindert_rechnung_nicht_und_steht_als_minusposten(self):
        self.assertEqual(services.offen(self.r), D("119.00"))
        self.assertEqual(services.gutschrift_rest(self.g), D("119.00"))
        self.assertEqual(services.offene_gutschriften(self.m)[0]["offen"], D("-119.00"))
        self.assertEqual(services.kunde_offen(self.m, self.kunde), D("0.00"))  # Saldo für das Kreditlimit
        r = self.client.get(reverse("finanzen:offene_posten"))
        self.assertContains(r, self.g.nummer)
        self.assertContains(r, "-119,00")

    def test_rechnung_laeuft_normal_im_mahnvorschlag(self):
        alt = self.rechnung(tage_alt=40)
        self.gutschrift(alt)
        self.assertIn(alt, [z["beleg"] for z in services.mahnvorschlag(self.m)])

    def test_teilbetrag_und_rest(self):
        services.gutschrift_verrechnen(self.g, self.r, D("19.00"))
        self.assertEqual(services.offen(self.r), D("100.00"))
        self.assertEqual(services.gutschrift_rest(self.g), D("100.00"))
        services.gutschrift_verrechnen(self.g, self.r, D("100.00"))
        self.assertEqual(services.offen(self.r), D("0.00"))
        self.assertEqual(services.offene_gutschriften(self.m), [])
        with self.assertRaisesRegex(ValueError, "nur noch"):
            services.gutschrift_verrechnen(self.g, self.r, D("0.01"))

    def test_bezahlte_rechnung_wird_ueberzahlt_und_rueckzahlung_gleicht_aus(self):
        services.zahlung_buchen(self.r, D("119.00"), date.today(), "ue")
        services.gutschrift_verrechnen(self.g, self.r, D("119.00"))
        self.assertEqual(services.offen(self.r), D("-119.00"))
        services.zahlung_buchen(self.r, D("-119.00"), date.today(), "ue", "Rückzahlung")
        self.assertEqual(services.offen(self.r), D("0.00"))

    def test_guthaben_auf_andere_rechnung_desselben_kunden(self):
        zweite = self.rechnung()
        services.gutschrift_verrechnen(self.g, zweite, D("50.00"))
        self.assertEqual(services.offen(zweite), D("69.00"))
        self.assertEqual(services.offen(self.r), D("119.00"))

    def test_grenzen(self):
        anderer = Kunde.objects.create(nummer="2", name1="Andere GmbH")
        with self.assertRaisesRegex(ValueError, "selben Kunden"):
            services.gutschrift_verrechnen(self.g, self.rechnung(kunde=anderer), D("1"))
        zone = Mandant.objects.create(kurzname="ZONE", firmenname="ZONE Handel")
        with self.assertRaisesRegex(ValueError, "selben Kunden"):
            services.gutschrift_verrechnen(self.g, self.rechnung(mandant=zone), D("1"))
        for betrag in (D("0"), D("-1")):
            with self.assertRaisesRegex(ValueError, "größer"):
                services.gutschrift_verrechnen(self.g, self.r, betrag)
        entwurf = self.rechnung().gutschrift_erzeugen()
        with self.assertRaisesRegex(ValueError, "abgeschlossene Gutschriften"):
            services.gutschrift_verrechnen(entwurf, self.r, D("1"))
        with self.assertRaisesRegex(ValueError, "abgeschlossene Gutschriften"):
            services.gutschrift_verrechnen(self.r, self.r, D("1"))
        offen = Beleg.objects.create(mandant=self.m, art=Belegart.RECHNUNG, kunde=self.kunde)
        with self.assertRaisesRegex(ValueError, "abgeschlossenen Rechnungen"):
            services.gutschrift_verrechnen(self.g, offen, D("1"))

    def test_verrechnung_unveraenderlich_und_aufhebbar(self):
        v = services.gutschrift_verrechnen(self.g, self.r, D("119.00"))
        with self.assertRaises(PermissionError):
            v.save()
        with self.assertRaises(PermissionError):
            v.delete()
        with self.assertRaisesRegex(ValueError, "Grund"):
            services.verrechnung_aufheben(v, "  ")
        gegen = services.verrechnung_aufheben(v, "falsche Rechnung")
        self.assertEqual(services.offen(self.r), D("119.00"))
        self.assertEqual(services.gutschrift_rest(self.g), D("119.00"))
        with self.assertRaisesRegex(ValueError, "bereits"):
            services.verrechnung_aufheben(v, "nochmal")
        with self.assertRaisesRegex(ValueError, "erneut"):
            services.verrechnung_aufheben(gegen, "nochmal")

    def test_doppelklick_bucht_nur_einmal(self):
        daten = {"gutschrift": self.g.pk, "betrag": "19,00", "offen_bisher": "119,00"}
        url = reverse("finanzen:gutschrift_verrechnen", args=[self.r.pk])
        self.client.post(url, daten)
        r = self.client.post(url, daten, follow=True)
        self.assertContains(r, "inzwischen geändert")
        self.assertEqual(services.offen(self.r), D("100.00"))
        self.assertEqual(Gutschriftverrechnung.objects.count(), 1)

    def test_ueberzahlung_ist_nicht_ueberfaellig_und_nicht_einziehbar(self):
        alt = self.rechnung(tage_alt=40)
        services.zahlung_buchen(alt, D("119.00"), date.today(), "ue")
        services.gutschrift_verrechnen(self.gutschrift(alt), alt, D("119.00"))
        zeile = next(z for z in services.offene_rechnungen(self.m) if z["beleg"] == alt)
        self.assertEqual((zeile["offen"], zeile["ueberfaellig"]), (D("-119.00"), 0))
        self.assertNotContains(self.client.get(reverse("finanzen:lastschrift")), alt.nummer)

    def test_gutschrift_ohne_rechnung_laesst_sich_verrechnen(self):
        frei = Beleg.objects.create(mandant=self.m, art=Belegart.GUTSCHRIFT, kunde=self.kunde)
        Position.objects.create(beleg=frei, bezeichnung="Dienst", einzelpreis=D("10"))
        frei.abschliessen()
        frei.refresh_from_db()
        self.assertContains(self.client.get(reverse("finanzen:beleg", args=[self.r.pk])), "(ohne Rechnung)")
        self.assertContains(self.client.get(reverse("finanzen:offene_posten")), frei.nummer)
        services.gutschrift_verrechnen(frei, self.r, D("11.90"))
        self.assertEqual(services.offen(self.r), D("107.10"))

    def test_seite_verrechnen_und_aufheben(self):
        seite = reverse("finanzen:beleg", args=[self.r.pk])
        self.assertContains(self.client.get(seite), 'name="offen_bisher" value="119,00"')
        r = self.client.post(reverse("finanzen:gutschrift_verrechnen", args=[self.r.pk]),
                             {"gutschrift": self.g.pk, "betrag": "19,00", "offen_bisher": "119,00"}, follow=True)
        self.assertContains(r, "verrechnet.")
        self.assertEqual(services.offen(self.r), D("100.00"))
        r = self.client.post(reverse("finanzen:gutschrift_verrechnen", args=[self.r.pk]),
                             {"gutschrift": self.g.pk, "betrag": "abc", "offen_bisher": "100,00"}, follow=True)
        self.assertContains(r, "Ungültiger Betrag")
        self.assertEqual(self.client.post(reverse("finanzen:gutschrift_verrechnen", args=[self.r.pk]),
                                          {"gutschrift": "x", "betrag": "1", "offen_bisher": "100,00"}).status_code, 404)
        v = Gutschriftverrechnung.objects.get()
        self.client.post(reverse("finanzen:verrechnung_aufheben", args=[v.pk]), {"grund": "Irrtum"})
        self.assertEqual(services.offen(self.r), D("119.00"))
        self.assertContains(self.client.get(seite), "aufgehoben")

    def test_vorschlag_begrenzt_auf_offenen_betrag(self):
        services.zahlung_buchen(self.r, D("100.00"), date.today(), "ue")
        self.assertEqual(self.client.get(reverse("finanzen:beleg", args=[self.r.pk])).context["gutschriften"][0]["vorschlag"], D("19.00"))
        services.zahlung_buchen(self.r, D("19.00"), date.today(), "ue")
        self.assertEqual(self.client.get(reverse("finanzen:beleg", args=[self.r.pk])).context["gutschriften"][0]["vorschlag"], D("119.00"))

    def test_fremder_mandant_kommt_nicht_durch(self):
        zone = Mandant.objects.create(kurzname="ZONE", firmenname="ZONE Handel")
        fremd = self.rechnung(mandant=zone)
        fremde_g = self.gutschrift(fremd)
        self.assertEqual(self.client.post(reverse("finanzen:gutschrift_verrechnen", args=[fremd.pk]),
                                          {"gutschrift": fremde_g.pk, "betrag": "1", "offen_bisher": "0"}).status_code, 404)
        self.assertEqual(self.client.post(reverse("finanzen:gutschrift_verrechnen", args=[self.r.pk]),
                                          {"gutschrift": fremde_g.pk, "betrag": "1", "offen_bisher": "0"}).status_code, 404)
        v = services.gutschrift_verrechnen(fremde_g, fremd, D("1"))
        self.assertEqual(self.client.post(reverse("finanzen:verrechnung_aufheben", args=[v.pk]), {"grund": "x"}).status_code, 404)

    def test_aufheben_nur_fuer_admin(self):
        v = services.gutschrift_verrechnen(self.g, self.r, D("1"))
        self.client.force_login(get_user_model().objects.create_user("t", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()
        self.client.post(reverse("finanzen:verrechnung_aufheben", args=[v.pk]), {"grund": "x"})
        self.assertEqual(Gutschriftverrechnung.objects.count(), 1)

    def test_kontoauszug_zaehlt_verrechnung_nicht_doppelt(self):
        _, saldo = services.kontoauszug(self.m, self.kunde)
        services.gutschrift_verrechnen(self.g, self.r, D("119.00"))
        self.assertEqual(services.kontoauszug(self.m, self.kunde)[1], saldo)
        self.assertEqual(saldo, D("0.00"))
