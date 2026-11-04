"""Issue 12, Schritt 2: Import-Quarantäne (Status, zurückgehaltene Datei) und Prüfliste für Einzelpreise. Löscht keine Daten."""
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("einkauf", "0014_bezeichnung_bestellung"),
        ("stammdaten", "0025_preisvorgabe_schutzgrenzen"),
    ]

    operations = [
        migrations.AddField(
            model_name="preislistenimport", name="status",
            field=models.CharField(choices=[("uebernommen", "übernommen"), ("zur_pruefung", "zur Prüfung"), ("verworfen", "verworfen")],
                                   db_index=True, default="uebernommen", max_length=12),
        ),
        migrations.AddField(
            model_name="preislistenimport", name="datei",
            field=models.BinaryField(blank=True, editable=False, null=True, verbose_name="zurückgehaltene Datei",
                                     help_text="Nur bei Status „zur Prüfung“; wird nach Freigabe oder Verwerfen gelöscht."),
        ),
        migrations.AddField(
            model_name="preislistenimport", name="zur_pruefung",
            field=models.PositiveIntegerField(default=0, verbose_name="Preise zur Prüfung"),
        ),
        migrations.CreateModel(
            name="Preispruefung",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("bestellnummer", models.CharField(max_length=100)),
                ("ek_alt", models.DecimalField(decimal_places=4, max_digits=12)),
                ("ek_neu", models.DecimalField(decimal_places=4, max_digits=12)),
                ("status", models.CharField(choices=[("offen", "offen"), ("uebernommen", "übernommen"), ("verworfen", "verworfen")],
                                            db_index=True, default="offen", max_length=10)),
                ("bearbeitet_am", models.DateTimeField(blank=True, null=True)),
                ("artikel", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="preispruefungen", to="stammdaten.artikel")),
                ("importlauf", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="pruefungen", to="einkauf.preislistenimport")),
                ("lieferant", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="preispruefungen", to="stammdaten.lieferant")),
            ],
            options={"verbose_name": "Preisprüfung", "verbose_name_plural": "Preisprüfungen",
                     "ordering": ["-importlauf__zeitpunkt", "artikel__nummer"]},
        ),
    ]
