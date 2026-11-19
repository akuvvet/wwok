"""Lieferanten-Gutschrift ohne Rücksendung erfassen (Wunsch des Nutzers 19.11.2026): Einkauf → Lieferanten-Gutschriften."""
from datetime import date, timedelta
from decimal import Decimal as D

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.kern.models import Mandant
from apps.stammdaten.models import Lieferant

from . import services
from .models import Lieferantengutschrift, Lieferantenrechnung

HEUTE = date.today()


@override_settings(SECURE_SSL_REDIRECT=False)
class GutschriftOhneRuecksendungTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.m2 = Mandant.objects.create(kurzname="ZONE", firmenname="ZONE Handel")
        self.lf = Lieferant.objects.create(nummer="L1", name1="Techdata GmbH", kurzname="TD")
        self.client.force_login(get_user_model().objects.create_superuser("chef", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    def daten(self, **extra):
        d = {"lieferant": str(self.lf.pk), "nummer": "GS-100", "datum": HEUTE.isoformat(), "netto": "100,00", "steuer": "19,00",
             "brutto": "119,00", "notiz": "Bonus Q3"}
        d.update(extra)
        return d

    def test_erfassen_ohne_ruecksendung(self):
        r = self.client.post(reverse("einkauf:gutschrift_neu"), self.daten())
        g = Lieferantengutschrift.objects.get()
        self.assertRedirects(r, reverse("einkauf:gutschrift", args=[g.pk]), fetch_redirect_response=False)
        self.assertEqual((g.mandant, g.lieferant, g.ruecksendung, g.nummer, g.brutto, g.notiz),
                         (self.m, self.lf, None, "GS-100", D("119.00"), "Bonus Q3"))
        self.assertEqual(g.restguthaben, D("119.00"))
        seite = self.client.get(reverse("einkauf:gutschrift", args=[g.pk]))
        self.assertContains(seite, "Gutschrift GS-100")

    def test_fehler_werden_gemeldet_nichts_angelegt(self):
        for extra in ({"lieferant": ""}, {"nummer": " "}, {"datum": ""}, {"brutto": "120,00"},
                      {"datum": (HEUTE + timedelta(days=1)).isoformat()}, {"netto": "0", "steuer": "0", "brutto": "0"}):
            r = self.client.post(reverse("einkauf:gutschrift_neu"), self.daten(**extra), follow=True)
            self.assertEqual(r.status_code, 200, extra)
            self.assertFalse(Lieferantengutschrift.objects.exists(), extra)

    def test_doppelte_nummer(self):
        self.client.post(reverse("einkauf:gutschrift_neu"), self.daten())
        r = self.client.post(reverse("einkauf:gutschrift_neu"), self.daten(), follow=True)
        self.assertContains(r, "bereits erfasst")
        self.assertEqual(Lieferantengutschrift.objects.count(), 1)

    def test_liste_offen_alle_und_mandant(self):
        g = services.gutschrift_erfassen(self.m, self.lf, "GS-1", HEUTE, D("10"), D("1.90"), D("11.90"))
        fremd = services.gutschrift_erfassen(self.m2, self.lf, "GS-FREMD", HEUTE, D("10"), D("1.90"), D("11.90"))
        re = Lieferantenrechnung.objects.create(mandant=self.m, lieferant=self.lf, rechnungsnummer="R1", datum=HEUTE, faellig=HEUTE,
                                                netto=D("100"), steuer=D("19"), brutto=D("119"))
        services.gutschrift_erfassen(self.m, self.lf, "GS-2", HEUTE, D("5"), D("0"), D("5"))
        services.gutschrift_verrechnen(Lieferantengutschrift.objects.get(nummer="GS-2"), re, D("5"))
        seite = self.client.get(reverse("einkauf:gutschriften"))
        self.assertContains(seite, "Lieferanten-Gutschriften")
        self.assertContains(seite, reverse("einkauf:gutschrift", args=[g.pk]))
        self.assertNotContains(seite, "GS-2<")  # voll verrechnet: nicht unter „Mit Restguthaben“
        self.assertNotContains(seite, fremd.nummer)
        alle = self.client.get(reverse("einkauf:gutschriften") + "?status=alle")
        self.assertContains(alle, "GS-2<")
        self.assertNotContains(alle, fremd.nummer)

    def test_menuepunkt(self):
        seite = self.client.get(reverse("einkauf:gutschriften"))
        self.assertContains(seite, 'aria-current="page" href="' + reverse("einkauf:gutschriften") + '"')
