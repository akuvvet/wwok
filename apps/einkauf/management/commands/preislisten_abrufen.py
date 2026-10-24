from django.core.management.base import BaseCommand

from apps.einkauf import preislisten
from apps.einkauf.models import Preislistenprofil


class Command(BaseCommand):
    help = ("Ruft die Preislisten ab (FTP, SFTP, URL), deren Intervall abgelaufen ist, und importiert sie. "
            "Gedacht für einen stündlichen Cron-Aufruf; mit --alle werden alle aktiven Profile sofort abgerufen.")

    def add_arguments(self, parser):
        parser.add_argument("--alle", action="store_true", help="Intervall ignorieren")

    def handle(self, alle=False, **opts):
        profile = list(Preislistenprofil.objects.filter(aktiv=True).exclude(quelle="upload")) if alle \
            else list(preislisten.faellige_profile())
        for profil in profile:
            try:
                dateiname, inhalt = preislisten.abrufen(profil)
                protokoll = preislisten.importieren(profil, inhalt, dateiname)
                self.stdout.write(f"{profil}: {protokoll.meldung}")
            except preislisten.PreislistenFehler as fehler:
                self.stderr.write(f"{profil}: {fehler}")
        if not profile:
            self.stdout.write("Keine Preisliste fällig.")
