"""Preislistenartikel-Verwaltung: Lieferant nur als Kurzname (erstes Wort von Name 1), Issue 36."""
from decimal import Decimal as D

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from .admin import PreislistenartikelAdmin
from .models import Lieferant, Preislistenartikel


@override_settings(SECURE_SSL_REDIRECT=False)
class PreislistenartikelKurzanzeigeTest(TestCase):
    def setUp(self):
        self.ma = PreislistenartikelAdmin(Preislistenartikel, admin.site)
        self.kosa = Lieferant.objects.create(nummer="70016", name1="Kosatec Computer GmbH")
        self.leer = Lieferant.objects.create(nummer="70099", name1="")
        self.p1 = Preislistenartikel.objects.create(lieferant=self.kosa, bestellnummer="A1", bezeichnung="X", ek=D("1.00"))
        self.p2 = Preislistenartikel.objects.create(lieferant=self.leer, bestellnummer="B1", bezeichnung="Y", ek=D("2.00"))

    def test_erstes_wort_mit_tooltip(self):
        html = str(self.ma.lieferant_kurz(self.p1))
        self.assertIn(">Kosatec</span>", html)
        self.assertIn('title="70016 – Kosatec Computer GmbH"', html)

    def test_leerer_name_zeigt_nummer(self):
        self.assertIn(">70099</span>", str(self.ma.lieferant_kurz(self.p2)))

    def test_gepflegter_kurzname_hat_vorrang(self):
        self.kosa.kurzname = "KT"
        html = str(self.ma.lieferant_kurz(self.p1))
        self.assertIn(">KT</span>", html)
        self.assertIn('title="70016 – Kosatec Computer GmbH"', html)

    def test_auswahl_alphabetisch_nach_kurzname(self):
        from .models import lieferanten_auswahl
        Lieferant.objects.create(nummer="70001", name1="Zeta AG", kurzname="Alpha")
        Lieferant.objects.create(nummer="70002", name1="Beta GmbH")
        Lieferant.objects.create(nummer="70003", name1="Gesperrt AG", kurzname="Aaa", gesperrt=True)
        namen = [lf.kurz for lf in lieferanten_auswahl()]
        self.assertEqual([n for n in namen if n != "70099"], ["Alpha", "Beta", "Kosatec"])
        self.assertNotIn("Aaa", namen)
        self.assertIn("Aaa", [lf.kurz for lf in lieferanten_auswahl(gesperrte=True)])

    def test_sortierung_und_link(self):
        self.assertEqual(self.ma.lieferant_kurz.admin_order_field, "lieferant__name1")
        self.assertEqual(self.ma.list_display_links, ["lieferant_kurz"])

    def test_liste_im_admin(self):
        self.client.force_login(get_user_model().objects.create_superuser("chef", password="x" * 14))
        url = reverse("admin:stammdaten_preislistenartikel_changelist")
        r = self.client.get(url)
        self.assertContains(r, ">Kosatec</span>")
        self.assertEqual(self.client.get(url + "?o=1").status_code, 200)
