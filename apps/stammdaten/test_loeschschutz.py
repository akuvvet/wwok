"""Löschschutz (Wunsch des Nutzers 25.11.2026): Artikel aller Typen, Kunden und Lieferanten lassen sich nicht löschen, wenn sie
irgendwo verwendet sind; Mandanten nur mit Masterpasswort."""
import os
from decimal import Decimal as D
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.db import transaction
from django.db.models import ProtectedError
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.einkauf.models import Bestellung
from apps.kern.models import Mandant
from apps.verkauf.models import Beleg, Belegart, Position

from .models import (Ansprechpartner, Artikel, GruppenKomponente, Kunde, Kundenpreis, Lieferant, Preislistenartikel,
                     Steuersatz)
from .verwendung import verwendungen


@override_settings(SECURE_SSL_REDIRECT=False)
class LoeschschutzTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.st = Steuersatz.objects.get(nummer=1)
        self.client.force_login(get_user_model().objects.create_superuser("chef", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    def artikel(self, nummer, typ="A"):
        return Artikel.objects.create(nummer=nummer, bezeichnung=nummer, steuersatz=self.st, vk1=D("10"), typ=typ)

    def test_unbenutzter_artikel_mit_eigenen_teilen_laesst_sich_loeschen(self):
        a = self.artikel("A1")
        self.assertEqual(verwendungen(a), [])
        a.delete()
        self.assertFalse(Artikel.objects.filter(nummer="A1").exists())

    def test_artikel_aller_typen_in_belegen_geschuetzt(self):
        k = Kunde.objects.create(nummer="1", name1="Kunde")
        for typ in ("A", "L", "G"):
            with self.subTest(typ):
                a = self.artikel(f"X{typ}", typ)
                b = Beleg.objects.create(mandant=self.m, art=Belegart.ANGEBOT, kunde=k)
                Position.objects.create(beleg=b, artikel=a, bezeichnung="x", menge=1, einzelpreis=D("1"))
                with self.assertRaises(ProtectedError), transaction.atomic():  # hier greift schon Djangos eigene Sperre
                    a.delete()

    def test_auch_verweise_ohne_sperre_zaehlen(self):
        """Kundenpreis, Komponente einer Gruppe, Preislistenartikel würden still mitgelöscht oder geleert – jetzt geschützt."""
        k = Kunde.objects.create(nummer="1", name1="Kunde")
        lf = Lieferant.objects.create(nummer="L1", name1="Lieferant")
        a1, a2, a3, g = self.artikel("K1"), self.artikel("K2"), self.artikel("K3"), self.artikel("G1", "G")
        Kundenpreis.objects.create(kunde=k, artikel=a1, preis=D("5"))
        GruppenKomponente.objects.create(gruppe=g, komponente=a2, menge=1)
        Preislistenartikel.objects.create(lieferant=lf, artikel=a3, bestellnummer="B1", bezeichnung="P", ek=D("1"))
        for a in (a1, a2, a3):
            with self.subTest(a.nummer), self.assertRaises(ProtectedError), transaction.atomic():
                a.delete()
        self.assertEqual(verwendungen(g), [])  # die eigenen Komponenten der Gruppe zählen nicht
        k.delete()  # Kundenpreise gehören zum Kunden
        self.assertFalse(Kundenpreis.objects.exists())

    def test_kunde_und_lieferant(self):
        k = Kunde.objects.create(nummer="1", name1="Frei")
        Ansprechpartner.objects.create(kunde=k, nachname="Teil des Kunden")
        k.delete()  # nur eigene Teile: geht
        k = Kunde.objects.create(nummer="2", name1="Benutzt")
        Beleg.objects.create(mandant=self.m, art=Belegart.ANGEBOT, kunde=k)
        with self.assertRaises(ProtectedError), transaction.atomic():
            Kunde.objects.filter(pk=k.pk).delete()  # auch die Sammellöschung
        lf = Lieferant.objects.create(nummer="L1", name1="Benutzt")
        Bestellung.objects.create(mandant=self.m, lieferant=lf)
        with self.assertRaises(ProtectedError), transaction.atomic():
            lf.delete()
        Lieferant.objects.create(nummer="L2", name1="Frei").delete()

    def test_verwaltung_zeigt_grund_statt_loeschen(self):
        k = Kunde.objects.create(nummer="2", name1="Benutzt")
        Beleg.objects.create(mandant=self.m, art=Belegart.ANGEBOT, kunde=k)
        url = reverse("admin:stammdaten_kunde_delete", args=[k.pk])
        seite = self.client.get(url)
        self.assertContains(seite, "wird noch verwendet und kann nicht gelöscht werden")
        self.client.post(url, {"post": "yes"})
        self.assertTrue(Kunde.objects.filter(pk=k.pk).exists())


@override_settings(SECURE_SSL_REDIRECT=False)
class MasterpasswortTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="ALT", firmenname="Alte Firma")
        self.client.force_login(get_user_model().objects.create_superuser("chef", password="x" * 14))
        self.url = reverse("admin:kern_mandant_delete", args=[self.m.pk])

    def test_ohne_eingerichtetes_masterpasswort_kein_loeschen(self):
        with mock.patch.dict(os.environ, {"WWOK_MASTERPASSWORT_HASH": ""}):
            antwort = self.client.post(self.url, {"post": "yes"}, follow=True)
        self.assertContains(antwort, "keines eingerichtet")
        self.assertTrue(Mandant.objects.filter(pk=self.m.pk).exists())

    def test_falsches_und_richtiges_passwort(self):
        with mock.patch.dict(os.environ, {"WWOK_MASTERPASSWORT_HASH": make_password("richtig-und-lang")}):
            self.assertContains(self.client.get(self.url), 'name="masterpasswort"')
            antwort = self.client.post(self.url, {"post": "yes", "masterpasswort": "falsch"}, follow=True)
            self.assertContains(antwort, "Masterpasswort falsch")
            self.assertTrue(Mandant.objects.filter(pk=self.m.pk).exists())
            self.client.post(self.url, {"post": "yes", "masterpasswort": "richtig-und-lang"})
        self.assertFalse(Mandant.objects.filter(pk=self.m.pk).exists())

    def test_keine_sammelloeschung(self):
        self.assertNotContains(self.client.get(reverse("admin:kern_mandant_changelist")), 'value="delete_selected"')
