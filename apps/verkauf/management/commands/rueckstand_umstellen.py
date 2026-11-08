from django.core.management.base import BaseCommand, CommandError

from apps.kern.models import Mandant
from apps.verkauf import uebernahme


class Command(BaseCommand):
    help = ("Stellt offene Bestellungen mit alten Teilübernahmen (Lieferschein/Rechnung als Kopie) auf den Rest um: "
            "Die Bestellung behält nur die noch offene Menge. Standard ist der Probelauf; geändert wird nur mit --ausfuehren. "
            "Abgeschlossene Bestellungen und solche mit aktiver Proforma werden nur gemeldet. Nichts wird gelöscht; "
            "Positionen mit Rest 0 bleiben mit Menge 0 stehen.")

    def add_arguments(self, parser):
        parser.add_argument("--mandant", help="Kurzname des Mandanten (z. B. OKAY); ohne Angabe alle")
        parser.add_argument("--ausfuehren", action="store_true", help="Änderungen wirklich schreiben (sonst nur Probelauf)")

    def handle(self, *args, **optionen):
        mandant = None
        if optionen["mandant"]:
            mandant = Mandant.objects.filter(kurzname__iexact=optionen["mandant"]).first()
            if mandant is None:
                raise CommandError(f"Mandant {optionen['mandant']} nicht gefunden.")
        ausfuehren = optionen["ausfuehren"]
        self.stdout.write("AUSFÜHREN" if ausfuehren else "PROBELAUF (es wird nichts geändert; zum Umstellen --ausfuehren)")
        umgestellt = gemeldet = fehler = 0
        for fall in uebernahme.altfaelle(mandant):
            ab = fall["beleg"]
            if fall["status"] != "umstellen":
                grund = "abgeschlossen (beim Kunden versandt)" if fall["status"] == "abgeschlossen" else "aktive Proforma-Rechnung"
                self.stdout.write(f"{ab} [{ab.mandant}]: nur gemeldet, nicht umgestellt – {grund}.")
                gemeldet += 1
                continue
            try:
                plan = uebernahme.altfall_plan(ab)
                if ausfuehren:
                    plan = uebernahme.altfall_umstellen(ab)
            except ValueError as e:
                self.stderr.write(f"{ab} [{ab.mandant}]: nicht umgestellt – {e}")
                fehler += 1
                continue
            self.stdout.write(f"{ab} [{ab.mandant}]: {'umgestellt' if ausfuehren else 'würde umgestellt'}")
            for z in plan:
                ziele = ", ".join(sorted({f"{k.beleg.get_art_display()} {k.beleg.nummer or '(Entwurf)'}" for k in z["kopien"]}))
                self.stdout.write(f"    {z['position'].bezeichnung}: {uebernahme._menge_text(z['alt'])} -> {uebernahme._menge_text(z['neu'])} "
                                  f"({uebernahme._menge_text(z['uebernommen'])} übernommen in {ziele})")
            umgestellt += 1
        self.stdout.write(f"{umgestellt} {'umgestellt' if ausfuehren else 'umzustellen'}, {gemeldet} nur gemeldet, {fehler} Fehler.")
