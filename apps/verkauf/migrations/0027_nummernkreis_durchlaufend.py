from datetime import date

from django.db import migrations, models


def zusammenfuehren(apps, schema_editor):
    """Je Mandant und Art bleibt ein Zähler mit dem höchsten Stand aller Jahre, damit keine Nummer doppelt vergeben wird."""
    Nummernkreis = apps.get_model("verkauf", "Nummernkreis")
    behalten = {}
    for kreis in Nummernkreis.objects.order_by("-letzte_nummer", "-jahr", "pk"):
        schluessel = (kreis.mandant_id, kreis.art)
        if schluessel in behalten:
            kreis.delete()
        else:
            behalten[schluessel] = kreis


def jahr_zurueck(apps, schema_editor):
    """Rückweg: Der Zähler gilt wieder für das laufende Jahr, sonst finge die alte Vergabe bei 1 an und Nummern kämen doppelt."""
    apps.get_model("verkauf", "Nummernkreis").objects.update(jahr=date.today().year)


class Migration(migrations.Migration):

    dependencies = [
        ("verkauf", "0026_bezeichnung_bestellung"),
    ]

    # Die neue Eindeutigkeit entsteht vor dem Entfernen der alten: MariaDB braucht für den Mandanten-Fremdschlüssel durchgehend einen Index
    operations = [
        migrations.RunPython(zusammenfuehren, jahr_zurueck),
        migrations.AddConstraint(
            model_name="nummernkreis",
            constraint=models.UniqueConstraint(fields=("mandant", "art"), name="nummernkreis_je_art"),
        ),
        migrations.RemoveConstraint(
            model_name="nummernkreis",
            name="nummernkreis_eindeutig",
        ),
        migrations.AlterField(
            model_name="nummernkreis",
            name="jahr",
            field=models.PositiveSmallIntegerField(default=0, verbose_name="Jahr"),
        ),
        migrations.RemoveField(
            model_name="nummernkreis",
            name="jahr",
        ),
        migrations.AddField(
            model_name="nummernkreis",
            name="letzte_vergabe",
            field=models.DateField(blank=True, editable=False, null=True, verbose_name="zuletzt vergeben am"),
        ),
        migrations.AlterModelOptions(
            name="nummernkreis",
            options={"verbose_name": "Laufende Nummer", "verbose_name_plural": "Laufende Nummern"},
        ),
    ]
