"""Bestehende Seriennummern bekommen ihren Wareneingang: über die gebuchte Wareneingangsposition, in der die Nummer erfasst wurde.

Löscht nichts und lässt sich gefahrlos erneut ausführen (füllt nur leere Einträge).
"""
from django.db import migrations


def herkunft_nachtragen(apps, schema_editor):
    Seriennummer = apps.get_model("lager", "Seriennummer")
    WareneingangPosition = apps.get_model("einkauf", "WareneingangPosition")
    for sn in Seriennummer.objects.filter(wareneingang__isnull=True).iterator():
        kandidaten = WareneingangPosition.objects.filter(
            artikel_id=sn.artikel_id, wareneingang__gebucht=True, seriennummern__contains=sn.nummer
        ).select_related("wareneingang").order_by("-wareneingang__datum", "-id")
        treffer = [p.wareneingang for p in kandidaten
                   if sn.nummer in [z.strip() for z in p.seriennummern.replace(",", "\n").splitlines()]]
        if not treffer:
            continue
        eigene = [we for we in treffer if we.mandant_id == sn.mandant_id]  # umgebuchte Nummern stammen aus dem anderen Mandanten
        sn.wareneingang_id = (eigene or treffer)[0].pk
        sn.save(update_fields=["wareneingang"])


class Migration(migrations.Migration):

    dependencies = [
        ("lager", "0006_seriennummer_wareneingang"),
        ("einkauf", "0021_status_nicht_abgeschlossen"),
    ]

    operations = [migrations.RunPython(herkunft_nachtragen, migrations.RunPython.noop)]
