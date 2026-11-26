"""Bestellungs-PDF wie in Mention (Wunsch des Nutzers 26.11.2026): Lieferanten-Nr. und unsere Kundennr. im Kopf,
Spalte „Gesamt“, unten Summe netto, Umsatzsteuer je Satz und Endsumme."""
import io
from decimal import Decimal as D

from django.test import TestCase
from pypdf import PdfReader

from apps.kern.models import Mandant
from apps.stammdaten.models import Artikel, Lieferant, LieferantMandant, Steuersatz

from . import services
from .pdf import bestellung_pdf, steuer_aufteilung


class BestellungPdfTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.lf = Lieferant.objects.create(nummer="70016", name1="Kosatec Computer GmbH", kurzname="KOS")
        LieferantMandant.objects.create(lieferant=self.lf, mandant=self.m, unsere_kundennummer="26464")
        self.voll = Steuersatz.objects.get(nummer=1)
        self.halb = Steuersatz.objects.exclude(pk=self.voll.pk).filter(prozent=D("7")).first() or \
            Steuersatz.objects.create(bezeichnung="ermäßigt test", prozent=D("7"))

    def test_kopf_spalten_und_summen(self):
        b = services.bestellung_anlegen(self.m, self.lf)
        ram = Artikel.objects.create(nummer="R1", bezeichnung="DDR4 16GB", steuersatz=self.voll, vk1=D("120"))
        buch = Artikel.objects.create(nummer="B1", bezeichnung="Handbuch", steuersatz=self.halb, vk1=D("20"))
        services.bestellposition(b, ram, D("2"), D("92.90"))
        services.bestellposition(b, buch, D("1"), D("10.00"))
        services.bestellung_abschliessen(b)
        b.refresh_from_db()
        self.assertEqual(steuer_aufteilung(b), [(self.voll.prozent, D("35.30")), (D("7.00"), D("0.70"))])
        text = "\n".join(s.extract_text() or "" for s in PdfReader(io.BytesIO(bestellung_pdf(b))).pages)
        for erwartet in ("Lieferanten-Nr.", "70016", "Unsere Kundennr.", "26464", "Pos.", "Gesamt netto", "Summe netto",
                         "195,80", "Umsatzsteuer 19 %", "35,30", "Umsatzsteuer 7 %", "0,70", "Endsumme", "231,80"):
            self.assertIn(erwartet, text)
