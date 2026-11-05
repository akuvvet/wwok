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
