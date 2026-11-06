from datetime import date
from decimal import Decimal as D

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.kern.models import Mandant
from apps.stammdaten.models import Kunde, Zahlungsbedingung
from apps.verkauf.models import Beleg, Belegart, Position

from . import services
from .models import Kassenbuch, Zahlung


@override_settings(SECURE_SSL_REDIRECT=False)
class DoppelklickSchutzTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        kunde = Kunde.objects.create(nummer="1", name1="Kunde GmbH")
        zb = Zahlungsbedingung.objects.create(bezeichnung="10 Tage", tage_netto=10, tage_mahnung=11)
        self.client.force_login(get_user_model().objects.create_superuser("chef", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()
        self.r = Beleg.objects.create(mandant=self.m, art=Belegart.RECHNUNG, kunde=kunde, zahlungsbedingung=zb,
                                      belegdatum=date.today())
        Position.objects.create(beleg=self.r, bezeichnung="Dienst", einzelpreis=D("100"))
        self.r.abschliessen()
        self.r.refresh_from_db()

    def test_zahlung_zweimal_absenden_bucht_einmal(self):
        seite = reverse("finanzen:beleg", args=[self.r.pk])
        self.assertContains(self.client.get(seite), 'name="offen_bisher" value="119,00"')
        daten = {"betrag": "119,00", "datum": date.today().isoformat(), "zahlart": "UEB", "offen_bisher": "119,00"}
        url = reverse("finanzen:zahlung", args=[self.r.pk])
        self.client.post(url, daten)
        r = self.client.post(url, daten, follow=True)
        self.assertContains(r, "offene Betrag hat sich")
        self.assertEqual(Zahlung.objects.filter(beleg=self.r).count(), 1)
        self.assertEqual(services.offen(self.r), D("0.00"))

    def test_kassenbuch_zweimal_absenden_bucht_einmal(self):
        url = reverse("finanzen:kassenbuch")
        token = self.client.get(url).context["token"]
        daten = {"art": Kassenbuch.EINNAHME, "betrag": "10,00", "text": "Einlage", "token": token}
        self.client.post(url, daten)
        r = self.client.post(url, daten, follow=True)
        self.assertContains(r, "bereits gebucht")
        self.assertEqual(Kassenbuch.objects.count(), 1)

    def test_kassenbuch_neues_formular_hat_neues_token(self):
        url = reverse("finanzen:kassenbuch")
        t1 = self.client.get(url).context["token"]
        t2 = self.client.get(url).context["token"]
        self.assertNotEqual(t1, t2)
        self.client.post(url, {"art": Kassenbuch.EINNAHME, "betrag": "5", "text": "a", "token": t1})
        self.client.post(url, {"art": Kassenbuch.EINNAHME, "betrag": "5", "text": "b", "token": t2})
        self.assertEqual(Kassenbuch.objects.count(), 2)


@override_settings(SECURE_SSL_REDIRECT=False)
class AngebotKopierenTest(TestCase):
    """Fehler 06.11.2026: „Kopieren als Angebot“ meldete „kann nicht als Kopie angelegt werden“. Der Doppelklick-Schutz
    sperrte den Knopf, obwohl die Rückfrage „Angebot abschließen?“ das Absenden angehalten hatte; beim erneuten
    Absenden fehlte dann die Belegart."""

    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.client.force_login(get_user_model().objects.create_superuser("chef", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()
        self.an = Beleg.objects.create(mandant=self.m, art=Belegart.ANGEBOT, kunde=Kunde.objects.create(nummer="1", name1="K"))
        Position.objects.create(beleg=self.an, bezeichnung="Dienst", einzelpreis=D("10"))
        self.an.abschliessen()

    def test_sperre_nur_wenn_das_absenden_nicht_angehalten_wurde(self):
        seite = self.client.get(reverse("verkauf:beleg", args=[self.an.pk])).content.decode()
        self.assertIn("if (e && e.defaultPrevented) return;", seite)
        self.assertIn("sperren(f, knopf, e)", seite)

    def test_angebot_als_angebot_kopieren(self):
        for urbeleg in ("0", "1"):
            r = self.client.post(reverse("verkauf:beleg_kopieren", args=[self.an.pk]), {"art": "AN", "urbeleg_abschliessen": urbeleg})
            neu = Beleg.objects.filter(art=Belegart.ANGEBOT).exclude(pk=self.an.pk).latest("pk")
            self.assertRedirects(r, reverse("verkauf:beleg", args=[neu.pk]), fetch_redirect_response=False)
        self.assertEqual(Beleg.objects.filter(art=Belegart.ANGEBOT).count(), 3)
