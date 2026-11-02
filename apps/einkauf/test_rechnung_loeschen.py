"""Lieferantenrechnung löschen: Pflichtnotiz, Status, Zuordnung zum Wareneingang, Protokoll, Mandantentrennung."""
from datetime import date
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.kern.models import AenderungsProtokoll, Mandant
from apps.lager.models import bestand
from apps.stammdaten.models import Artikel, Lieferant, Steuersatz

from . import services
from .models import Lieferantenrechnung, RechnungStatus


@override_settings(SECURE_SSL_REDIRECT=False)
class RechnungLoeschenTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.m2 = Mandant.objects.create(kurzname="ZONE", firmenname="ZONE Handel")
        self.lf = Lieferant.objects.create(nummer="L1", name1="API GmbH", kurzname="API")
        st = Steuersatz.objects.create(bezeichnung="19", prozent=Decimal("19"))
        self.a = Artikel.objects.create(nummer="A1", bezeichnung="Maus", steuersatz=st)
        self.user = get_user_model().objects.create_user("t", password="x" * 14)
        self.client.force_login(self.user)
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()
        self.we = services.wareneingang_anlegen(self.m, self.lf)
        services.wareneingang_erfassen(self.we, self.a, Decimal("1"), Decimal("100"))
        services.wareneingang_buchen(self.we)
        self.r = services.rechnung_erfassen(self.m, self.lf, "R-1", date.today(), Decimal("100"), Decimal("119"), wareneingaenge=[self.we])

    def loeschen(self, notiz="Falsch erfasst", r=None):
        return self.client.post(reverse("einkauf:rechnung_loeschen", args=[(r or self.r).pk]), {"notiz": notiz}, follow=True)

    def test_loeschen_hebt_zuordnung_auf_und_protokolliert(self):
        r = self.loeschen("Doppelt erfasst")
        self.assertContains(r, "gelöscht")
        self.assertContains(r, self.we.nummer)
        self.assertFalse(Lieferantenrechnung.objects.exists())
        self.assertFalse(self.we.rechnungen.exists())
        eintrag = AenderungsProtokoll.objects.get(tabelle="einkauf.Lieferantenrechnung", aktion="loeschen")
        self.assertEqual(eintrag.vorher["rechnungsnummer"], "R-1")
        self.assertIn("Doppelt erfasst", eintrag.vorher["historie"])
        self.assertIn(self.we.nummer, eintrag.vorher["historie"])
        self.assertIn(" t: Rechnung gelöscht", eintrag.vorher["historie"])
        self.assertEqual(bestand(self.m, self.a), Decimal("1"))  # das Lager bleibt unberührt

    def test_wareneingang_und_nummer_sind_wieder_frei(self):
        self.loeschen()
        neu = services.rechnung_erfassen(self.m, self.lf, "R-1", date.today(), Decimal("100"), Decimal("119"))
        self.assertContains(self.client.get(reverse("einkauf:rechnung", args=[neu.pk])), self.we.nummer)
        self.client.post(reverse("einkauf:rechnung", args=[neu.pk]), {"wareneingang": [self.we.pk]})
        self.assertEqual(neu.wareneingaenge.get(), self.we)

    def test_notiz_ist_pflicht(self):
        for notiz in ("", "   "):
            self.assertContains(self.loeschen(notiz), "Bitte eine Notiz angeben")
        self.assertTrue(Lieferantenrechnung.objects.filter(pk=self.r.pk).exists())
        self.assertEqual(self.r.wareneingaenge.count(), 1)

    def test_bezahlt_und_exportiert_bleiben(self):
        for status, text in ((RechnungStatus.BEZAHLT, "erst die Zahlung stornieren"), (RechnungStatus.EXPORTIERT, "mit der Bank")):
            Lieferantenrechnung.objects.filter(pk=self.r.pk).update(status=status)
            antwort = self.client.get(reverse("einkauf:rechnung", args=[self.r.pk]))
            self.assertNotContains(antwort, "Beleg löschen")
            self.assertContains(self.loeschen(), text)
        self.assertEqual(self.r.wareneingaenge.count(), 1)
        self.r.refresh_from_db()
        self.assertEqual(self.r.historie, "")

    def test_nach_zahlungsstorno_loeschbar(self):
        Lieferantenrechnung.objects.filter(pk=self.r.pk).update(status=RechnungStatus.BEZAHLT, bezahlt_am=date.today(), bezahlter_betrag=Decimal("119"))
        self.r.refresh_from_db()
        self.r.zahlung_stornieren("Falsch gebucht", self.user)
        self.loeschen()
        self.assertFalse(Lieferantenrechnung.objects.exists())

    def test_ohne_wareneingang_und_formular_nur_bei_offen(self):
        ohne = services.rechnung_erfassen(self.m, self.lf, "R-2", date.today(), Decimal("10"), Decimal("11.90"))
        seite = self.client.get(reverse("einkauf:rechnung", args=[ohne.pk]))
        self.assertContains(seite, "Beleg löschen")
        self.assertNotContains(seite, "Zuordnung zum Wareneingang wird aufgehoben")
        self.assertContains(self.client.get(reverse("einkauf:rechnung", args=[self.r.pk])), "Zuordnung zum Wareneingang wird aufgehoben")
        self.assertNotContains(self.loeschen(r=ohne), "Zuordnung aufgehoben")

    def test_veraltete_kopie_legt_die_rechnung_nicht_neu_an(self):
        veraltet = Lieferantenrechnung.objects.get(pk=self.r.pk)
        self.loeschen()
        veraltet.status = RechnungStatus.BEZAHLT
        with self.assertRaisesMessage(ValueError, "inzwischen gelöscht"):
            veraltet.save()
        self.assertFalse(Lieferantenrechnung.objects.exists())

    def test_zweiter_klick_gibt_meldung(self):
        with mock.patch("apps.einkauf.views.services.rechnung_loeschen", side_effect=Lieferantenrechnung.DoesNotExist):
            antwort = self.loeschen()
        self.assertContains(antwort, "bereits gelöscht")

    def test_fremder_mandant_und_get(self):
        fremd = services.rechnung_erfassen(self.m2, self.lf, "R-9", date.today(), Decimal("10"), Decimal("11.90"))
        self.assertEqual(self.client.post(reverse("einkauf:rechnung_loeschen", args=[fremd.pk]), {"notiz": "x"}).status_code, 404)
        self.assertEqual(self.client.get(reverse("einkauf:rechnung_loeschen", args=[self.r.pk])).status_code, 405)
        self.assertEqual(Lieferantenrechnung.objects.count(), 2)
