from decimal import Decimal as D

from django.contrib import admin
from django.test import TestCase

from apps.stammdaten.models import Artikel, Kategorie, Steuersatz


class KategorieOnlineTest(TestCase):
    def setUp(self):
        self.st = Steuersatz.objects.create(bezeichnung="19", prozent=D("19"))
        self.ober = Kategorie.objects.create(name="Hardware", online=True)
        self.unter = Kategorie.objects.create(name="Mäuse", eltern=self.ober, online=True)

    def artikel(self, nummer, kategorie=None, **kw):
        return Artikel.objects.create(nummer=nummer, bezeichnung=nummer, steuersatz=self.st, vk1=D("1"), online=True,
                                      kategorie=kategorie, **kw)

    def nummern(self):
        return set(Artikel.objects.shop_sichtbar().values_list("nummer", flat=True))

    def test_neue_kategorie_vorgabe_offline(self):
        self.assertFalse(Kategorie.objects.create(name="Neu").online)

    def test_migration_setzt_bestehende_online(self):
        from importlib import import_module
        mig = import_module("apps.stammdaten.migrations.0028_kategorie_online")
        Kategorie.objects.update(online=False)
        from django.apps import apps
        mig.bestehende_online(apps, None)
        self.assertFalse(Kategorie.objects.filter(online=False).exists())

    def test_regel_ohne_kategorie_online_offline_und_vererbung(self):
        self.artikel("O", None)
        self.artikel("U", self.unter)
        self.artikel("H", self.ober)
        self.artikel("AUS", self.unter, nicht_aktiv=True)
        self.assertEqual(self.nummern(), {"O", "U", "H"})
        self.unter.online = False
        self.unter.save()
        self.assertEqual(self.nummern(), {"O", "H"})
        self.unter.online = True
        self.unter.save()
        self.ober.online = False
        self.ober.save()
        self.assertEqual(self.nummern(), {"O"})  # Unterkategorie online, Eltern offline: Artikel offline
        self.ober.online = True
        self.ober.save()
        self.assertEqual(self.nummern(), {"O", "U", "H"})  # Checkbox nie überschrieben

    def test_artikel_checkbox_bleibt_unveraendert(self):
        a = self.artikel("U", self.unter)
        self.ober.online = False
        self.ober.save()
        a.refresh_from_db()
        self.assertTrue(a.online)
        self.assertEqual(self.unter.offline_grund(), self.ober)
        self.assertEqual(self.ober.mit_unterkategorien(), {self.ober.pk, self.unter.pk})

    def test_admin_status_und_artikelanzahl(self):
        ma = admin.site._registry[Kategorie]
        self.artikel("U1", self.unter)
        self.artikel("H1", self.ober)
        self.assertEqual(ma.betroffene_artikel(self.ober), 2)
        self.assertEqual(ma.shop_status(self.unter), "online")
        self.ober.online = False
        self.ober.save()
        self.assertEqual(ma.shop_status(self.unter), "offline (über Hardware)")
        self.assertEqual(ma.shop_status(self.ober), "offline")

    def test_artikel_hinweis_bei_offline_kategorie(self):
        ma = admin.site._registry[Artikel]
        a = self.artikel("U1", self.unter)
        self.assertEqual(ma.shop_hinweis(a), "–")
        self.ober.online = False
        self.ober.save()
        a.refresh_from_db()
        self.assertIn("Hardware", ma.shop_hinweis(a))
