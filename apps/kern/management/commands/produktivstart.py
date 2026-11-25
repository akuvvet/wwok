"""Produktivstart: Probedaten löschen, Einstellungen behalten, Belegnummern auf Anfang (siehe apps/kern/produktivstart.py).

Ohne Optionen nur Vorschau (löscht nichts):
    python manage.py produktivstart
Ausführen (legt vorher eine vollständige Sicherung an):
    python manage.py produktivstart --ausfuehren --bestaetigung PRODUKTIVSTART
"""
import os

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.kern import produktivstart as ps


class Command(BaseCommand):
    help = "Alle Probedaten löschen (Belege, Artikel, Kunden, Lieferanten ohne Preislisten-Profil …), Einstellungen behalten."

    def add_arguments(self, parser):
        parser.add_argument("--ausfuehren", action="store_true", help="Wirklich löschen (sonst nur Vorschau).")
        parser.add_argument("--bestaetigung", default="", help=f"Muss genau „{ps.BESTAETIGUNG}“ lauten.")
        parser.add_argument("--sicherung", default=os.path.join(settings.BASE_DIR, "sicherungen"),
                            help="Ordner für die Sicherung vor dem Löschen.")
        parser.add_argument("--ohne-sicherung", action="store_true", help="Keine eigene Sicherung (nur wenn schon gesichert).")

    def handle(self, *args, **opt):
        v = ps.vorschau()
        self.stdout.write("Wird GELÖSCHT:")
        for name, anzahl in sorted(v["loeschen"].items()):
            if anzahl:
                self.stdout.write(f"  {name:45} {anzahl:>8}")
        self.stdout.write("Bleibt ERHALTEN:")
        for name, anzahl in sorted(v["behalten"].items()):
            if anzahl:
                self.stdout.write(f"  {name:45} {anzahl:>8}")
        if v["unklassifiziert"]:
            raise CommandError("Nicht zugeordnete Tabellen (erst im Code einordnen): " + ", ".join(v["unklassifiziert"]))
        if not opt["ausfuehren"]:
            self.stdout.write(self.style.WARNING("Nur Vorschau – nichts gelöscht. Ausführen mit: "
                                                 f"--ausfuehren --bestaetigung {ps.BESTAETIGUNG}"))
            return
        if opt["bestaetigung"] != ps.BESTAETIGUNG:
            raise CommandError(f"Bestätigung fehlt: --bestaetigung {ps.BESTAETIGUNG}")
        if not opt["ohne_sicherung"]:
            pfad = ps.sichern(opt["sicherung"])
            self.stdout.write(f"Sicherung: {pfad} ({os.path.getsize(pfad) // 1024} KB)")
        try:
            geloescht = ps.ausfuehren(opt["bestaetigung"])
        except Exception as fehler:
            raise CommandError(f"Abgebrochen, nichts gelöscht (alles zurückgerollt): {fehler}")
        self.stdout.write(self.style.SUCCESS(f"Fertig: {sum(geloescht.values())} Zeilen gelöscht. "
                                             "Jetzt unter Verwaltung → Laufende Nummern die Startwerte eintragen."))
