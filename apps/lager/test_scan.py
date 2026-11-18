"""Seriennummern mit der Smartphone-Kamera scannen: Der Baustein ist auf den Seiten mit Seriennummern-Feldern eingebunden."""
from decimal import Decimal as D

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.kern.models import Mandant
from apps.stammdaten.models import Artikel, Steuersatz


class ScanTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        st = Steuersatz.objects.create(bezeichnung="19", prozent=D("19"))
        self.a = Artikel.objects.create(nummer="NT1", bezeichnung="Netzteil", steuersatz=st)

    def anmelden(self):
        self.client.force_login(get_user_model().objects.create_superuser("chef", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    def test_suche_hat_scanfeld_und_baustein(self):
        self.anmelden()
        r = self.client.get(reverse("lager:seriennummern"))
        self.assertContains(r, 'id="q" name="q" value="" autofocus size="30" data-scan')
        self.assertContains(r, "window.wwokScan = einrichten")
        self.assertContains(r, "textarea[name=seriennummern], input[name=seriennummer], [data-scan]")

    def test_artikelseite_mit_seriennummernfeld_hat_den_baustein(self):
        self.anmelden()
        r = self.client.get(reverse("lager:artikel", args=[self.a.pk]))
        self.assertContains(r, '<textarea name="seriennummern"')
        self.assertContains(r, "window.wwokScan = einrichten")

    def test_gescannt_wird_nur_im_browser(self):
        """Kein fremder Dienst und keine nachgeladene Bibliothek: Der Baustein enthält keine externe Adresse."""
        self.anmelden()
        seite = self.client.get(reverse("lager:seriennummern")).content.decode()
        baustein = seite.split("Seriennummer mit der Kamera scannen")[0].rsplit("<style>", 1)[1] + seite.split("window.wwokScan = einrichten")[0].rsplit("<script>", 1)[1]
        self.assertNotIn("http://", baustein)
        self.assertNotIn("https://", baustein)

    def test_anmeldeseite_ohne_baustein(self):
        self.assertNotContains(self.client.get(reverse("login")), "wwokScan")
