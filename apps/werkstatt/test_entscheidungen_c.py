"""Prüfbericht Abschnitt C, Punkt 11: „abgeholt“ nur mit Rechnung oder nach ausdrücklicher Bestätigung."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.kern.models import Mandant
from apps.stammdaten.models import Kunde

from . import services
from .models import Status


@override_settings(SECURE_SSL_REDIRECT=False)
class AbholenTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.kunde = Kunde.objects.create(nummer="1", name1="Kunde GmbH")
        self.user = get_user_model().objects.create_superuser("w1", "w@example.com", "x" * 14)

    def auftrag(self, **kw):
        return services.annehmen(self.m, self.kunde, "Notebook", "defekt", **kw)

    def test_ohne_rechnung_rueckfrage_und_nichts_geaendert(self):
        r = self.auftrag()
        with self.assertRaises(services.OhneBerechnungRueckfrage):
            services.status_setzen(r, Status.ABGEHOLT, mail=False)
        r.refresh_from_db()
        self.assertEqual(r.status, Status.ANGENOMMEN)
        self.assertIsNone(r.abgeholt_am)

    def test_mit_bestaetigung_gesetzt_und_vermerkt(self):
        r = self.auftrag()
        r.interne_notiz = "Alt"
        r.save()
        services.status_setzen(r, Status.ABGEHOLT, self.user, mail=False, ohne_berechnung=True)
        r.refresh_from_db()
        self.assertEqual(r.status, Status.ABGEHOLT)
        self.assertTrue(r.interne_notiz.startswith("Alt\n"))
        self.assertIn("Ohne Berechnung abgeholt", r.interne_notiz)

    def test_mit_rechnung_und_garantie_ohne_rueckfrage(self):
        r = self.auftrag()
        services.position_hinzufuegen(r, "A", "Arbeit", Decimal("1"), Decimal("50"))
        services.rechnung_erzeugen(r)
        services.status_setzen(r, Status.ABGEHOLT, mail=False)
        self.assertEqual(r.status, Status.ABGEHOLT)
        self.assertNotIn("Ohne Berechnung", r.interne_notiz)
        g = self.auftrag(garantie=True)
        services.status_setzen(g, Status.ABGEHOLT, mail=False)
        self.assertEqual(g.status, Status.ABGEHOLT)

    def test_andere_status_unberuehrt(self):
        r = self.auftrag()
        services.status_setzen(r, Status.FERTIG, mail=False)
        self.assertEqual(r.status, Status.FERTIG)

    def test_view_zweistufig(self):
        r = self.auftrag()
        self.client.force_login(self.user)
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()
        url = reverse("werkstatt:status", args=[r.pk])
        antwort = self.client.post(url, {"status": "abgeholt"})
        self.assertEqual(antwort.status_code, 200)
        self.assertContains(antwort, "Ohne Berechnung abholen?")
        r.refresh_from_db()
        self.assertEqual(r.status, Status.ANGENOMMEN)
        self.client.post(url, {"status": "abgeholt", "ohne_berechnung": "1"})
        r.refresh_from_db()
        self.assertEqual(r.status, Status.ABGEHOLT)
