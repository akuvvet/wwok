"""Abschließen mit Auswahl (PDF, Drucken, E-Mail), Ansichtsseite und eigene Dialoge statt Browser-Fenster."""
import pathlib
from decimal import Decimal

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.kern.models import Mandant
from apps.stammdaten.models import Kunde

from .models import Beleg, Belegart, Position


@override_settings(SECURE_SSL_REDIRECT=False)
class AbschlussDialogTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.kunde = Kunde.objects.create(nummer="1", name1="Muster GmbH")
        self.user = get_user_model().objects.create_user("t", password="x" * 14, is_staff=True)
        self.client.force_login(self.user)
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    def beleg(self):
        b = Beleg.objects.create(mandant=self.m, art=Belegart.ANGEBOT, kunde=self.kunde)
        Position.objects.create(beleg=b, bezeichnung="Dienst", einzelpreis=Decimal("10"))
        return b

    def abschliessen(self, b, danach):
        return self.client.post(reverse("verkauf:beleg_abschliessen", args=[b.pk]), {"danach": danach})

    def test_nur_abschliessen_fuehrt_zum_kunden(self):
        b = self.beleg()
        self.assertRedirects(self.abschliessen(b, ""), reverse("verkauf:kunde", args=[self.kunde.pk]), fetch_redirect_response=False)

    def test_danach_pdf_drucken_und_mail(self):
        b = self.beleg()
        r = self.abschliessen(b, "pdf")
        self.assertEqual(r["Location"], reverse("verkauf:beleg_ansicht", args=[b.pk]))
        b2 = self.beleg()
        self.assertEqual(self.abschliessen(b2, "druck")["Location"], reverse("verkauf:beleg_ansicht", args=[b2.pk]) + "?druck=1")
        b3 = self.beleg()
        self.assertEqual(self.abschliessen(b3, "mail")["Location"], reverse("verkauf:beleg_mail", args=[b3.pk]))
        b.refresh_from_db()
        self.assertTrue(b.abgeschlossen)

    def test_ansichtsseite_und_pdf_im_rahmen(self):
        b = self.beleg()
        self.abschliessen(b, "")
        r = self.client.get(reverse("verkauf:beleg_ansicht", args=[b.pk]), {"druck": "1"})
        self.assertContains(r, 'id="pdfrahmen"')
        self.assertContains(r, "setTimeout(drucken")
        self.assertNotContains(self.client.get(reverse("verkauf:beleg_ansicht", args=[b.pk])), "setTimeout(drucken")
        pdf = self.client.get(reverse("verkauf:beleg_pdf", args=[b.pk]))
        self.assertEqual(pdf["X-Frame-Options"], "SAMEORIGIN")

    def test_beleg_zeigt_auswahl_statt_browserfenster(self):
        r = self.client.get(reverse("verkauf:beleg", args=[self.beleg().pk]))
        self.assertContains(r, 'id="abschlussform"')
        self.assertContains(r, "Abschließen + Drucken")
        self.assertContains(r, 'name="danach"')

    def test_keine_browser_dialoge_mehr_in_den_vorlagen(self):
        for datei in pathlib.Path(settings.BASE_DIR, "templates").rglob("*.html"):
            inhalt = datei.read_text(encoding="utf-8")
            self.assertNotRegex(inhalt, r"(?<![\w.])(confirm|alert)\(", str(datei))
