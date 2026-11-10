from django.core.management.base import BaseCommand, CommandError

from apps.kern.models import Mandant
from apps.verkauf.models import VORLAEUFIG_MINUTEN, Beleg


class Command(BaseCommand):
    help = (f"Verwirft vorläufige (nicht gespeicherte) Belege, deren Maske geschlossen wurde oder seit {VORLAEUFIG_MINUTEN} Minuten kein Lebenszeichen sendet: "
            "alles wird zurückgenommen (Mengen in der Bestellung, Umwandlungen, Angebote für Preislistenartikel). Es entstehen keine "
            "Nummernlücken, weil vorläufige Belege keine Nummer haben. Standard ist der Probelauf; verworfen wird nur mit --ausfuehren. "
            "Shop-, Abo- und Werkstattbelege sowie Altentwürfe werden nie angefasst. Cron: jede Minute.")

    def add_arguments(self, parser):
        parser.add_argument("--mandant", help="Kurzname des Mandanten (z. B. OKAY); ohne Angabe alle")
        parser.add_argument("--ausfuehren", action="store_true", help="Wirklich verwerfen (sonst nur Probelauf)")

    def handle(self, *args, **optionen):
        mandant = None
        if optionen["mandant"]:
            mandant = Mandant.objects.filter(kurzname__iexact=optionen["mandant"]).first()
            if mandant is None:
                raise CommandError(f"Mandant {optionen['mandant']} nicht gefunden.")
        ausfuehren = optionen["ausfuehren"]
        self.stdout.write("AUSFÜHREN" if ausfuehren else "PROBELAUF (es wird nichts geändert; zum Verwerfen --ausfuehren)")
        verworfen = fehler = 0
        liste = Beleg.vorlaeufige_verwerfen(mandant, ausfuehren=ausfuehren)
        for eintrag in liste:
            b = eintrag["beleg"]
            if eintrag["fehler"]:
                self.stderr.write(f"{b} [{b.mandant}] {b.kunde.name1} ({eintrag['weg']}): nicht verworfen – {eintrag['fehler']}")
                fehler += 1
                continue
            status = "verworfen" if eintrag["verworfen"] else ("würde verworfen" if not ausfuehren else "inzwischen aktiv oder gespeichert, übersprungen")
            self.stdout.write(f"{b} [{b.mandant}] {b.kunde.name1} ({eintrag['weg']}): {status}")
            verworfen += bool(eintrag["verworfen"]) or not ausfuehren
        self.stdout.write(f"{verworfen} {'verworfen' if ausfuehren else 'zu verwerfen'}, {fehler} Fehler.")
