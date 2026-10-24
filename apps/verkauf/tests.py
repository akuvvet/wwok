from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.kern.models import Mandant
from apps.stammdaten.models import Artikel, Kunde, Steuersatz

from .models import Beleg, BelegGesperrt, Belegart, Position


@override_settings(SECURE_SSL_REDIRECT=False)
class VerkaufTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.m2 = Mandant.objects.create(kurzname="ZONE", firmenname="ZONE Handel")
        self.kunde = Kunde.objects.create(nummer="1000", name1="Muster GmbH", ort="Solingen", preisstufe=2)
        st = Steuersatz.objects.create(bezeichnung="19", prozent=Decimal("19"))
        self.art = Artikel.objects.create(nummer="A1", bezeichnung="Maus", steuersatz=st,
                                          vk1=Decimal("10"), vk2=Decimal("8"))

    def beleg(self, art=Belegart.RECHNUNG, mandant=None):
        return Beleg.objects.create(mandant=mandant or self.m, art=art, kunde=self.kunde)

    def test_summen_und_steuer(self):
        b = self.beleg()
        Position.objects.create(beleg=b, artikel=self.art, bezeichnung="Maus", menge=Decimal("3"),
                                einzelpreis=Decimal("8"), steuer_prozent=Decimal("19"))
        b.versandkosten = Decimal("5")
        b.save()
        b.berechne()
        self.assertEqual(b.summe_netto, Decimal("29.00"))
        self.assertEqual(b.summe_steuer, Decimal("5.51"))
        self.assertEqual(b.summe_brutto, Decimal("34.51"))

    def test_nummernkreis_pro_mandant_lueckenlos(self):
        a = self.beleg(Belegart.ANGEBOT)
        b = self.beleg(Belegart.ANGEBOT)
        c = self.beleg(Belegart.ANGEBOT, self.m2)
        self.assertTrue(a.nummer.endswith("00001") and b.nummer.endswith("00002") and c.nummer.endswith("00001"))

    def test_rechnung_wird_beim_abschluss_nummeriert_und_gesperrt(self):
        b = self.beleg()
        self.assertIsNone(b.nummer)
        with self.assertRaises(ValueError):
            b.abschliessen()
        Position.objects.create(beleg=b, bezeichnung="Dienst", einzelpreis=Decimal("100"))
        b.abschliessen()
        b.refresh_from_db()
        self.assertTrue(b.nummer.startswith("RE") and b.gesperrt)
        with self.assertRaises(BelegGesperrt):
            Position.objects.create(beleg=b, bezeichnung="x", einzelpreis=Decimal("1"))
        b.versandart = "DHL"
        b.save()  # Versandart bleibt änderbar
        b.versandkosten = Decimal("9")
        with self.assertRaises(BelegGesperrt):
            b.save()

    def test_belegfluss(self):
        a = self.beleg(Belegart.ANGEBOT)
        Position.objects.create(beleg=a, artikel=self.art, bezeichnung="Maus", einzelpreis=Decimal("8"))
        auftrag = a.folgebeleg_erzeugen()
        self.assertEqual((auftrag.art, auftrag.vorgaenger, auftrag.positionen.count()), ("AB", a, 1))
        self.assertEqual(auftrag.summe_brutto, Decimal("9.52"))

    def test_masken_und_mandantentrennung(self):
        user = get_user_model().objects.create_user("t", password="x" * 14)
        self.client.force_login(user)
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()
        r = self.client.post(reverse("verkauf:beleg_neu", args=[self.kunde.pk]), {"art": "AB"})
        b = Beleg.objects.get(art="AB")
        self.assertRedirects(r, reverse("verkauf:beleg", args=[b.pk]))
        self.client.post(reverse("verkauf:position_neu", args=[b.pk]),
                         {"artikel": "A1", "menge": "2", "preis": "", "typ": "A"})
        b.refresh_from_db()
        self.assertEqual(b.summe_netto, Decimal("16.00"))  # VK2 des Kunden
        fremd = self.beleg(Belegart.ANGEBOT, self.m2)
        self.assertEqual(self.client.get(reverse("verkauf:beleg", args=[fremd.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("verkauf:api_artikel") + "?q=Ma&kunde=%d" % self.kunde.pk)
                         .json()["treffer"][0]["preis"], "8.00")

    def test_artikelsuche_mehrere_woerter_beliebige_reihenfolge(self):
        user = get_user_model().objects.create_user("s", password="x" * 14)
        self.client.force_login(user)
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()
        Artikel.objects.create(nummer="A2", bezeichnung="Netzteil 500W", herstellernummer="AZZA-500",
                               steuersatz=self.art.steuersatz)
        url = reverse("verkauf:api_artikel")
        for q in ("netz azza", "azza netz", "NETZ 500"):
            self.assertEqual([t["nummer"] for t in self.client.get(url, {"q": q}).json()["treffer"]], ["A2"], q)
        self.assertEqual(self.client.get(url, {"q": "netz maus"}).json()["treffer"], [])

    def test_login_pflicht(self):
        self.assertEqual(self.client.get(reverse("verkauf:start")).status_code, 302)
