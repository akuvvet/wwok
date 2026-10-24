import xlrd
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.stammdaten.models import Zahlungsbedingung

SPALTEN = ("zb", "text", "real", "intern", "lieferstop")


class Command(BaseCommand):
    help = "Importiert Zahlungsbedingungen aus der Mention-Tabelle (zb, text, real, intern, Lieferstop)."

    def add_arguments(self, parser):
        parser.add_argument("datei")

    @transaction.atomic
    def handle(self, datei, **opts):
        blatt = xlrd.open_workbook(datei, encoding_override="cp1252").sheet_by_index(0)
        kopf = [str(c).strip().lower() for c in blatt.row_values(0)]
        for spalte in SPALTEN:
            if spalte not in kopf:
                raise CommandError(f"Spalte '{spalte}' fehlt")
        pos = {s: kopf.index(s) for s in SPALTEN}
        neu = 0
        for i in range(1, blatt.nrows):
            z = blatt.row_values(i)
            if z[pos["zb"]] == "":
                continue
            text = str(z[pos["text"]]).strip()
            _, angelegt = Zahlungsbedingung.objects.update_or_create(
                nummer=int(z[pos["zb"]]),
                defaults={
                    "bezeichnung": text,
                    "druck_text": text,
                    "tage_netto": int(z[pos["real"]]),
                    "tage_mahnung": int(z[pos["intern"]]),
                    "tage_lieferstop": int(z[pos["lieferstop"]]),
                    "vorkasse": "vorkasse" in text.lower(),
                    "lastschrift": "lastschrift" in text.lower(),
                })
            neu += angelegt
        self.stdout.write(f"{blatt.nrows - 1} Zeilen, {neu} neu angelegt")
