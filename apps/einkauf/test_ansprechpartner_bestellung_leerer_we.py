"""Wünsche des Nutzers 20.11.2026: Ansprechpartner in der Lieferantenbestellung; Wareneingang ohne Position wird beim
Verlassen nicht aufbewahrt."""
from datetime import timedelta
from decimal import Decimal as D

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.kern.models import Mandant
from apps.stammdaten.models import Anrede, Ansprechpartner, Artikel, Lieferant, Steuersatz

from . import services
from .models import Bestellung, Lieferantenrechnung, Wareneingang
from .pdf import bestellung_pdf


@override_settings(SECURE_SSL_REDIRECT=False)
class AnsprechpartnerBestellungTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.m2 = Mandant.objects.create(kurzname="ZONE", firmenname="ZONE Handel")
        self.lf = Lieferant.objects.create(nummer="L1", name1="Kosatec Computer GmbH", kurzname="KOS")
        self.maus = Artikel.objects.create(nummer="M1", bezeichnung="Maus", steuersatz=Steuersatz.objects.get(nummer=1), vk1=D("15"))
        self.user = get_user_model().objects.create_superuser("chef", password="x" * 14)
        self.client.force_login(self.user)
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()
        frau = Anrede.objects.filter(bezeichnung="Frau").first() or Anrede.objects.create(nummer=93, bezeichnung="Frau")
        self.erster = Ansprechpartner.objects.create(lieferant=self.lf, mandant=self.m, nachname="Albers", email="a@example.com")
        self.zweiter = Ansprechpartner.objects.create(lieferant=self.lf, mandant=self.m, nachname="Zeller", vorname="Petra",
                                                      anrede=frau, email="z@example.com")
        self.fremd = Ansprechpartner.objects.create(lieferant=self.lf, mandant=self.m2, nachname="Fremd", email="f@example.com")

    def test_plus_bestellung_uebernimmt_gewaehlten_sonst_ersten(self):
        html = self.client.get(reverse("einkauf:lieferant", args=[self.lf.pk])).content.decode()
        self.assertIn('name="ansprechpartner" form="bestellung-neu"', html)
        self.assertIn('id="bestellung-neu"', html)
        for ap in (self.zweiter.pk, "", self.fremd.pk):
            self.client.post(reverse("einkauf:bestellung_neu"), {"lieferant": self.lf.pk, "ansprechpartner": ap})
        self.assertEqual([b.ansprechpartner for b in Bestellung.objects.order_by("pk")], [self.zweiter, self.erster, self.erster])

    def test_im_tab_aendern_pdf_und_mail(self):
        b = services.bestellung_anlegen(self.m, self.lf)
        self.assertEqual(b.ansprechpartner, self.erster)
        services.bestellposition(b, self.maus, D("1"), D("5"))
        seite = self.client.get(reverse("einkauf:bestellung", args=[b.pk]))
        self.assertContains(seite, 'name="ansprechpartner"')
        self.assertNotContains(seite, "Fremd")  # nur Ansprechpartner des Mandanten
        url = reverse("einkauf:bestellung_kopf", args=[b.pk])
        self.client.post(url, {"tab": "versand", "ansprechpartner": self.zweiter.pk})
        b.refresh_from_db()
        self.assertEqual(b.ansprechpartner, self.zweiter)
        import io

        from pypdf import PdfReader
        text = "\n".join(s.extract_text() or "" for s in PdfReader(io.BytesIO(bestellung_pdf(b))).pages)
        self.assertIn("z. Hd. Frau Petra Zeller", text)
        self.assertEqual(self.client.get(reverse("einkauf:bestellung_mail", args=[b.pk])).context["an"], "z@example.com")
        self.client.post(url, {"tab": "versand", "ansprechpartner": self.fremd.pk})  # fremder Mandant: abgelehnt
        b.refresh_from_db()
        self.assertEqual(b.ansprechpartner, self.zweiter)
        self.client.post(url, {"tab": "versand", "ansprechpartner": ""})
        b.refresh_from_db()
        self.assertIsNone(b.ansprechpartner)


@override_settings(SECURE_SSL_REDIRECT=False)
class LeererWareneingangTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.lf = Lieferant.objects.create(nummer="L1", name1="Kosatec Computer GmbH", kurzname="KOS")
        self.maus = Artikel.objects.create(nummer="M1", bezeichnung="Maus", steuersatz=Steuersatz.objects.get(nummer=1), vk1=D("15"))
        self.user = get_user_model().objects.create_superuser("chef", password="x" * 14)
        self.anderer = get_user_model().objects.create_user("lager", password="x" * 14)
        self.client.force_login(self.user)
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    def test_eigener_leerer_we_beim_zurueck_zum_lieferanten_weg_nummer_frei(self):
        we = services.wareneingang_anlegen(self.m, self.lf, benutzer=self.user)
        seite = self.client.get(reverse("einkauf:lieferant", args=[self.lf.pk]))
        self.assertFalse(Wareneingang.objects.filter(pk=we.pk).exists())
        self.assertNotContains(seite, we.nummer)
        self.assertEqual(services.wareneingang_anlegen(self.m, self.lf, benutzer=self.user).nummer, we.nummer)  # keine Lücke

    def test_mit_position_gebucht_oder_an_rechnung_bleibt(self):
        mit = services.wareneingang_anlegen(self.m, self.lf, benutzer=self.user)
        services.wareneingang_erfassen(mit, self.maus, D("1"), D("5"))
        an_rechnung = services.wareneingang_anlegen(self.m, self.lf, benutzer=self.user)
        heute = timezone.localdate()
        lr = Lieferantenrechnung.objects.create(mandant=self.m, lieferant=self.lf, rechnungsnummer="R1", datum=heute, faellig=heute,
                                                netto=D("1"), brutto=D("1.19"))
        lr.wareneingaenge.add(an_rechnung)
        self.assertEqual(services.leere_wareneingaenge_entfernen(self.m, benutzer=self.user), 0)

    def test_fremder_erst_nach_minuten(self):
        we = services.wareneingang_anlegen(self.m, self.lf, benutzer=self.anderer)
        self.client.get(reverse("einkauf:wareneingaenge"))
        self.assertTrue(Wareneingang.objects.filter(pk=we.pk).exists())  # der Kollege arbeitet vielleicht gerade daran
        Wareneingang.objects.filter(pk=we.pk).update(geaendert_am=timezone.now() - timedelta(minutes=5))
        self.client.get(reverse("einkauf:lieferanten"))
        self.assertFalse(Wareneingang.objects.filter(pk=we.pk).exists())
