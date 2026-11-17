"""Artikel & Bestand: Spalte „Onl.“ zeigt als Symbol, ob der Artikel im Shop sichtbar ist."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.kern.models import Mandant
from apps.stammdaten.models import Artikel, Kategorie, Steuersatz


@override_settings(SECURE_SSL_REDIRECT=False)
class OnlineSpalteTest(TestCase):
    def setUp(self):
        m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
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
        self.assertContains(r, "status-symbol ton-warn")
