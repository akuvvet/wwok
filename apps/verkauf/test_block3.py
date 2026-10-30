from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.kern.models import Mandant
from apps.stammdaten.models import Kunde, Steuersatz

from .models import Beleg, Belegart, Position, Versandart, Wiedervorlage
from .pdf import beleg_pdf


@override_settings(SECURE_SSL_REDIRECT=False)
class Block3Test(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.m2 = Mandant.objects.create(kurzname="ZONE", firmenname="ZONE Handel")
        self.kunde = Kunde.objects.create(nummer="1000", name1="Muster GmbH", ort="Solingen")
        self.ausland = Kunde.objects.create(nummer="2000", name1="Fremd AG", ort="Wien", land="AT")
        self.user = get_user_model().objects.create_user("t", password="x" * 14)
        self.client.force_login(self.user)
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    def beleg(self, art=Belegart.RECHNUNG, kunde=None, mandant=None, preis="100"):
        b = Beleg.objects.create(mandant=mandant or self.m, art=art, kunde=kunde or self.kunde)
        Position.objects.create(beleg=b, bezeichnung="Dienst", einzelpreis=Decimal(preis))
        return b

    # Belegübersicht
    def test_belegliste_filtert_und_trennt_mandanten(self):
        a = self.beleg(Belegart.ANGEBOT)
        r = self.beleg(Belegart.RECHNUNG)
        fremd = self.beleg(Belegart.RECHNUNG, mandant=self.m2)
        Beleg.objects.create(mandant=self.m, art=Belegart.RECHNUNG, kunde=self.kunde)  # ohne Position: nicht gelistet
        url = reverse("verkauf:belege")
        antwort = self.client.get(url)
        self.assertEqual({b.pk for b in antwort.context["seite"].object_list}, {a.pk, r.pk})
        self.assertNotIn(fremd.pk, [b.pk for b in antwort.context["seite"].object_list])
        nur_re = self.client.get(url, {"art": "RE"}).context["seite"].object_list
        self.assertEqual([b.pk for b in nur_re], [r.pk])
        self.assertEqual(self.client.get(url, {"q": "Muster", "status": "abgeschlossen"}).context["anzahl"], 0)
        self.assertEqual(self.client.get(url, {"q": "Muster", "status": "offen"}).context["anzahl"], 2)
        morgen = (date.today() + timedelta(days=1)).isoformat()
        self.assertEqual(self.client.get(url, {"von": morgen}).context["anzahl"], 0)
        self.assertEqual(self.client.get(url, {"von": "kein-datum"}).status_code, 200)

    def test_belegliste_braucht_login(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse("verkauf:belege")).status_code, 302)

    # Versandarten
    def test_versandart_belegt_kosten_vor_und_frachtfrei(self):
        Versandart.objects.create(name="DHL Paket", versandkosten=Decimal("5.90"), verpackung=Decimal("1"),
                                  versicherung=Decimal("0.50"), frachtfrei_ab=Decimal("200"))
        b = self.beleg(preis="100")
        b.versandart_setzen("dhl paket")
        self.assertEqual((b.versandart, b.versandkosten, b.verpackung, b.versicherung),
                         ("DHL Paket", Decimal("5.90"), Decimal("1"), Decimal("0.50")))
        teuer = self.beleg(preis="250")
        teuer.versandart_setzen("DHL Paket")
        self.assertEqual(teuer.versandkosten, Decimal("0"))

    def test_versandart_nur_inland_und_freitext(self):
        Versandart.objects.create(name="Post Inland", versandkosten=Decimal("4"), nur_inland=True)
        b = self.beleg(kunde=self.ausland)
        with self.assertRaises(ValueError):
            b.versandart_setzen("Post Inland")
        b.versandart_setzen("Abholung")  # nicht in der Stammtabelle: bleibt Freitext, Kosten unverändert
        self.assertEqual((b.versandart, b.versandkosten), ("Abholung", Decimal("0")))

    def test_kopf_uebernimmt_standardkosten_nur_bei_geaenderter_versandart(self):
        Versandart.objects.create(name="DHL Paket", versandkosten=Decimal("5.90"))
        b = self.beleg()
        url = reverse("verkauf:beleg_kopf", args=[b.pk])
        self.client.post(url, {"versandart": "DHL Paket", "versandkosten": "0"})
        b.refresh_from_db()
        self.assertEqual(b.versandkosten, Decimal("5.90"))
        self.client.post(url, {"versandart": "DHL Paket", "versandkosten": "7.50"})  # manuell überschrieben, Versandart gleich
        b.refresh_from_db()
        self.assertEqual(b.versandkosten, Decimal("7.50"))

    # Belegkopf
    def test_kopffelder_speichern_und_im_pdf(self):
        b = self.beleg(Belegart.ANGEBOT)
        gueltig = date.today() + timedelta(days=14)
        self.client.post(reverse("verkauf:beleg_kopf", args=[b.pk]), {
            "bezugnahme": "telefonisch", "gueltig_bis": gueltig.isoformat(), "liefertermin": "2030-01-31"})
        b.refresh_from_db()
        self.assertEqual((b.bezugnahme, b.gueltig_bis, b.liefertermin), ("telefonisch", gueltig, date(2030, 1, 31)))
        self.assertTrue(beleg_pdf(b).startswith(b"%PDF"))
        r = self.client.post(reverse("verkauf:beleg_kopf", args=[b.pk]), {"liefertermin": "31.01.2030"}, follow=True)
        self.assertContains(r, "Ungültiges Datum")

    def test_neuer_beleg_hat_bearbeiter(self):
        r = self.client.post(reverse("verkauf:beleg_neu", args=[self.kunde.pk]), {"art": "AN"})
        self.assertEqual(Beleg.objects.get().bearbeiter, self.user)
        self.assertEqual(r.status_code, 302)

    # Wiedervorlage
    def test_wiedervorlage_als_symbol_im_kundenkopf(self):
        url = reverse("verkauf:kunde", args=[self.kunde.pk])
        r = self.client.get(url)
        self.assertContains(r, 'class="menue-mehr wiedervorlage"')
        self.assertContains(r, 'data-tip="Wiedervorlage"')
        self.assertNotContains(r, "<h2>Wiedervorlage</h2>")
        self.client.post(reverse("verkauf:wiedervorlage_neu", args=[self.kunde.pk]), {"faellig": date.today().isoformat(), "text": "Nachfassen"})
        r = self.client.get(url)
        self.assertContains(r, 'data-tip="Wiedervorlage (1 offen)"')
        self.assertContains(r, "Nachfassen")

    def test_wiedervorlage_erscheint_faellig_auf_startseite_und_wird_erledigt(self):
        self.client.post(reverse("verkauf:wiedervorlage_neu", args=[self.kunde.pk]),
                         {"faellig": date.today().isoformat(), "text": "Angebot nachfassen"})
        self.client.post(reverse("verkauf:wiedervorlage_neu", args=[self.kunde.pk]),
                         {"faellig": (date.today() + timedelta(days=5)).isoformat(), "text": "später"})
        self.client.post(reverse("verkauf:wiedervorlage_neu", args=[self.kunde.pk]), {"faellig": "", "text": ""})
        self.assertEqual(Wiedervorlage.objects.count(), 2)
        start = self.client.get(reverse("verkauf:start"))
        self.assertContains(start, "Angebot nachfassen")
        self.assertNotContains(start, "später")
        w = Wiedervorlage.objects.get(text="Angebot nachfassen")
        self.client.post(reverse("verkauf:wiedervorlage_erledigt", args=[w.pk]))
        w.refresh_from_db()
        self.assertTrue(w.erledigt and w.erledigt_am)
        self.assertNotContains(self.client.get(reverse("verkauf:start")), "Angebot nachfassen")

    def test_wiedervorlage_anderer_mandant_nicht_erledigbar(self):
        w = Wiedervorlage.objects.create(mandant=self.m2, kunde=self.kunde, faellig=date.today(), text="fremd")
        self.assertEqual(self.client.post(reverse("verkauf:wiedervorlage_erledigt", args=[w.pk])).status_code, 404)
