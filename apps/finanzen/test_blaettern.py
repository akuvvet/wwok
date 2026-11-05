"""Blätterleiste statt abgeschnittener Listen; Offene Posten mit Suche, Sortierung und Seiten."""
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.einkauf.models import Lieferant
from apps.kern.models import Mandant
from apps.stammdaten.models import Kunde, Zahlungsbedingung
from apps.verkauf.models import Beleg, Belegart, Position

from .models import Bankumsatz, Kassenbuch


@override_settings(SECURE_SSL_REDIRECT=False)
class BlaetternTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.user = get_user_model().objects.create_user("t", password="x" * 14, is_staff=True)
        self.client.force_login(self.user)
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    def test_lieferanten_blaettern(self):
        for i in range(55):
            Lieferant.objects.create(nummer=f"L{i:03d}", name1=f"Lieferant {i:03d}")
        r1 = self.client.get(reverse("einkauf:lieferanten"))
        self.assertContains(r1, 'aria-label="Seiten"')
        self.assertContains(r1, "Lieferant 000")
        self.assertNotContains(r1, "Lieferant 054")
        r2 = self.client.get(reverse("einkauf:lieferanten"), {"seite": 2})
        self.assertContains(r2, "Lieferant 054")

    def test_kassenbuch_blaettern(self):
        for i in range(1, 56):
            Kassenbuch.objects.create(mandant=self.m, nummer=i, art="E", betrag=Decimal("1"), text=f"Buchung-{i:03d}")
        r1 = self.client.get(reverse("finanzen:kassenbuch"))
        self.assertContains(r1, 'aria-label="Seiten"')
        self.assertNotContains(r1, "Buchung-001")
        self.assertContains(self.client.get(reverse("finanzen:kassenbuch"), {"seite": 2}), "Buchung-001")

    def test_bank_blaettern_mit_filter(self):
        for i in range(55):
            Bankumsatz.objects.create(mandant=self.m, buchungsdatum=date(2026, 1, 1) + timedelta(days=i), betrag=Decimal("1"),
                                      name=f"Zahler-{i:03d}", pruefsumme=f"p{i}")
        r1 = self.client.get(reverse("finanzen:bank"))
        self.assertContains(r1, "status=offen")  # Filter bleibt in den Blätterlinks
        self.assertContains(r1, 'aria-label="Seiten"')
        gesamt = sum(f"Zahler-{i:03d}" in self.client.get(reverse("finanzen:bank"), {"seite": s}).content.decode()
                     for s in (1, 2) for i in range(55))
        self.assertEqual(gesamt, 55)


@override_settings(SECURE_SSL_REDIRECT=False)
class OffenePostenBlaetternTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.user = get_user_model().objects.create_user("t", password="x" * 14, is_staff=True)
        self.client.force_login(self.user)
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()
        self.zb = Zahlungsbedingung.objects.create(bezeichnung="sofort", tage_netto=0, tage_mahnung=0)

    def rechnung(self, kunde, preis="100", faellig_tage=0):
        zb = Zahlungsbedingung.objects.create(bezeichnung=f"{faellig_tage}T", tage_netto=faellig_tage, tage_mahnung=0)
        b = Beleg.objects.create(mandant=self.m, art=Belegart.RECHNUNG, kunde=kunde, zahlungsbedingung=zb)
        Position.objects.create(beleg=b, bezeichnung="Dienst", einzelpreis=Decimal(preis))
        b.abschliessen()
        b.refresh_from_db()
        return b

    def nummern(self, antwort):
        return [z["beleg"].nummer for z in antwort.context["zeilen"]]

    def test_suche_nach_kunde_und_belegnummer(self):
        a = Kunde.objects.create(nummer="1", name1="Alpha GmbH")
        b = Kunde.objects.create(nummer="2", name1="Beta AG")
        ra, rb = self.rechnung(a), self.rechnung(b)
        url = reverse("finanzen:offene_posten")
        self.assertEqual(self.nummern(self.client.get(url, {"q": "alpha"})), [ra.nummer])
        self.assertEqual(self.nummern(self.client.get(url, {"q": rb.nummer})), [rb.nummer])
        self.assertEqual(self.nummern(self.client.get(url, {"q": "gibtsnicht"})), [])

    def test_sortierung_nach_offen_auf_und_absteigend(self):
        k = Kunde.objects.create(nummer="1", name1="Kunde GmbH")
        klein, gross, mittel = self.rechnung(k, "10"), self.rechnung(k, "300"), self.rechnung(k, "50")
        url = reverse("finanzen:offene_posten")
        self.assertEqual(self.nummern(self.client.get(url, {"sort": "offen"})), [klein.nummer, mittel.nummer, gross.nummer])
        self.assertEqual(self.nummern(self.client.get(url, {"sort": "-offen"})), [gross.nummer, mittel.nummer, klein.nummer])
        self.assertEqual(self.client.get(url, {"sort": "unsinn"}).status_code, 200)

    def test_seiten_zu_50_und_filter_bleiben_beim_blaettern(self):
        k = Kunde.objects.create(nummer="1", name1="Kunde GmbH")
        for _ in range(55):
            self.rechnung(k)
        url = reverse("finanzen:offene_posten")
        r1 = self.client.get(url, {"q": "kunde", "sort": "-offen", "nur": ""})
        self.assertEqual(len(r1.context["zeilen"]), 50)
        self.assertContains(r1, "q=kunde&amp;sort=-offen&seite=2")
        r2 = self.client.get(url, {"q": "kunde", "sort": "-offen", "seite": 2})
        self.assertEqual(len(r2.context["zeilen"]), 5)
        # Summe gilt für alle Treffer, nicht nur für die Seite
        self.assertEqual(r2.context["summe"], Decimal("55") * r2.context["zeilen"][0]["offen"])
