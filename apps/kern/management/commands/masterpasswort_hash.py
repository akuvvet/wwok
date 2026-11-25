"""Hash für das Masterpasswort erzeugen (Löschen von Mandanten). Ausgabe als Zeile für die .env; das Passwort selbst wird nirgends
gespeichert:
    python manage.py masterpasswort_hash
"""
import getpass

from django.contrib.auth.hashers import make_password
from django.core.management.base import BaseCommand, CommandError

from apps.kern.masterpasswort import UMGEBUNG

MINDESTLAENGE = 12


class Command(BaseCommand):
    help = "Erzeugt den Hash des Masterpassworts für die .env (Löschen von Mandanten)."

    def handle(self, *args, **opt):
        erstes = getpass.getpass("Masterpasswort: ")
        if len(erstes) < MINDESTLAENGE:
            raise CommandError(f"Mindestens {MINDESTLAENGE} Zeichen.")
        if getpass.getpass("Wiederholen: ") != erstes:
            raise CommandError("Die Eingaben stimmen nicht überein.")
        self.stdout.write("Diese Zeile in die .env eintragen (einfache Anführungszeichen wegen $), dann wwok neu starten:")
        self.stdout.write(f"{UMGEBUNG}='{make_password(erstes)}'")
