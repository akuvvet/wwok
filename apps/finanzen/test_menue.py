"""Finanzen-Menü: Offene Posten und Zahlungen jeweils für Kunden und Lieferanten."""
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.einkauf import services as einkauf
from apps.kern.models import Mandant
from apps.stammdaten.models import Lieferant


@override_settings(SECURE_SSL_REDIRECT=False)
class FinanzenMenueTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.lf = Lieferant.objects.create(nummer="L1", name1="Kosatec Computer GmbH", kurzname="KOSATEC", ort="Braunschweig")
        self.user = get_user_model().objects.create_user("t", password="x" * 14)
        self.client.force_login(self.user)
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()
        self.rechnung = einkauf.rechnung_erfassen(self.m, self.lf, "LR-4711", date(2026, 11, 1), Decimal("100"), Decimal("119"))

    def test_menue_gliedert_nach_kunden_und_lieferanten(self):
        html = self.client.get(reverse("finanzen:offene_posten")).content.decode()
        menue = html[html.index('id="seitenleiste"'):html.index('id="leistenschleier"')]
        finanzen = menue[menue.index('title="Finanzen"'):]
        reihenfolge = [finanzen.index(t) for t in (
            '<div class="sl-titel">Offene Posten</div>', 'title="Offene Posten Kunden"', 'title="Offene Posten Lieferanten"',
            '<div class="sl-titel">Zahlungen</div>', 'title="Zahlungen Kunden"', 'title="Zahlungen Lieferanten"',
            'title="Mahnwesen"', 'title="Kassenbuch"', 'title="DATEV-Export"')]
        self.assertEqual(reihenfolge, sorted(reihenfolge))
        for name in ("offene_posten", "op_lieferanten", "bank", "zahlungen_lieferanten"):
            self.assertIn(f'href="{reverse("finanzen:" + name)}"', finanzen)
        self.assertIn(f'href="{reverse("einkauf:rechnungen")}"', menue)  # bleibt auch im Einkauf-Menü

    def test_offene_posten_lieferanten(self):
        seite = self.client.get(reverse("finanzen:op_lieferanten"))
        self.assertContains(seite, "<h1>Offene Posten Lieferanten")
        self.assertContains(seite, "LR-4711")
        self.assertContains(seite, 'name="rechnungsnummer"')  # Rechnung erfassen
        self.assertContains(seite, "E-Rechnung einlesen")
        self.assertContains(seite, 'title="Offene Posten Lieferanten"><svg')
        self.assertRegex(seite.content.decode(), r'aktiv" aria-current="page" href="[^"]*" title="Offene Posten Lieferanten"')

    def test_zahlungen_lieferanten(self):
        seite = self.client.get(reverse("finanzen:zahlungen_lieferanten"))
        self.assertContains(seite, "<h1>Zahlungen Lieferanten")
        self.assertContains(seite, "LR-4711")
        self.assertContains(seite, "SEPA-Datei für Auswahl erzeugen")
        self.assertContains(seite, "Zu zahlen")
        self.assertNotContains(seite, 'name="rechnungsnummer"')  # Erfassen gehört zu den Offenen Posten
        self.assertNotContains(seite, "lieferantsuche")
        self.assertRegex(seite.content.decode(), r'aktiv" aria-current="page" href="[^"]*" title="Zahlungen Lieferanten"')
        self.rechnung.status = "exportiert"
        self.rechnung.save()
        self.assertContains(self.client.get(reverse("finanzen:zahlungen_lieferanten") + "?status=exportiert"), "LR-4711")
        self.assertNotContains(self.client.get(reverse("finanzen:zahlungen_lieferanten") + "?status=bezahlt"), "LR-4711")

    def test_zahlungen_kunden_bank_und_lastschrift(self):
        for name in ("bank", "lastschrift"):
            seite = self.client.get(reverse("finanzen:" + name))
            self.assertContains(seite, "<h1>Zahlungen Kunden:")
            self.assertContains(seite, f'href="{reverse("finanzen:bank")}">Bankabgleich</a>')
            self.assertContains(seite, f'href="{reverse("finanzen:lastschrift")}">Lastschrift-Einzug')
            self.assertRegex(seite.content.decode(), r'aktiv" aria-current="page" href="[^"]*" title="Zahlungen Kunden"')

    def test_einkauf_seite_unveraendert(self):
        seite = self.client.get(reverse("einkauf:rechnungen"))
        self.assertContains(seite, "<h1>Lieferantenrechnungen")
        self.assertContains(seite, 'name="rechnungsnummer"')
        self.assertContains(seite, "Nicht bezahlt")
