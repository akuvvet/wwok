"""Prüfbericht C, Stammdaten: Nummernkollision beim gleichzeitigen Anlegen ergibt eine neue Nummer statt Fehler 500."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.kern.models import Mandant

from . import masken
from .models import Artikel, Lieferant, Steuersatz, Stammnummer


def lieferant_daten(nummer):
    return {"nummer": nummer, "name1": "API", "land": "DE", "ap-TOTAL_FORMS": "0", "ap-INITIAL_FORMS": "0", "ap-MIN_NUM_FORMS": "0",
            "ap-MAX_NUM_FORMS": "1000", "mindestbestellwert": "0", "versandkosten": "0", "lieferzeit_tage": "1"}


@override_settings(SECURE_SSL_REDIRECT=False)
class NummernkollisionTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.st = Steuersatz.objects.create(bezeichnung="19", prozent=Decimal("19"))
        user = get_user_model().objects.create_user("t", password="x" * 14, is_staff=True)
        self.client.force_login(user)
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    def konkurrent(self, original, modell, **felder):
        """Ersetzt die Prüfung: Nach erfolgreicher Prüfung legt ein zweiter Benutzer dieselbe Nummer an."""
        def ersatz(*formulare):
            ergebnis = original(*formulare)
            modell.objects.create(**felder)
            return ergebnis
        return ersatz

    def test_lieferant_bekommt_neue_nummer_statt_fehler_500(self):
        Stammnummer.setzen("LF", 300)
        ersatz = self.konkurrent(masken.alle_gueltig, Lieferant, nummer="300", name1="Schneller")
        masken.alle_gueltig, original = ersatz, masken.alle_gueltig
        try:
            r = self.client.post(reverse("stammdaten:lieferant_neu"), lieferant_daten("300"))
        finally:
            masken.alle_gueltig = original
        self.assertEqual(r.status_code, 422)
        self.assertContains(r, "inzwischen vergeben", status_code=422)
        self.assertContains(r, 'value="301"', status_code=422)
        self.assertEqual(Lieferant.objects.filter(name1="API").count(), 0)
        # erneutes Speichern mit der neuen Nummer gelingt
        r = self.client.post(reverse("stammdaten:lieferant_neu"), lieferant_daten("301"))
        self.assertEqual(r.status_code, 200)
        self.assertTrue(Lieferant.objects.filter(nummer="301", name1="API").exists())

    def test_artikel_bekommt_neue_nummer_statt_fehler_500(self):
        Stammnummer.setzen("A", 3000)
        daten = {"nummer": "3000", "typ": "A", "bezeichnung": "Maus", "steuersatz": self.st.pk, "mengeneinheit": "Stück",
                 "md-mindestbestand": "", "md-maximalbestand": "", "md-lagerplatz": ""}
        original = masken.alle_gueltig
        masken.alle_gueltig = self.konkurrent(original, Artikel, nummer="3000", bezeichnung="Schneller", steuersatz=self.st)
        try:
            r = self.client.post(reverse("stammdaten:artikel_neu"), daten)
        finally:
            masken.alle_gueltig = original
        self.assertEqual(r.status_code, 422)
        self.assertContains(r, "inzwischen vergeben", status_code=422)
        self.assertContains(r, 'value="3001"', status_code=422)
        self.assertFalse(Artikel.objects.filter(bezeichnung="Maus").exists())

    def test_ohne_kollision_bleibt_alles_wie_bisher(self):
        Stammnummer.setzen("LF", 300)
        r = self.client.post(reverse("stammdaten:lieferant_neu"), lieferant_daten("300"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Stammnummer.vorschlag("LF"), "301")
