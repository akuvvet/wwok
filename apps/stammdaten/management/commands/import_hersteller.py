import xlrd
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.stammdaten.models import Hersteller


class Command(BaseCommand):
    help = "Importiert Hersteller aus der Mention-Tabelle (kennzeichen, hersteller, online)."

    def add_arguments(self, parser):
        parser.add_argument("datei")

    @transaction.atomic
    def handle(self, datei, **opts):
        blatt = xlrd.open_workbook(datei, encoding_override="cp1252").sheet_by_index(0)
        kopf = [str(c).strip().lower() for c in blatt.row_values(0)]
        for spalte in ("kennzeichen", "hersteller", "online"):
            if spalte not in kopf:
                raise CommandError(f"Spalte '{spalte}' fehlt")
        pos = {s: kopf.index(s) for s in ("kennzeichen", "hersteller", "online")}
        neu = 0
        for i in range(1, blatt.nrows):
            z = blatt.row_values(i)
            kennzeichen = str(z[pos["kennzeichen"]]).strip()
            if not kennzeichen:
                continue
            online = str(z[pos["online"]]).strip().lower() in ("ja", "1", "1.0", "true")
            _, angelegt = Hersteller.objects.update_or_create(
                kennzeichen=kennzeichen,
                defaults={"name": str(z[pos["hersteller"]]).strip(), "online": online})
            neu += angelegt
        self.stdout.write(f"{blatt.nrows - 1} Zeilen, {neu} neu angelegt")
