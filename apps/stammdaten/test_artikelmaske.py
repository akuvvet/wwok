import json
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.kern.models import Mandant

from .models import Artikel, ArtikelMandant, Kategorie, Steuersatz


@override_settings(SECURE_SSL_REDIRECT=False)
class ArtikelMaskeTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.st = Steuersatz.objects.create(bezeichnung="19", prozent=Decimal("19"))
        self.client.force_login(get_user_model().objects.create_user("t", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    def daten(self, **extra):
        d = {"nummer": "5000", "typ": "A", "bezeichnung": "Maus", "mengeneinheit": "Stück", "steuersatz": self.st.pk,
             "vk1": "10.00", "md-lagerplatz": "R1", "md-mindestbestand": "2", "md-maximalbestand": "10"}
        d.update(extra)
        return d

    def test_login_noetig(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse("stammdaten:artikel_neu")).status_code, 302)

    def test_neuer_artikel_bekommt_naechste_nummer_und_registerkarten(self):
        Artikel.objects.create(nummer="24007", bezeichnung="Alt", steuersatz=self.st)
        r = self.client.get(reverse("stammdaten:artikel_neu"))
        self.assertContains(r, "24008")
        for titel in ("Stammdaten", "Preise", "Maße &amp; Versand", "Online &amp; Texte", "Mandant"):
            self.assertContains(r, titel)

    def test_anlegen_speichert_artikel_und_mandantendaten(self):
        r = self.client.post(reverse("stammdaten:artikel_neu"), self.daten())
        self.assertEqual(r.status_code, 200)
        a = Artikel.objects.get(nummer="5000")
        self.assertEqual(json.loads(r.content)["ziel"], reverse("lager:artikel", args=[a.pk]))
        md = ArtikelMandant.objects.get(artikel=a, mandant=self.m)
        self.assertEqual((md.lagerplatz, md.mindestbestand, md.maximalbestand), ("R1", 2, 10))

    def test_maximalbestand_unter_minimum_und_doppelte_nummer_werden_abgelehnt(self):
        r = self.client.post(reverse("stammdaten:artikel_neu"), self.daten(**{"md-maximalbestand": "1"}))
        self.assertEqual(r.status_code, 422)
        self.assertFalse(Artikel.objects.exists())
        Artikel.objects.create(nummer="5000", bezeichnung="Vorhanden", steuersatz=self.st)
        r = self.client.post(reverse("stammdaten:artikel_neu"), self.daten())
        self.assertEqual(r.status_code, 422)
        self.assertEqual(Artikel.objects.count(), 1)

    def test_p_artikel_nicht_anlegbar_und_typ_beim_bearbeiten_gesperrt(self):
        r = self.client.post(reverse("stammdaten:artikel_neu"), self.daten(typ="P"))
        self.assertEqual(r.status_code, 422)
        a = Artikel.objects.create(nummer="5001", bezeichnung="Lager", steuersatz=self.st)
        r = self.client.post(reverse("stammdaten:artikel_bearbeiten", args=[a.pk]), self.daten(nummer="5001", typ="L"))
        self.assertEqual(r.status_code, 200)
        a.refresh_from_db()
        self.assertEqual(a.typ, "A")  # Typwechsel wird ignoriert

    def test_artikelansicht_zeigt_bestellt_und_bearbeiten(self):
        a = Artikel.objects.create(nummer="5002", bezeichnung="Kabel", steuersatz=self.st)
        r = self.client.get(reverse("lager:artikel", args=[a.pk]))
        self.assertContains(r, "bestellt (offen)")
        self.assertContains(r, reverse("stammdaten:artikel_bearbeiten", args=[a.pk]))

    def test_esd_key_nur_in_kategorie_software(self):
        software = Kategorie.objects.create(name="Software")
        hardware = Kategorie.objects.create(name="Hardware")
        r = self.client.post(reverse("stammdaten:artikel_neu"), self.daten(esd_key="on", kategorie=hardware.pk))
        self.assertEqual(r.status_code, 422)
        r = self.client.post(reverse("stammdaten:artikel_neu"), self.daten(esd_key="on", kategorie=software.pk))
        self.assertEqual(r.status_code, 200)
        self.assertTrue(Artikel.objects.get(nummer="5000").esd_key)
