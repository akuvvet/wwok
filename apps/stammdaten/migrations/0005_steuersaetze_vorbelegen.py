import datetime
from decimal import Decimal

from django.db import migrations

SAETZE = [
    (1, "Inland Mehrwertsteuer 19%", Decimal("19"), datetime.date(2020, 1, 1)),
    (2, "Inland Mehrwertsteuer 7%", Decimal("7"), datetime.date(2021, 1, 1)),
]


def vorbelegen(apps, schema_editor):
    Steuersatz = apps.get_model("stammdaten", "Steuersatz")
    for nummer, bezeichnung, prozent, ab in SAETZE:
        Steuersatz.objects.update_or_create(
            nummer=nummer, defaults={"bezeichnung": bezeichnung, "prozent": prozent, "gueltig_ab": ab})


class Migration(migrations.Migration):
    dependencies = [("stammdaten", "0004_steuersatz_id_gueltig_ab")]
    operations = [migrations.RunPython(vorbelegen, migrations.RunPython.noop)]
