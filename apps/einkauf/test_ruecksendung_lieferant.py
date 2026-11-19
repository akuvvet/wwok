"""Wunsch des Nutzers 19.11.2026: „+ WE-Rücksendung“ steht wie „+ Bestellung“ beim Lieferanten (erst Lieferant wählen);
der Menüpunkt WE-Rücksendung in der Seitenleiste entfällt."""
from decimal import Decimal as D

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.kern.models import Mandant
from apps.stammdaten.models import Artikel, Lieferant, Steuersatz

from . import services


@override_settings(SECURE_SSL_REDIRECT=False)
class RuecksendungBeimLieferantenTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.m2 = Mandant.objects.create(kurzname="ZONE", firmenname="ZONE Handel")
        self.lf = Lieferant.objects.create(nummer="L1", name1="Techdata GmbH", kurzname="TD")
        self.maus = Artikel.objects.create(nummer="M1", bezeichnung="Maus", steuersatz=Steuersatz.objects.get(nummer=1), vk1=D("15"))
        self.client.force_login(get_user_model().objects.create_superuser("chef", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    def gebuchter_we(self, mandant=None):
        we = services.wareneingang_anlegen(mandant or self.m, self.lf)
        services.wareneingang_erfassen(we, self.maus, D("2"), D("5"))
        services.wareneingang_buchen(we, bestaetigt=True)
        return we

    def test_knopf_beim_lieferanten_mit_gebuchten_wareneingaengen(self):
        offen = services.wareneingang_anlegen(self.m, self.lf)  # ungebucht: keine Auswahl
        we = self.gebuchter_we()
        fremd = self.gebuchter_we(self.m2)
        seite = self.client.get(reverse("einkauf:lieferant", args=[self.lf.pk])).content.decode()
        self.assertIn("+ WE-Rücksendung", seite)
        self.assertIn(f'<option value="{we.pk}">aus {we.nummer}', seite)
        self.assertNotIn(f'<option value="{offen.pk}">', seite)
        self.assertNotIn(f'<option value="{fremd.pk}">', seite)

    def test_ohne_gebuchten_wareneingang_kein_knopf(self):
        self.assertNotContains(self.client.get(reverse("einkauf:lieferant", args=[self.lf.pk])), "+ WE-Rücksendung")

    def test_start_fuehrt_zur_ruecksendung_aus_dem_wareneingang(self):
        we = self.gebuchter_we()
        r = self.client.get(reverse("einkauf:ruecksendung_start"), {"we": we.pk, "lieferant": self.lf.pk})
        self.assertRedirects(r, reverse("einkauf:ruecksendung_neu", args=[we.pk]), fetch_redirect_response=False)

    def test_start_ohne_auswahl_und_fremder_mandant(self):
        r = self.client.get(reverse("einkauf:ruecksendung_start"), {"lieferant": self.lf.pk})
        self.assertRedirects(r, reverse("einkauf:lieferant", args=[self.lf.pk]), fetch_redirect_response=False)
        fremd = self.gebuchter_we(self.m2)
        self.assertEqual(self.client.get(reverse("einkauf:ruecksendung_start"), {"we": fremd.pk}).status_code, 404)

    def test_kein_menuepunkt_mehr_in_der_seitenleiste(self):
        seite = self.client.get(reverse("einkauf:lieferanten")).content.decode()
        self.assertNotIn('<span>WE-Rücksendung</span>', seite)
        self.assertNotIn('href="' + reverse("einkauf:ruecksendungen") + '"', seite)
