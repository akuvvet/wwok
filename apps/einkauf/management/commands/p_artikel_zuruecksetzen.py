from django.core.management.base import BaseCommand
from django.db.models import Exists, OuterRef

from apps.stammdaten.models import Artikel, ArtikelLieferant


class Command(BaseCommand):
    help = ("Entfernt Lieferantenprodukte (Typ P), die aus Preislisten angelegt, aber nirgends verwendet wurden "
            "(kein Beleg, keine Bestellung, kein Lager, keine Gruppe, keine Sonderpreise usw.), samt ihren Lieferantenpreisen. "
            "Ohne --loeschen wird nur gezählt. Danach kann die Preisliste neu eingelesen werden (z. B. mit anderer Nummerierung).")

    def add_arguments(self, parser):
        parser.add_argument("--loeschen", action="store_true", help="wirklich löschen (sonst nur anzeigen)")
        parser.add_argument("--lieferant", help="nur Artikel dieses Lieferanten (Lieferantennummer)")

    def handle(self, loeschen=False, lieferant=None, **opts):
        kandidaten = Artikel.objects.filter(typ="P")
        if lieferant:
            kandidaten = kandidaten.filter(pk__in=ArtikelLieferant.objects.filter(lieferant__nummer=lieferant).values("artikel_id"))
        for beziehung in Artikel._meta.related_objects:
            if beziehung.related_model is ArtikelLieferant or beziehung.many_to_many:
                continue  # Lieferantenpreise gehören zum Artikel und werden mitgelöscht
            modell, feld = beziehung.related_model, beziehung.field.name
            kandidaten = kandidaten.exclude(Exists(modell.objects.filter(**{feld: OuterRef("pk")})))
        ids = list(kandidaten.values_list("pk", flat=True))
        gesamt_p = Artikel.objects.filter(typ="P").count()
        self.stdout.write(f"{gesamt_p} Lieferantenprodukte (Typ P) insgesamt, davon {len(ids)} unbenutzt und entfernbar.")
        if not loeschen:
            self.stdout.write("Es wurde nichts gelöscht. Zum Löschen den Befehl mit --loeschen wiederholen.")
            return
        for i in range(0, len(ids), 2000):  # in Blöcken, ohne Einzel-Signale (die Preisliste wird ohnehin neu eingelesen)
            block = ids[i:i + 2000]
            ArtikelLieferant.objects.filter(artikel_id__in=block)._raw_delete(using="default")
            Artikel.objects.filter(pk__in=block)._raw_delete(using="default")
        self.stdout.write(f"{len(ids)} Artikel samt Lieferantenpreisen gelöscht. Preisliste jetzt neu einlesen.")
