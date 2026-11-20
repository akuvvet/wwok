"""Wunsch des Nutzers 20.11.2026: Ansprechpartner auch im Kundenbeleg änderbar (im Belegkopf, mit Telefon und E-Mail)."""
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.kern.models import Mandant
from apps.stammdaten.models import Ansprechpartner, Kunde

from .models import Beleg


@override_settings(SECURE_SSL_REDIRECT=False)
class BelegAnsprechpartnerTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.k = Kunde.objects.create(nummer="K1", name1="Muster GmbH")
        self.erster = Ansprechpartner.objects.create(kunde=self.k, nachname="Albers", vorname="Axel", telefon="0212 1")
        self.haupt = Ansprechpartner.objects.create(kunde=self.k, nachname="Zeller", vorname="Petra", email="p@example.com",
                                                    hauptkontakt=True)
        self.fremd = Ansprechpartner.objects.create(kunde=Kunde.objects.create(nummer="K2", name1="Andere"), nachname="Fremd")
        self.user = get_user_model().objects.create_superuser("chef", password="x" * 14)
        self.client.force_login(self.user)
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    def beleg(self, **kw):
        return Beleg.objects.create(mandant=self.m, art=kw.pop("art", "AN"), kunde=self.k, ansprechpartner=self.erster, **kw)

    def test_kopf_zeigt_daten_und_wechselt_sofort(self):
        b = self.beleg()
        seite = self.client.get(reverse("verkauf:beleg", args=[b.pk]))
        self.assertEqual(seite.context["ansprechpartner"], [self.haupt, self.erster])  # Hauptkontakt zuerst
        self.assertContains(seite, reverse("verkauf:beleg_ansprechpartner", args=[b.pk]))
        self.assertContains(seite, "tel:0212 1")
        url = reverse("verkauf:beleg_ansprechpartner", args=[b.pk])
        self.client.post(url, {"ansprechpartner": self.haupt.pk})
        b.refresh_from_db()
        self.assertEqual(b.ansprechpartner, self.haupt)
        self.assertContains(self.client.get(reverse("verkauf:beleg", args=[b.pk])), "mailto:p@example.com")
        self.client.post(url, {"ansprechpartner": self.fremd.pk})  # anderer Kunde: abgelehnt
        b.refresh_from_db()
        self.assertEqual(b.ansprechpartner, self.haupt)
        self.client.post(url, {"ansprechpartner": ""})
        b.refresh_from_db()
        self.assertIsNone(b.ansprechpartner)

    def test_kopf_speichern_ohne_feld_behaelt_ansprechpartner(self):
        b = self.beleg()
        self.client.post(reverse("verkauf:beleg_kopf", args=[b.pk]), {"versandart": "", "bemerkung": "x"})
        b.refresh_from_db()
        self.assertEqual((b.ansprechpartner, b.bemerkung), (self.erster, "x"))

    def test_gesperrter_beleg_bleibt(self):
        b = self.beleg(art="RE", abgeschlossen=True)
        seite = self.client.get(reverse("verkauf:beleg", args=[b.pk]))
        self.assertNotContains(seite, reverse("verkauf:beleg_ansprechpartner", args=[b.pk]))
        self.assertContains(seite, "Axel Albers")  # wird nur angezeigt
        self.client.post(reverse("verkauf:beleg_ansprechpartner", args=[b.pk]), {"ansprechpartner": self.haupt.pk})
        b.refresh_from_db()
        self.assertEqual(b.ansprechpartner, self.erster)
