from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.verkauf.models import faellige_abos


class Command(BaseCommand):
    help = ("Legt zu allen fälligen wiederkehrenden Rechnungen eine Bestellung an (täglicher Cron-Aufruf). "
            "Die Bestellung hat sofort eine Nummer und wird nie automatisch verworfen; daraus entsteht von Hand die Rechnung.")

    def handle(self, **opts):
        anzahl = 0
        for abo in list(faellige_abos()):
            try:  # je Lauf höchstens eine Rechnung je Vertrag; versäumte Termine holt der nächste Lauf nach
                beleg = abo.rechnung_erzeugen()
            except Exception as fehler:  # ein fehlerhaftes Abo darf die übrigen nicht aufhalten
                self.stderr.write(f"{abo}: {fehler}")
                continue
            if beleg is None:  # Stichtag ohne fällige Position, nur der Termin ist weitergerückt
                continue
            anzahl += 1
            self.stdout.write(f"{abo}: Bestellung {beleg.nummer} für {abo.kunde.name1} angelegt (Beleg {beleg.pk})")
        if not anzahl:
            self.stdout.write("Keine wiederkehrende Rechnung fällig.")
