"""Artikel & Bestand: Spalte „Onl.“ zeigt als Symbol, ob der Artikel im Shop sichtbar ist."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.kern.models import Mandant
from apps.shop.models import Shop, ShopArtikel
from apps.stammdaten.models import Artikel, Kategorie, Steuersatz


@override_settings(SECURE_SSL_REDIRECT=False)
class OnlineSpalteTest(TestCase):
    def setUp(self):
        self.m = m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        st = Steuersatz.objects.create(bezeichnung="19", prozent=Decimal("19"))
        an = Kategorie.objects.create(name="Sichtbar", online=True)
        aus = Kategorie.objects.create(name="Versteckt", online=False)
        Artikel.objects.create(nummer="A1", bezeichnung="Im Shop", steuersatz=st, online=True, kategorie=an)
        Artikel.objects.create(nummer="A2", bezeichnung="Ohne Kategorie", steuersatz=st, online=True)
        Artikel.objects.create(nummer="A3", bezeichnung="Kategorie offline", steuersatz=st, online=True, kategorie=aus)
        Artikel.objects.create(nummer="A4", bezeichnung="Nicht online", steuersatz=st, online=False, kategorie=an)
        self.client.force_login(get_user_model().objects.create_user("t", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = m.pk
        s.save()

    def test_status_je_artikel_wie_shop_sichtbar(self):
        r = self.client.get(reverse("lager:uebersicht"))
        stand = {z["artikel"].nummer: z["online"][0] for z in r.context["zeilen"]}
        self.assertEqual(stand, {"A1": "ja", "A2": "ja", "A3": "verdeckt", "A4": "nein"})
        sichtbar = set(Artikel.objects.shop_sichtbar().values_list("nummer", flat=True))  # dieselbe Regel wie der Shop
        self.assertEqual({n for n, s in stand.items() if s == "ja"}, sichtbar)

    def test_spalte_mit_symbol_und_legende(self):
        r = self.client.get(reverse("lager:uebersicht"))
        self.assertContains(r, ">Onl.<details")  # Spaltenkopf mit Legende
        self.assertContains(r, 'class="status-symbol ton-ok" role="img" tabindex="0" aria-label="online – im Shop sichtbar"', count=3)
        self.assertContains(r, 'aria-label="nicht online"', count=2)  # A4 und Legende
        self.assertContains(r, 'data-tip="online gesetzt, aber nicht im Shop: Kategorie ist offline"')
        self.assertContains(r, "status-symbol ton-warn")  # nicht online: rot
        # online gesetzt, aber nicht sichtbar: gelb mit waagerechtem Strich (Zeile A3 und Legende)
        self.assertContains(r, '<span class="status-symbol ton-hinweis" role="img" tabindex="0" '
                               'aria-label="online gesetzt, aber nicht im Shop: Kategorie ist offline"')
        self.assertContains(r, "#i-s-minus", count=2)
        self.assertContains(r, 'id="i-s-minus"')

    def test_online_aber_noch_nicht_abgeglichen_ist_gelb(self):
        shop = Shop.objects.create(mandant=self.m, name="Webshop", url="https://shop.example.test", client_id="abc")
        ShopArtikel.objects.create(shop=shop, artikel=Artikel.objects.get(nummer="A1"), produkt_id="p1")
        ShopArtikel.objects.create(shop=shop, artikel=Artikel.objects.get(nummer="A3"), produkt_id="p3")
        r = self.client.get(reverse("lager:uebersicht"))
        stand = {z["artikel"].nummer: z["online"] for z in r.context["zeilen"]}
        self.assertEqual({n: s[0] for n, s in stand.items()}, {"A1": "ja", "A2": "verdeckt", "A3": "verdeckt", "A4": "nein"})
        self.assertEqual(stand["A2"][1], "online gesetzt, aber noch nicht im Shop sichtbar: noch nicht mit dem Shop abgeglichen")
        self.assertIn("Kategorie ist offline", stand["A3"][1])  # der bisherige Grund hat Vorrang
        ShopArtikel.objects.filter(artikel__nummer="A1").update(im_shop_aktiv=False)  # im Shop deaktiviert, noch nicht wieder gemeldet
        r = self.client.get(reverse("lager:uebersicht"))
        self.assertEqual({z["artikel"].nummer: z["online"][0] for z in r.context["zeilen"]}["A1"], "verdeckt")

    def test_fremder_und_inaktiver_shop_zaehlen_nicht(self):
        zone = Mandant.objects.create(kurzname="ZONE", firmenname="ZONE Handel")
        Shop.objects.create(mandant=zone, name="Z", url="https://z.example.test", client_id="z")
        Shop.objects.create(mandant=self.m, name="Alt", url="https://alt.example.test", client_id="a", aktiv=False)
        r = self.client.get(reverse("lager:uebersicht"))
        self.assertEqual({z["artikel"].nummer: z["online"][0] for z in r.context["zeilen"]}["A1"], "ja")
