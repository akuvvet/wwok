"""Entscheidungen Prüfbericht C, Punkt 9: Zahl- und Stornostatus schon importierter Shop-Bestellungen nachziehen."""
from decimal import Decimal as D

from django.test import TestCase
from django.utils import timezone

from apps.finanzen.models import Zahlung
from apps.kern.models import Mandant
from apps.stammdaten.models import Artikel, Steuersatz
from apps.verkauf.models import Belegart

from . import services
from .models import Shop, ShopBestellung
from .tests import FakeClient, bestellung


class ShopStatusNachziehenTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        st = Steuersatz.objects.create(bezeichnung="19", prozent=D("19"))
        Artikel.objects.create(nummer="WIN11", bezeichnung="Windows", steuersatz=st, vk1=D("100"))
        self.shop = Shop.objects.create(mandant=self.m, name="Webshop", url="https://shop.example.test", client_id="abc")

    def abrufen(self, b):
        return services.bestellungen_abrufen(self.shop, FakeClient(self.shop, [b]))

    def test_spaeter_bezahlt_wird_genau_einmal_gebucht(self):
        self.abrufen(bestellung(bezahlt=False))
        sb = ShopBestellung.objects.get()
        self.assertFalse(Zahlung.objects.exists())
        self.assertFalse(sb.zahlung_gebucht)
        ergebnis = self.abrufen(bestellung(bezahlt=True))
        self.assertEqual((ergebnis["neu"], ergebnis["bekannt"]), (0, 1))
        self.abrufen(bestellung(bezahlt=True))  # wiederholter Abruf
        self.abrufen(bestellung(bezahlt=True))
        z = Zahlung.objects.get()
        self.assertEqual((z.betrag, z.beleg), (D("237.80"), sb.beleg))
        sb.refresh_from_db()
        self.assertTrue(sb.zahlung_gebucht)

    def test_zahlung_wird_auch_ueber_die_statusabfrage_gefunden(self):
        # Bestellung ist älter als der Abrufzeitraum und fehlt in der Zeitabfrage, kommt aber über ihre ID
        services.bestellung_importieren(self.shop, bestellung(bezahlt=False))

        class NurIds(FakeClient):
            def bestellungen(self, seit=None, seite=1, limit=50, ids=None):
                if ids:
                    return {"data": [bestellung(bezahlt=True)]}
                return {"data": []}
        services.bestellungen_abrufen(self.shop, NurIds(self.shop))
        self.assertEqual(Zahlung.objects.count(), 1)

    def test_erstimport_bezahlt_bucht_einmal_und_wird_nicht_verdoppelt(self):
        self.abrufen(bestellung(bezahlt=True))
        self.abrufen(bestellung(bezahlt=True))
        self.assertEqual(Zahlung.objects.count(), 1)
        self.assertTrue(ShopBestellung.objects.get().zahlung_gebucht)

    def test_im_shop_storniert_nur_hinweis_nichts_storniert(self):
        self.abrufen(bestellung(bezahlt=True))
        b = bestellung(bezahlt=True)
        b["stateMachineState"] = {"technicalName": "cancelled"}
        self.abrufen(b)
        sb = ShopBestellung.objects.get()
        self.assertIsNotNone(sb.im_shop_storniert_am)
        self.assertEqual(Zahlung.objects.count(), 1)  # keine Gegenbuchung
        sb.beleg.refresh_from_db()
        self.assertEqual(sb.beleg.art, Belegart.AUFTRAG)
        erstes = sb.im_shop_storniert_am
        self.abrufen(b)
        sb.refresh_from_db()
        self.assertEqual(sb.im_shop_storniert_am, erstes)  # Zeitpunkt bleibt

    def test_storno_beim_erstimport_wird_vermerkt(self):
        b = bestellung(bezahlt=False)
        b["stateMachineState"] = {"technicalName": "cancelled"}
        self.abrufen(b)
        self.assertIsNotNone(ShopBestellung.objects.get().im_shop_storniert_am)

    def test_bezahlt_bei_nicht_zahlbarem_beleg_wird_nicht_gebucht_sondern_vermerkt(self):
        self.abrufen(bestellung(bezahlt=False))
        sb = ShopBestellung.objects.get()
        sb.beleg.vorlaeufig = True  # Zahlungen gehen nur auf abgeschlossene Belege
        sb.beleg.save()
        self.abrufen(bestellung(bezahlt=True))
        sb.refresh_from_db()
        self.assertFalse(sb.zahlung_gebucht)
        self.assertFalse(Zahlung.objects.exists())
        self.assertIn("nicht gebucht", sb.hinweis)

    def test_uebersicht_zeigt_storno_hinweis(self):
        from django.contrib.auth import get_user_model
        self.abrufen(bestellung())
        ShopBestellung.objects.update(im_shop_storniert_am=timezone.now())
        u = get_user_model().objects.create_user("t", password="x", is_superuser=True, is_staff=True)
        self.client.force_login(u)
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()
        antwort = self.client.get("/shop/", secure=True)
        self.assertEqual(antwort.status_code, 200)
        self.assertContains(antwort, "im Shop storniert am")

    def test_schon_von_hand_gebuchte_zahlung_wird_nicht_verdoppelt(self):
        from apps.finanzen.services import zahlung_buchen
        from apps.verkauf.models import Beleg, Position
        self.abrufen(bestellung(bezahlt=False))
        sb = ShopBestellung.objects.get()
        rechnung = Beleg.objects.create(mandant=self.m, art=Belegart.RECHNUNG, kunde=sb.beleg.kunde, vorgaenger=sb.beleg)
        Position.objects.create(beleg=rechnung, typ="L", bezeichnung="Windows", menge=1, einzelpreis=D("100"))
        rechnung.abschliessen()
        zahlung_buchen(rechnung, D("119.00"), timezone.localdate(), "ue")  # Geldeingang von Hand auf die Rechnung gebucht
        self.abrufen(bestellung(bezahlt=True))
        self.assertEqual(Zahlung.objects.count(), 1)
        sb.refresh_from_db()
        self.assertFalse(sb.zahlung_gebucht)
        self.assertIn("schon eine Zahlung gebucht", sb.hinweis)
