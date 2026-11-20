"""Wunsch des Nutzers 20.11.2026: eine Blätterleiste für alle Tabellen, mit wählbarer Anzahl pro Seite (Inventur, Kunden,
Lieferanten, G- und L-Artikel); Suchfeld der Verwaltungslisten filtert beim Tippen."""
from decimal import Decimal as D

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.kern.models import Mandant
from apps.lager.models import Bewegungsart, Lagerbewegung
from apps.stammdaten.models import Artikel, Kunde, Leistungsartikel, Lieferant, Steuersatz


@override_settings(SECURE_SSL_REDIRECT=False)
class BlaetternTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.st = Steuersatz.objects.get(nummer=1)
        self.client.force_login(get_user_model().objects.create_superuser("chef", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    def test_artikelliste_pro_seite_wird_gemerkt(self):
        for i in range(60):
            Artikel.objects.create(nummer=f"A{i:03d}", bezeichnung=f"Artikel {i}", steuersatz=self.st)
        url = reverse("lager:uebersicht")
        r = self.client.get(url)
        self.assertEqual((r.context["seite"].pro_seite, r.context["seite"].paginator.num_pages), (50, 2))
        self.assertContains(r, 'data-pro-seite')
        r = self.client.get(url, {"pro_seite": "25", "seite": "2"})
        self.assertEqual((r.context["seite"].number, r.context["seite"].paginator.num_pages), (2, 3))
        self.assertContains(r, '<option value="25" selected>')
        self.assertNotIn("pro_seite", r.context["parameter"])  # gemerkt, nicht in jedem Link
        self.assertEqual(self.client.get(url).context["seite"].pro_seite, 25)  # gemerkt für diese Liste
        self.assertEqual(self.client.get(url, {"pro_seite": "7"}).context["seite"].pro_seite, 25)  # ungültig: gemerkte bleibt

    def test_inventur_blaettert(self):
        for i in range(30):
            a = Artikel.objects.create(nummer=f"I{i:03d}", bezeichnung=f"Teil {i}", steuersatz=self.st)
            Lagerbewegung.objects.create(mandant=self.m, artikel=a, art=Bewegungsart.ANFANGSBESTAND, menge=D("1"))
        r = self.client.get(reverse("lager:inventur_liste"), {"pro_seite": "25"})
        self.assertEqual(len(r.context["zeilen"]), 25)
        self.assertContains(r, "30 Artikel mit Bestand")
        self.assertContains(r, "Seite 1 von 2")
        self.assertContains(r, "(1–25 von 30)")
        r = self.client.get(reverse("lager:inventur_liste"), {"seite": "2"})
        self.assertEqual(len(r.context["zeilen"]), 5)

    def test_kundenliste_im_verkauf_blaettert(self):
        for i in range(30):
            Kunde.objects.create(nummer=f"{2000 + i}", name1=f"Muster {i:02d} GmbH")
        url = reverse("verkauf:start")
        r = self.client.get(url, {"q": "muster", "pro_seite": "25"})
        self.assertEqual([k.nummer for k in r.context["treffer"]][:2], ["2000", "2001"])
        self.assertEqual(len(r.context["treffer"]), 25)
        self.assertContains(r, "Seite 1 von 2")
        self.assertContains(r, "(1–25 von 30)")
        self.assertContains(r, "data-pro-seite")
        r = self.client.get(url, {"q": "muster", "seite": "2"})
        self.assertEqual([k.nummer for k in r.context["treffer"]], [f"{2025 + i}" for i in range(5)])
        self.assertRedirects(self.client.get(url, {"q": "Muster 07"}), reverse("verkauf:kunde", args=[Kunde.objects.get(nummer="2007").pk]),
                             fetch_redirect_response=False)  # ein Treffer öffnet den Kunden wie bisher

    def admin_liste(self, url, **get):
        return self.client.get(url, get)

    def test_verwaltung_kunden_blaettern_und_suche(self):
        for i in range(30):
            Kunde.objects.create(nummer=f"{1000 + i}", name1=f"Kunde {i:02d}")
        url = reverse("admin:stammdaten_kunde_changelist")
        r = self.admin_liste(url, pro_seite="25")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Seite 1 von 2 (1–25 von 30)")
        self.assertContains(r, 'class="paginator wwok-seiten"')
        self.assertContains(r, '<option value="25" selected>')
        self.assertContains(r, "weiter ›")
        self.assertEqual(len(r.context["cl"].result_list), 25)
        r = self.admin_liste(url, p="2")  # gemerkt
        self.assertEqual(len(r.context["cl"].result_list), 5)
        self.assertContains(r, "‹ zurück")
        r = self.admin_liste(url, q="Kunde 07")  # Suche und Größe zusammen
        self.assertEqual([k.name1 for k in r.context["cl"].result_list], ["Kunde 07"])
        self.assertContains(r, 'id="searchbar"')

    def test_verwaltung_lieferanten_g_und_l_artikel(self):
        for i in range(30):
            Lieferant.objects.create(nummer=f"L{i:02d}", name1=f"Lieferant {i}", kurzname=f"L{i}")
            Artikel.objects.create(nummer=f"G{i:02d}", typ="G", bezeichnung=f"Paket {i}", steuersatz=self.st)
            Leistungsartikel.objects.create(nummer=f"S{i:02d}", typ="L", bezeichnung=f"Leistung {i}", steuersatz=self.st)
        for name, get in (("admin:stammdaten_lieferant_changelist", {}), ("admin:stammdaten_artikel_changelist", {"typ__exact": "G"}),
                          ("admin:stammdaten_leistungsartikel_changelist", {})):
            r = self.admin_liste(reverse(name), pro_seite="25", **get)
            self.assertEqual(r.status_code, 200, name)
            self.assertContains(r, "Seite 1 von 2 (1–25 von 30)", msg_prefix=name)
            self.assertContains(r, "data-pro-seite", msg_prefix=name)
