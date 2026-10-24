from django.core.management.base import BaseCommand

from apps.shop import services
from apps.shop.models import Shop
from apps.shop.shopware import ShopFehler


class Command(BaseCommand):
    help = ("Gleicht alle aktiven Shops ab: neue Bestellungen holen und Bestände/Preise melden. "
            "Gedacht für einen Cron-Aufruf alle paar Minuten.")

    def add_arguments(self, parser):
        parser.add_argument("--nur-bestellungen", action="store_true")
        parser.add_argument("--nur-bestand", action="store_true")
        parser.add_argument("--alle", action="store_true", help="beim Bestandsabgleich alle Artikel senden, nicht nur Änderungen")

    def handle(self, nur_bestellungen=False, nur_bestand=False, alle=False, **opts):
        for shop in Shop.objects.filter(aktiv=True):
            try:
                if not nur_bestand:
                    e = services.bestellungen_abrufen(shop)
                    self.stdout.write(f"{shop}: {e['neu']} neue Bestellung(en), {e['bekannt']} bekannt" + (f", FEHLER: {e['fehler']}" if e["fehler"] else ""))
                if not nur_bestellungen:
                    e = services.abgleichen(shop, alle=alle)
                    self.stdout.write(f"{shop}: {e['gesendet']} Artikel gesendet")
            except ShopFehler as fehler:
                self.stderr.write(f"{shop}: {fehler}")
