"""Artikel mit gebuchtem Wareneingang bekommen die EK-Herkunft „Wareneingang“ (vor Einführung der Herkunft gebucht)."""
from django.db import migrations


def herkunft_setzen(apps, schema_editor):
    Artikel = apps.get_model("stammdaten", "Artikel")
    Lagerbewegung = apps.get_model("lager", "Lagerbewegung")
    ids = Lagerbewegung.objects.filter(art="WE").values("artikel_id")
    Artikel.objects.filter(pk__in=ids, ek_herkunft="").update(ek_herkunft="wareneingang")


class Migration(migrations.Migration):
    dependencies = [
        ("lager", "0004_reparaturbestand"),
        ("stammdaten", "0021_artikel_ek_manuell"),
    ]

    operations = [migrations.RunPython(herkunft_setzen, migrations.RunPython.noop)]
