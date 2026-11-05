"""Issue 44: Listen stellen keine Abfragen je Zeile mehr. Geprüft wird, dass die Abfrageanzahl bei mehr Zeilen gleich bleibt."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.einkauf.models import BestellPosition
from apps.einkauf.services import bestellung_anlegen
from apps.esd.models import ESDKey
from apps.finanzen.services import kasse_buchen, kassenstand
from apps.kern.models import Mandant
from apps.shop.models import Shop, SyncProtokoll
from apps.stammdaten.models import Artikel, Kunde, Lieferant, Steuersatz
from apps.verkauf.models import Beleg, Belegart, Position

from .models import Bewegungsart
from .services import buche_manuell


@override_settings(SECURE_SSL_REDIRECT=False)
class AbfragenTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        cls.st = Steuersatz.objects.create(bezeichnung="19", prozent=Decimal("19"))
        cls.kunde = Kunde.objects.create(nummer="1", name1="Kunde")
        cls.user = get_user_model().objects.create_superuser("admin44", "a@example.com", "pw")

    def setUp(self):
        self.client.force_login(self.user)
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()
        self.zaehler = 0

    def anzahl(self, url, **parameter):
        with CaptureQueriesContext(connection) as ctx:
            antwort = self.client.get(url, parameter)
        self.assertEqual(antwort.status_code, 200)
        return len(ctx)

    def artikel(self, mit_bestand=True):
        self.zaehler += 1
        a = Artikel.objects.create(nummer=f"X{self.zaehler}", bezeichnung=f"Artikel {self.zaehler}", steuersatz=self.st,
                                   vk1=Decimal("10"), durchschnitts_ek=Decimal("5"))
        if mit_bestand:
            buche_manuell(self.m, a, Bewegungsart.WARENEINGANG, Decimal("10"))
        return a

    def beleg(self, art, artikel, **kw):
        b = Beleg.objects.create(mandant=self.m, art=art, kunde=self.kunde, **kw)
        Position.objects.create(beleg=b, artikel=artikel, bezeichnung=artikel.bezeichnung, menge=Decimal("2"), einzelpreis=Decimal("10"))
        return b

    def test_lager_uebersicht(self):
        def neu(n):
            for _ in range(n):
                self.beleg(Belegart.AUFTRAG, self.artikel())
        neu(2)
        wenig = self.anzahl(reverse("lager:uebersicht"))
        neu(4)
        self.assertEqual(self.anzahl(reverse("lager:uebersicht")), wenig)

    def test_lager_reservierung_unveraendert(self):
        a = self.artikel()
        self.beleg(Belegart.AUFTRAG, a)
        antwort = self.client.get(reverse("lager:uebersicht"))
        zeile = [z for z in antwort.context["zeilen"] if z["artikel"].pk == a.pk][0]
        self.assertEqual((zeile["bestand"], zeile["reserviert"], zeile["verfuegbar"]), (Decimal("10"), Decimal("2"), Decimal("8")))

    def test_bestellliste(self):
        lf = Lieferant.objects.create(nummer="L1", name1="Lieferant")

        def neu(n):
            for _ in range(n):
                b = bestellung_anlegen(self.m, lf)
                BestellPosition.objects.create(bestellung=b, bezeichnung="Teil", menge=Decimal("1"), ek=Decimal("3"))
                BestellPosition.objects.create(bestellung=b, bezeichnung="Teil 2", menge=Decimal("2"), ek=Decimal("3"))
        neu(2)
        wenig = self.anzahl(reverse("einkauf:bestellungen"))
        neu(4)
        self.assertEqual(self.anzahl(reverse("einkauf:bestellungen")), wenig)

    def test_kundenansicht(self):
        def neu(n):
            for _ in range(n):
                self.beleg(Belegart.LIEFERSCHEIN, self.artikel(), abgeschlossen=True)
                self.beleg(Belegart.RECHNUNG, self.artikel(), abgeschlossen=True, nummer=f"R{self.zaehler}")
        neu(2)
        url = reverse("verkauf:kunde", args=[self.kunde.pk])
        wenig = self.anzahl(url)
        neu(4)
        self.assertEqual(self.anzahl(url), wenig)

    def test_kundenansicht_sammelrechnung_und_offen(self):
        ls1 = self.beleg(Belegart.LIEFERSCHEIN, self.artikel(), abgeschlossen=True)
        ls2 = self.beleg(Belegart.LIEFERSCHEIN, self.artikel(), abgeschlossen=True)
        antwort = self.client.get(reverse("verkauf:kunde", args=[self.kunde.pk]))
        self.assertTrue(antwort.context["sammelrechnung"])
        self.assertEqual({z["beleg"].pk for z in antwort.context["zeilen"] if z["abrechenbar"]}, {ls1.pk, ls2.pk})

    def test_verwaltung_artikel(self):
        for _ in range(2):
            self.artikel()
        url = reverse("admin:stammdaten_artikel_changelist")
        wenig = self.anzahl(url)
        for _ in range(4):
            self.artikel()
        self.assertEqual(self.anzahl(url), wenig)

    def test_verwaltung_artikel_ampel_wie_einzelberechnung(self):
        from apps.stammdaten import radar
        a = self.artikel()
        a.vk1 = Decimal("1")  # unter EK: Verlust
        a.save()
        self.assertEqual(radar.ampeln([a], self.m), {a.pk: radar.ampel(a, self.m)})
        self.assertEqual(radar.ampel(a, self.m), "rot")
        self.assertContains(self.client.get(reverse("admin:stammdaten_artikel_changelist")), "🔴")

    def test_esd_uebersicht(self):
        def neu(n):
            for _ in range(n):
                a = self.artikel(mit_bestand=False)
                a.esd_key = True
                a.save()
                ESDKey.objects.create(mandant=self.m, artikel=a, key=f"K-{a.pk}-1")
                ESDKey.objects.create(mandant=self.m, artikel=a, key=f"K-{a.pk}-2", max_vergaben=3)
        neu(2)
        wenig = self.anzahl(reverse("esd:uebersicht"))
        neu(4)
        self.assertEqual(self.anzahl(reverse("esd:uebersicht")), wenig)

    def test_esd_zahlen_unveraendert(self):
        from apps.esd import services
        a = self.artikel(mit_bestand=False)
        a.esd_key = True
        a.save()
        ESDKey.objects.create(mandant=self.m, artikel=a, key="K1")
        ESDKey.objects.create(mandant=self.m, artikel=a, key="K2", max_vergaben=3, vergeben=1)
        ESDKey.objects.create(mandant=self.m, artikel=a, key="K3", status="gesperrt")
        antwort = self.client.get(reverse("esd:uebersicht"))
        zeile = [z for z in antwort.context["zeilen"] if z["artikel"].pk == a.pk][0]
        erwartet = services.bestand(self.m, a)
        self.assertEqual({k: zeile[k] for k in erwartet}, erwartet)
        frei = self.client.get(reverse("esd:artikel", args=[a.pk]), {"status": "frei"})
        self.assertEqual({k.key for k in frei.context["seite"].object_list}, {"K1", "K2"})

    def test_kassenstand(self):
        kasse_buchen(self.m, "E", Decimal("100.50"), "Einnahme")
        kasse_buchen(self.m, "A", Decimal("30.25"), "Ausgabe")
        self.assertEqual(kassenstand(self.m), Decimal("70.25"))
        self.assertEqual(kassenstand(Mandant.objects.create(kurzname="Z", firmenname="Z")), Decimal("0"))

    def test_shop_uebersicht_zeigt_letzte_drei_protokolle(self):
        shops = [Shop.objects.create(mandant=self.m, name=f"Shop {i}", url="https://s.example", client_id="x") for i in range(2)]
        for shop in shops:
            for i in range(5):
                SyncProtokoll.objects.create(shop=shop, art="abgleich", meldung=f"Meldung {i}")
        antwort = self.client.get(reverse("shop:uebersicht"))
        for shop in antwort.context["shops"]:
            self.assertEqual(len(shop.letzte_protokolle), 3)
        wenig = self.anzahl(reverse("shop:uebersicht"))
        for i in range(3):
            Shop.objects.create(mandant=self.m, name=f"Weiterer {i}", url="https://s.example", client_id="x")
        self.assertEqual(self.anzahl(reverse("shop:uebersicht")), wenig)
