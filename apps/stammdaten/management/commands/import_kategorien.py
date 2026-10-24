import xlrd
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.stammdaten.models import Kategorie


class Command(BaseCommand):
    help = "Importiert Kategorien aus der Mention-Tabelle (kategorie, bezeichnung, oberkategorie)."

    def add_arguments(self, parser):
        parser.add_argument("datei")

    @transaction.atomic
    def handle(self, datei, **opts):
        blatt = xlrd.open_workbook(datei, encoding_override="cp1252").sheet_by_index(0)
        kopf = [str(c).strip().lower() for c in blatt.row_values(0)]
        for spalte in ("kategorie", "bezeichnung", "oberkategorie"):
            if spalte not in kopf:
                raise CommandError(f"Spalte '{spalte}' fehlt")

        def text(wert):
            return str(int(wert)) if isinstance(wert, float) else str(wert).strip()

        d = [
            {"kategorie": text(z[kopf.index("kategorie")]), "bezeichnung": text(z[kopf.index("bezeichnung")]),
             "oberkategorie": text(z[kopf.index("oberkategorie")])}
            for z in (blatt.row_values(i) for i in range(1, blatt.nrows)) if z[kopf.index("kategorie")] != ""
        ]
        neu = 0
        for z in d:
            _, angelegt = Kategorie.objects.update_or_create(
                nummer=z['kategorie'].strip(), defaults={"name": z['bezeichnung'].strip()})
            neu += angelegt
        for z in d:
            if z['oberkategorie']:
                eltern = Kategorie.objects.get(nummer=z['oberkategorie'].strip())
                Kategorie.objects.filter(nummer=z['kategorie'].strip()).update(eltern=eltern)
        self.stdout.write(f"{len(d)} Zeilen, {neu} neu angelegt")
