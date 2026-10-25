import json
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.kern.models import Mandant

from .models import Ansprechpartner, Kunde, KundeMandant, Lieferant, LieferantMandant


def verwaltung(praefix, **felder):
    daten = {f"{praefix}-TOTAL_FORMS": "0", f"{praefix}-INITIAL_FORMS": "0",
             f"{praefix}-MIN_NUM_FORMS": "0", f"{praefix}-MAX_NUM_FORMS": "1000"}
    daten.update(felder)
    return daten


@override_settings(SECURE_SSL_REDIRECT=False)
class StammdatenMaskenTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.kunde = Kunde.objects.create(nummer="1000", name1="Muster GmbH", plz="42651", ort="Solingen", preisstufe=1)
        self.lieferant = Lieferant.objects.create(nummer="L1", name1="API GmbH", kurzname="API")
        self.client.force_login(get_user_model().objects.create_user("t", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    def daten_kunde(self, **extra):
        d = {"nummer": "1000", "name1": "Muster GmbH", "plz": "42651", "ort": "Solingen", "land": "DE", "kundenart": "endkunde", "preisstufe": "1"}
        d.update(verwaltung("ap"))
        d.update(verwaltung("an"))
        d.update({"md-kreditlimit": ""})
        d.update(extra)
        return d

    def test_login_noetig(self):
        self.client.logout()
        r = self.client.get(reverse("stammdaten:kunde_bearbeiten", args=[self.kunde.pk]))
        self.assertEqual(r.status_code, 302)

    def test_kunde_fragment_hat_registerkarten(self):
        r = self.client.get(reverse("stammdaten:kunde_bearbeiten", args=[self.kunde.pk]))
        self.assertEqual(r.status_code, 200)
        for kennung in ("grund", "einstufung", "bank", "ap", "an", "mandant"):
            self.assertContains(r, f'data-modaltab="{kennung}"')
        self.assertContains(r, "Muster GmbH")

    def test_kunde_speichern_und_kreditlimit_je_mandant(self):
        r = self.client.post(reverse("stammdaten:kunde_bearbeiten", args=[self.kunde.pk]),
                             self.daten_kunde(name1="Muster AG", **{"md-kreditlimit": "500"}))
        self.assertEqual(r.status_code, 200, r.content[:300])
        self.assertTrue(json.loads(r.content)["ok"])
        self.kunde.refresh_from_db()
        self.assertEqual(self.kunde.name1, "Muster AG")
        self.assertEqual(KundeMandant.objects.get(kunde=self.kunde, mandant=self.m).kreditlimit, Decimal("500"))

    def test_kunde_ansprechpartner_anlegen_und_loeschen(self):
        url = reverse("stammdaten:kunde_bearbeiten", args=[self.kunde.pk])
        d = self.daten_kunde()
        d.update({"ap-TOTAL_FORMS": "1", "ap-0-nachname": "Beispiel", "ap-0-email": "b@example.org"})
        self.assertEqual(self.client.post(url, d).status_code, 200)
        ap = Ansprechpartner.objects.get(kunde=self.kunde)
        self.assertEqual(ap.nachname, "Beispiel")
        d = self.daten_kunde()
        d.update({"ap-TOTAL_FORMS": "1", "ap-INITIAL_FORMS": "1", "ap-0-id": str(ap.pk), "ap-0-kunde": str(self.kunde.pk),
                  "ap-0-nachname": "Beispiel", "ap-0-DELETE": "on"})
        self.assertEqual(self.client.post(url, d).status_code, 200)
        self.assertFalse(Ansprechpartner.objects.filter(kunde=self.kunde).exists())

    def test_fehler_liefern_422_und_markieren_registerkarte(self):
        r = self.client.post(reverse("stammdaten:kunde_bearbeiten", args=[self.kunde.pk]), self.daten_kunde(name1=""))
        self.assertEqual(r.status_code, 422)
        self.assertContains(r, "mit-fehler", status_code=422)

    def test_neukunde_nummer_und_dublettenwarnung(self):
        url = reverse("stammdaten:kunde_neu")
        r = self.client.get(url)
        self.assertContains(r, 'value="1001"')
        d = self.daten_kunde(nummer="1001")
        r = self.client.post(url, d)
        self.assertEqual(r.status_code, 422)
        self.assertContains(r, "Trotzdem anlegen", status_code=422)
        self.assertEqual(Kunde.objects.count(), 1)
        r = self.client.post(url, dict(d, trotzdem="1"))
        self.assertEqual(r.status_code, 200)
        neu = Kunde.objects.get(nummer="1001")
        self.assertEqual(json.loads(r.content)["ziel"], reverse("verkauf:kunde", args=[neu.pk]))

    def test_lieferant_bearbeiten_mit_ansprechpartner_je_mandant(self):
        url = reverse("stammdaten:lieferant_bearbeiten", args=[self.lieferant.pk])
        r = self.client.get(url)
        for kennung in ("grund", "konditionen", "bank", "ap", "mandant"):
            self.assertContains(r, f'data-modaltab="{kennung}"')
        d = {"nummer": "L1", "name1": "API Computer GmbH", "kurzname": "API", "land": "DE", "md-unsere_kundennummer": "K-77"}
        d.update(verwaltung("ap"))
        d.update({"ap-TOTAL_FORMS": "1", "ap-0-nachname": "Einkauf"})
        r = self.client.post(url, d)
        self.assertEqual(r.status_code, 200, r.content[:400])
        self.lieferant.refresh_from_db()
        self.assertEqual(self.lieferant.name1, "API Computer GmbH")
        self.assertEqual(Ansprechpartner.objects.get(lieferant=self.lieferant).mandant, self.m)
        self.assertEqual(LieferantMandant.objects.get(lieferant=self.lieferant, mandant=self.m).unsere_kundennummer, "K-77")

    def test_lieferantenliste_und_suche(self):
        r = self.client.get(reverse("einkauf:lieferanten") + "?q=api")
        self.assertContains(r, "API GmbH")
        self.assertContains(r, "data-modal")
        r = self.client.get(reverse("einkauf:lieferanten") + "?q=nichtda")
        self.assertNotContains(r, "API GmbH")
