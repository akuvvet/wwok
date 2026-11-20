"""Lieferantenrechnung mit mehreren Wareneingängen (z. B. 1. und 2. Teillieferung einer Bestellung): Auswahl mit
Bestell-Nr., Summe der angehakten Wareneingänge, Abgleich über alle verknüpften."""
from decimal import Decimal as D

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.kern.models import Mandant
from apps.stammdaten.models import Artikel, Lieferant, Steuersatz

from . import services
from .models import Lieferantenrechnung


@override_settings(SECURE_SSL_REDIRECT=False)
class RechnungMehrereWareneingaengeTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.lf = Lieferant.objects.create(nummer="L1", name1="Kosatec Computer GmbH", kurzname="KOS")
        self.maus = Artikel.objects.create(nummer="M1", bezeichnung="Maus", steuersatz=Steuersatz.objects.get(nummer=1), vk1=D("15"))
        self.client.force_login(get_user_model().objects.create_superuser("chef", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()
        self.b = services.bestellung_anlegen(self.m, self.lf)
        services.bestellposition(self.b, self.maus, D("4"), D("5"))
        services.bestellen(self.b)
        self.we1, self.we2 = self.teillieferung(D("2")), self.teillieferung(D("2"))
        heute = timezone.localdate()
        self.lr = Lieferantenrechnung.objects.create(mandant=self.m, lieferant=self.lf, rechnungsnummer="R-1", datum=heute,
                                                     faellig=heute, netto=D("20"), steuer=D("3.80"), brutto=D("23.80"))

    def teillieferung(self, menge):
        we = services.wareneingang_anlegen(self.m, self.lf, self.b)
        services.wareneingang_erfassen(we, self.maus, menge, D("5"))
        services.wareneingang_buchen(we, bestaetigt=True)
        return we

    def test_beide_teillieferungen_verknuepfen_und_abgleich_passt(self):
        url = reverse("einkauf:rechnung", args=[self.lr.pk])
        seite = self.client.get(url)
        self.assertContains(seite, self.b.nummer, count=2)  # Bestell-Nr. bei beiden Wareneingängen
        self.assertContains(seite, 'id="we-summe"')
        self.assertContains(seite, 'data-netto="10.00"')
        self.client.post(url, {"wareneingang": [self.we1.pk, self.we2.pk]})
        self.assertEqual(set(self.lr.wareneingaenge.values_list("pk", flat=True)), {self.we1.pk, self.we2.pk})
        a = self.lr.abgleich()
        self.assertEqual((a["wareneingang_netto"], a["ok"]), (D("20.00"), True))
        self.assertContains(self.client.get(url), "passt zur Rechnung")

    def test_nur_eine_teillieferung_zeigt_abweichung(self):
        self.client.post(reverse("einkauf:rechnung", args=[self.lr.pk]), {"wareneingang": [self.we1.pk]})
        self.assertTrue(self.lr.abgleich()["offen"])

    def test_beim_erfassen_direkt_verknuepfen(self):
        liste = self.client.get(reverse("einkauf:rechnung_wareneingaenge"), {"lieferant": self.lf.pk}).json()["wareneingaenge"]
        self.assertEqual({w["pk"] for w in liste}, {self.we1.pk, self.we2.pk})
        self.assertEqual(liste[0]["bestellung"], self.b.nummer)
        self.assertEqual(liste[0]["netto"], "10.00")
        self.assertContains(self.client.get(reverse("einkauf:rechnungen")), 'id="re-we"')
        r = self.client.post(reverse("einkauf:rechnung_neu"), {
            "lieferant": self.lf.pk, "rechnungsnummer": "R-NEU", "datum": timezone.localdate().isoformat(),
            "netto": "20,00", "brutto": "23,80", "wareneingang": [self.we1.pk, self.we2.pk]})
        neu = Lieferantenrechnung.objects.get(rechnungsnummer="R-NEU")
        self.assertRedirects(r, reverse("einkauf:rechnung", args=[neu.pk]), fetch_redirect_response=False)
        self.assertEqual(set(neu.wareneingaenge.values_list("pk", flat=True)), {self.we1.pk, self.we2.pk})
        self.assertTrue(neu.abgleich()["ok"])
        # danach sind beide vergeben: nicht mehr in der Auswahl, auch nicht per POST an eine weitere Rechnung
        self.assertEqual(self.client.get(reverse("einkauf:rechnung_wareneingaenge"), {"lieferant": self.lf.pk}).json()["wareneingaenge"], [])
        self.client.post(reverse("einkauf:rechnung_neu"), {
            "lieferant": self.lf.pk, "rechnungsnummer": "R-NEU2", "netto": "5", "brutto": "5,95", "wareneingang": [self.we1.pk]})
        self.assertFalse(Lieferantenrechnung.objects.get(rechnungsnummer="R-NEU2").wareneingaenge.exists())

    def test_fremder_mandant_und_ohne_lieferant(self):
        andere = Mandant.objects.create(kurzname="ZONE", firmenname="ZONE Handel")
        s = self.client.session
        s["mandant_id"] = andere.pk
        s.save()
        self.assertEqual(self.client.get(reverse("einkauf:rechnung_wareneingaenge"), {"lieferant": self.lf.pk}).json()["wareneingaenge"], [])
        self.assertEqual(self.client.get(reverse("einkauf:rechnung_wareneingaenge")).json()["wareneingaenge"], [])

    def test_bezahlt_ohne_speichern_knopf(self):
        self.lr.status, self.lr.bezahlt_am, self.lr.bezahlter_betrag = "bezahlt", timezone.localdate(), D("23.80")
        self.lr.save()
        seite = self.client.get(reverse("einkauf:rechnung", args=[self.lr.pk]))
        self.assertNotContains(seite, "Verknüpfung speichern")
        self.assertContains(seite, "disabled")
