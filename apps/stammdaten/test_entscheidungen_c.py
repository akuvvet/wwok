"""Prüfbericht C, Punkt 14: doppelte EAN bei Artikeln und doppelte Lieferanten werden gewarnt (mit „Trotzdem speichern“), nicht gesperrt."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.kern.models import Mandant

from .models import Artikel, Lieferant, Steuersatz


def lieferant_daten(nummer, **extra):
    d = {"nummer": nummer, "name1": "Neu GmbH", "land": "DE", "ap-TOTAL_FORMS": "0", "ap-INITIAL_FORMS": "0", "ap-MIN_NUM_FORMS": "0",
         "ap-MAX_NUM_FORMS": "1000", "mindestbestellwert": "0", "versandkosten": "0", "lieferzeit_tage": "1"}
    d.update(extra)
    return d


@override_settings(SECURE_SSL_REDIRECT=False)
class BasisTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.st = Steuersatz.objects.create(bezeichnung="19", prozent=Decimal("19"))
        self.client.force_login(get_user_model().objects.create_user("t", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()


class ArtikelEanTest(BasisTest):
    def daten(self, **extra):
        d = {"nummer": "5000", "typ": "A", "bezeichnung": "Maus", "mengeneinheit": "Stück", "steuersatz": self.st.pk,
             "vk1": "10.00", "md-lagerplatz": "R1", "md-mindestbestand": "2", "md-maximalbestand": "10"}
        d.update(extra)
        return d

    def setUp(self):
        super().setUp()
        self.alt = Artikel.objects.create(nummer="4000", bezeichnung="Tastatur", steuersatz=self.st, ean="4012345678901")

    def test_doppelte_ean_warnt_und_speichert_nicht(self):
        r = self.client.post(reverse("stammdaten:artikel_neu"), self.daten(ean="4012345678901"))
        self.assertEqual(r.status_code, 422)
        self.assertContains(r, "4000", status_code=422)
        self.assertContains(r, "Tastatur", status_code=422)
        self.assertContains(r, "Trotzdem speichern", status_code=422)
        self.assertFalse(Artikel.objects.filter(nummer="5000").exists())

    def test_mit_bestaetigung_wird_gespeichert(self):
        r = self.client.post(reverse("stammdaten:artikel_neu"), self.daten(ean="4012345678901", trotzdem="1"))
        self.assertEqual(r.status_code, 200)
        self.assertTrue(Artikel.objects.filter(nummer="5000", ean="4012345678901").exists())

    def test_leere_oder_neue_ean_warnt_nicht(self):
        self.assertEqual(self.client.post(reverse("stammdaten:artikel_neu"), self.daten(ean="")).status_code, 200)
        self.assertEqual(self.client.post(reverse("stammdaten:artikel_neu"), self.daten(nummer="5001", ean="9999999999999")).status_code, 200)

    def test_eigener_artikel_loest_keine_warnung_aus(self):
        r = self.client.post(reverse("stammdaten:artikel_bearbeiten", args=[self.alt.pk]),
                             self.daten(nummer="4000", bezeichnung="Tastatur 2", ean="4012345678901"))
        self.assertEqual(r.status_code, 200, r.content[:300])
        self.alt.refresh_from_db()
        self.assertEqual(self.alt.bezeichnung, "Tastatur 2")

    def test_aendern_auf_fremde_ean_warnt(self):
        b = Artikel.objects.create(nummer="4001", bezeichnung="Kabel", steuersatz=self.st, ean="111")
        url = reverse("stammdaten:artikel_bearbeiten", args=[b.pk])
        r = self.client.post(url, self.daten(nummer="4001", bezeichnung="Kabel", ean="4012345678901"))
        self.assertEqual(r.status_code, 422)
        self.assertContains(r, "Tastatur", status_code=422)
        b.refresh_from_db()
        self.assertEqual(b.ean, "111")
        r = self.client.post(url, self.daten(nummer="4001", bezeichnung="Kabel", ean="4012345678901", trotzdem="1"))
        self.assertEqual(r.status_code, 200)
        b.refresh_from_db()
        self.assertEqual(b.ean, "4012345678901")

    def test_model_save_blockiert_nicht(self):
        Artikel.objects.create(nummer="4002", bezeichnung="Import", steuersatz=self.st, ean="4012345678901")
        self.assertEqual(Artikel.objects.filter(ean="4012345678901").count(), 2)


class LieferantDubletteTest(BasisTest):
    def setUp(self):
        super().setUp()
        self.alt = Lieferant.objects.create(nummer="L1", name1="API GmbH", ust_id="DE123456789", email="info@api.example")

    def test_gleicher_name_warnt_ohne_gross_klein_und_randleerzeichen(self):
        r = self.client.post(reverse("stammdaten:lieferant_neu"), lieferant_daten("L2", name1="  api gmbh "))
        self.assertEqual(r.status_code, 422)
        self.assertContains(r, "L1", status_code=422)
        self.assertContains(r, "Trotzdem speichern", status_code=422)
        self.assertFalse(Lieferant.objects.filter(nummer="L2").exists())

    def test_gleiche_ust_id_und_email_warnen(self):
        r = self.client.post(reverse("stammdaten:lieferant_neu"), lieferant_daten("L2", ust_id="DE 123456789"))
        self.assertEqual(r.status_code, 422)
        self.assertContains(r, "USt-IdNr.", status_code=422)
        r = self.client.post(reverse("stammdaten:lieferant_neu"), lieferant_daten("L2", email="INFO@api.example"))
        self.assertEqual(r.status_code, 422)
        self.assertContains(r, "E-Mail", status_code=422)

    def test_mit_bestaetigung_wird_gespeichert(self):
        r = self.client.post(reverse("stammdaten:lieferant_neu"), lieferant_daten("L2", name1="API GmbH", trotzdem="1"))
        self.assertEqual(r.status_code, 200, r.content[:300])
        self.assertTrue(Lieferant.objects.filter(nummer="L2").exists())

    def test_anderer_lieferant_und_leere_felder_warnen_nicht(self):
        Lieferant.objects.create(nummer="L5", name1="Anders", ust_id="", email="")
        r = self.client.post(reverse("stammdaten:lieferant_neu"), lieferant_daten("L6", name1="Weiter", ust_id="", email=""))
        self.assertEqual(r.status_code, 200, r.content[:300])

    def test_eigener_datensatz_loest_keine_warnung_aus_und_aenderung_warnt(self):
        url = reverse("stammdaten:lieferant_bearbeiten", args=[self.alt.pk])
        r = self.client.post(url, lieferant_daten("L1", name1="API GmbH", ust_id="DE123456789", email="info@api.example", kurzname="API"))
        self.assertEqual(r.status_code, 200, r.content[:300])
        b = Lieferant.objects.create(nummer="L3", name1="Zweiter")
        url = reverse("stammdaten:lieferant_bearbeiten", args=[b.pk])
        r = self.client.post(url, lieferant_daten("L3", name1="api gmbh"))
        self.assertEqual(r.status_code, 422)
        b.refresh_from_db()
        self.assertEqual(b.name1, "Zweiter")
        r = self.client.post(url, lieferant_daten("L3", name1="api gmbh", trotzdem="1"))
        self.assertEqual(r.status_code, 200)
