"""Programmvermerke („Bestellvorschlag“, „Aus Lieferantenvergleich“, „Aus Angebot AN…“, „Rest nicht mehr lieferbar …“) standen im
Text für den Lieferanten und damit auf dem Bestell-PDF. Sie gehören in die interne Notiz (Wunsch des Nutzers 20.11.2026).
Es wird nichts gelöscht: die erkannten Zeilen wandern aus bemerkung in notiz, alles andere bleibt, wo es ist."""
import re

from django.db import migrations

VERMERK = re.compile(r"^(Bestellvorschlag|Aus Lieferantenvergleich|Aus [A-Za-zÄÖÜäöüß]+ [A-Z]{2,3}\d{4}-\d+.*|Rest nicht mehr lieferbar \(.*)$")


def verschieben(apps, schema_editor):
    Bestellung = apps.get_model("einkauf", "Bestellung")
    for b in Bestellung.objects.exclude(bemerkung="").only("pk", "bemerkung", "notiz").iterator():
        zeilen = b.bemerkung.split("\n")
        vermerke = [z for z in zeilen if VERMERK.match(z.strip())]
        if not vermerke:
            continue
        rest = "\n".join(z for z in zeilen if not VERMERK.match(z.strip())).strip()
        notiz = "\n".join(vermerke + ([b.notiz] if b.notiz else []))
        Bestellung.objects.filter(pk=b.pk).update(bemerkung=rest, notiz=notiz)


class Migration(migrations.Migration):
    dependencies = [("einkauf", "0026_bestellung_ansprechpartner")]
    operations = [migrations.RunPython(verschieben, migrations.RunPython.noop)]
