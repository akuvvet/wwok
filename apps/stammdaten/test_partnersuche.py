"""Gemeinsame Kunden-/Lieferantensuche (Wunsch des Nutzers 19.11.2026): dieselbe Syntax an beiden Stellen."""
from django.test import TestCase

from apps.einkauf.lieferantenkonto import lieferanten_suchen
from apps.verkauf.views import kunden_suchen

from .models import Kunde, Lieferant


class PartnersucheTest(TestCase):
    def setUp(self):
        for nummer, name, ort in (("10001", "Kosmos Markt GmbH", "Solingen"), ("10002", "Maier Bau", "Hilden"),
                                  ("10003", "Mauer Technik GmbH", "Solingen"), ("10004", "Dekos AG", "Wuppertal")):
            Kunde.objects.create(nummer=nummer, name1=name, ort=ort)
            Lieferant.objects.create(nummer=f"L{nummer}", name1=name, ort=ort)

    def beide(self, q):
        k = sorted(kunden_suchen(q).values_list("name1", flat=True))
        l = sorted(lieferanten_suchen(q).values_list("name1", flat=True))
        self.assertEqual(k, l, f"Kunden und Lieferanten suchen unterschiedlich bei {q!r}")
        return k

    def test_stern_woerter_und_wortanfang(self):
        self.assertEqual(len(self.beide("*")), 4)
        self.assertEqual(self.beide("gmbh solingen"), ["Kosmos Markt GmbH", "Mauer Technik GmbH"])
        self.assertEqual(self.beide("kos"), ["Dekos AG", "Kosmos Markt GmbH"])
        self.assertEqual(self.beide("kos*"), ["Kosmos Markt GmbH"])  # Stern am Ende = Wortanfang

    def test_prozent_wie_like(self):
        self.assertEqual(self.beide("Ma%er%"), ["Maier Bau", "Mauer Technik GmbH"])  # beginnt mit Ma, enthält danach er
        self.assertEqual(self.beide("%gmbh"), ["Kosmos Markt GmbH", "Mauer Technik GmbH"])  # endet mit GmbH
        self.assertEqual(self.beide("%markt%"), ["Kosmos Markt GmbH"])
        self.assertEqual(self.beide("Ma%er"), [])  # verankert: muss mit „er“ enden
        self.assertEqual(len(self.beide("%")), 4)
        self.assertEqual(self.beide("%ger% solingen"), [])  # Sonderzeichen werden nicht als Regex gelesen

    def test_nummern(self):
        self.assertEqual(list(kunden_suchen("1000%").values_list("nummer", flat=True).order_by("nummer")), ["10001", "10002", "10003", "10004"])
        self.assertEqual(list(lieferanten_suchen("L10002").values_list("name1", flat=True)), ["Maier Bau"])
