from decimal import Decimal

import xlrd
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.kern.models import Mandant
from apps.lager.models import Bewegungsart, Lagerbewegung
from apps.stammdaten.models import Artikel


class Command(BaseCommand):
    help = ("Legt Anfangsbestände aus der Artikeltabelle an (Spalten Nummer und Bestand). "
            "Artikel, die für den Mandanten schon Bewegungen haben, werden übersprungen; bei doppelten Nummern gilt die erste Zeile.")

    def add_arguments(self, parser):
        parser.add_argument("datei")
        parser.add_argument("--mandant", default="OKAY")

    @transaction.atomic
    def handle(self, datei, mandant, **opts):
        m = Mandant.objects.filter(kurzname=mandant).first()
        if m is None:
            raise CommandError(f"Mandant '{mandant}' nicht gefunden")
        blatt = xlrd.open_workbook(datei, encoding_override="cp1252").sheet_by_index(0)
        kopf = [str(c).strip() for c in blatt.row_values(0)]
        for name in ("Nummer", "Bestand"):
            if name not in kopf:
                raise CommandError(f"Spalte '{name}' fehlt")
        i_nr, i_best = kopf.index("Nummer"), kopf.index("Bestand")
        gesehen, neu, uebersprungen, unbekannt, summe = set(), 0, 0, [], Decimal("0")
        for i in range(1, blatt.nrows):
            z = blatt.row_values(i)
            nummer = str(int(z[i_nr])) if isinstance(z[i_nr], float) else str(z[i_nr]).strip()
            if not nummer or nummer in gesehen:
                continue
            gesehen.add(nummer)
            menge = Decimal(str(z[i_best] or 0))
            if menge == 0:
                continue
            artikel = Artikel.objects.filter(nummer=nummer).first()
            if artikel is None:
                unbekannt.append(nummer)
                continue
            if Lagerbewegung.objects.filter(mandant=m, artikel=artikel).exists():
                uebersprungen += 1
                continue
            Lagerbewegung.objects.create(mandant=m, artikel=artikel, art=Bewegungsart.ANFANGSBESTAND, menge=menge,
                                         bemerkung="Import aus Artikeltabelle")
            neu += 1
            summe += menge
        self.stdout.write(f"{neu} Anfangsbestände für {m.kurzname} (Summe {summe} Stück), {uebersprungen} übersprungen")
        if unbekannt:
            self.stdout.write("Artikel nicht gefunden: " + ", ".join(unbekannt))
