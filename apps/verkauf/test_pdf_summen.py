"""PDF: Summe netto, Umsatzsteuer und Gesamtbetrag stehen immer zusammen auf einer Seite."""
from decimal import Decimal
from io import BytesIO

from django.test import TestCase
from pypdf import PdfReader

from apps.kern.models import Mandant
from apps.stammdaten.models import Kunde
from apps.werkstatt.models import Reparatur, ReparaturPosition
from apps.werkstatt.pdf import schein_pdf

from .models import Beleg, Belegart, Position
from .pdf import beleg_pdf, euro


def seiten(pdf: bytes) -> list:
    return [s.extract_text() or "" for s in PdfReader(BytesIO(pdf)).pages]


def seite_von(texte, wort) -> list:
    return [i for i, t in enumerate(texte) if wort in t]


class SummenblockTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer", strasse="Weg 1", plz="42651", ort="Solingen")
        self.k = Kunde.objects.create(nummer="1", name1="Muster GmbH", strasse="Str. 2", plz="10115", ort="Berlin")

    def test_beleg_summen_nie_auf_zwei_seiten(self):
        b = Beleg.objects.create(mandant=self.m, art=Belegart.RECHNUNG, kunde=self.k, versandkosten=Decimal("5"),
                                 schlusstext="Vielen Dank fuer Ihren Auftrag.")
        mehrseitig = 0
        for n in range(1, 61):  # jede Positionsanzahl: irgendwann fällt der Summenblock genau auf den Seitenrand
            Position.objects.create(beleg=b, bezeichnung=f"Artikel {n}", einzelpreis=Decimal("10"), steuer_prozent=Decimal("19"))
            if n < 15:
                continue
            b.berechne()
            texte = seiten(beleg_pdf(b))
            orte = {w: seite_von(texte, w) for w in ("Summe netto", "Umsatzsteuer", "Gesamtbetrag")}
            with self.subTest(positionen=n):
                self.assertTrue(all(len(o) == 1 for o in orte.values()), orte)
                self.assertEqual(len({o[0] for o in orte.values()}), 1, orte)
                self.assertIn(euro(b.summe_brutto), texte[orte["Gesamtbetrag"][0]])
            mehrseitig += len(texte) > 1
        self.assertGreater(mehrseitig, 20)  # der Test deckt mehrseitige Belege wirklich ab

    def test_werkstatt_abholschein_summen_nie_auf_zwei_seiten(self):
        r = Reparatur.objects.create(mandant=self.m, nummer="WS1", kunde=self.k, geraet="Notebook", fehler="startet nicht")
        mehrseitig = 0
        for n in range(1, 61):
            ReparaturPosition.objects.create(reparatur=r, art="T", bezeichnung=f"Teil {n}", einzelpreis=Decimal("10"))
            if n < 15:
                continue
            texte = seiten(schein_pdf(r, abholung=True))
            netto = euro(r.summe_netto)
            orte = [seite_von(texte, "Gesamt"), seite_von(texte, "Steuer"), seite_von(texte, netto)]
            with self.subTest(positionen=n):
                self.assertTrue(all(len(o) == 1 for o in orte), orte)
                self.assertEqual(len({o[0] for o in orte}), 1, orte)
            mehrseitig += len(texte) > 1
        self.assertGreater(mehrseitig, 20)
