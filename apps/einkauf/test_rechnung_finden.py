"""Wunsch des Nutzers 20.11.2026: Eine Lieferantenrechnung zu WE2026-00039/40/41 war „nirgends zu finden“. Jetzt zeigt der
Wareneingang seine Rechnung, die Wareneingangsliste hat eine Spalte Rechnung, die Rechnungsliste eine Suche (auch nach WE-Nr.),
Schnellsuche und Kopfsuche finden Rechnungen und Wareneingänge."""
from decimal import Decimal as D

from .models import Wareneingang
from .test_rechnung_mehrere_we import RechnungMehrereWareneingaengeTest
from django.urls import reverse


class RechnungFindenTest(RechnungMehrereWareneingaengeTest):
    def test_wareneingang_zeigt_rechnung(self):
        url = reverse("einkauf:wareneingang", args=[self.we1.pk])
        self.assertContains(self.client.get(url), "noch keine Lieferantenrechnung")
        self.lr.wareneingaenge.set([self.we1])
        seite = self.client.get(url)
        self.assertContains(seite, f'href="{reverse("einkauf:rechnung", args=[self.lr.pk])}">R-1</a>')
        liste = self.client.get(reverse("einkauf:wareneingaenge"))
        self.assertContains(liste, "<th>Rechnung</th>")
        self.assertContains(liste, ">R-1</a>")

    def test_rechnungsliste_suche_nach_we_nummer_und_lieferant(self):
        self.lr.wareneingaenge.set([self.we2])
        url = reverse("einkauf:rechnungen")
        self.assertEqual(len(self.client.get(url, {"q": self.we2.nummer, "status": "alle"}).context["zeilen"]), 1)
        self.assertEqual(len(self.client.get(url, {"q": self.we1.nummer, "status": "alle"}).context["zeilen"]), 0)
        self.assertEqual(len(self.client.get(url, {"q": "kosatec r-1"}).context["zeilen"]), 1)
        self.assertContains(self.client.get(url), "data-livefilter")

    def test_schnellsuche_und_kopfsuche(self):
        self.lr.wareneingaenge.set([self.we1])
        gruppen = {t["gruppe"]: t["titel"] for t in self.client.get(reverse("schnellsuche"), {"q": self.we1.nummer}).json()["treffer"]}
        self.assertEqual(gruppen.get("Wareneingang"), self.we1.nummer)
        self.assertEqual(gruppen.get("Lieferantenrechnung"), "R-1")  # über die Nummer des verknüpften Wareneingangs
        suche = reverse("verkauf:suche")
        self.assertRedirects(self.client.get(suche, {"q": self.we1.nummer.lower()}), reverse("einkauf:wareneingang", args=[self.we1.pk]),
                             fetch_redirect_response=False)
        self.assertRedirects(self.client.get(suche, {"q": "R-1"}), reverse("einkauf:rechnung", args=[self.lr.pk]),
                             fetch_redirect_response=False)
        self.assertRedirects(self.client.get(suche, {"q": self.b.nummer}), reverse("einkauf:bestellung", args=[self.b.pk]),
                             fetch_redirect_response=False)

    # nur den Aufbau (setUp) übernehmen, die geerbten Tests nicht doppelt laufen lassen
    test_beide_teillieferungen_verknuepfen_und_abgleich_passt = test_nur_eine_teillieferung_zeigt_abweichung = None
    test_beim_erfassen_direkt_verknuepfen = test_fremder_mandant_und_ohne_lieferant = test_bezahlt_ohne_speichern_knopf = None
