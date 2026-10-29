from django.core.management.base import BaseCommand, CommandError

from apps.kern.models import Mandant
from apps.verkauf import internetmarke as im


class Command(BaseCommand):
    help = "Prüft den INTERNETMARKE-Zugang eines Mandanten (Anmeldung, Guthaben, Druckformat, Produktcodes) – ohne Kauf."

    def add_arguments(self, parser):
        parser.add_argument("mandant", help="Kurzname, z. B. OKAY")

    def handle(self, *args, **optionen):
        mandant = Mandant.objects.filter(kurzname__iexact=optionen["mandant"]).first()
        if mandant is None:
            raise CommandError(f"Mandant {optionen['mandant']} nicht gefunden.")
        try:
            for zeile in im.pruefen(mandant):
                self.stdout.write(zeile)
        except im.PostFehler as fehler:
            raise CommandError(str(fehler))
