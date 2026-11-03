"""Issue 12, Schritt 3: Einstellungen des Preis-Radars und Tabelle „Gesehen“. Nur neue Spalten bzw. Tabelle, keine Datenänderung."""
from decimal import Decimal

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("kern", "0001_initial"),
        ("stammdaten", "0025_preisvorgabe_schutzgrenzen"),
    ]

    operations = [
        migrations.AddField(
            model_name="preisvorgabe", name="radar_preisverfall_faktor",
            field=models.DecimalField(decimal_places=2, default=Decimal("0.85"), max_digits=5, verbose_name="Radar: Preisverfall-Faktor",
                                      help_text="Preisverfall, wenn der Import-EK unter Ø-EK × diesem Faktor liegt."),
        ),
        migrations.AddField(
            model_name="preisvorgabe", name="radar_ek2_abweichung",
            field=models.DecimalField(decimal_places=2, default=20, max_digits=6, verbose_name="Radar: EK2-Abweichung %",
                                      help_text="Hinweis, wenn Import-EK bzw. Wareneingang um mehr als das von EK2 abweichen."),
        ),
        migrations.AddField(
            model_name="preisvorgabe", name="radar_ek2_alter_tage",
            field=models.PositiveIntegerField(default=180, verbose_name="Radar: EK2 prüfen nach Tagen"),
        ),
        migrations.AddField(
            model_name="preisvorgabe", name="radar_ignorieren_prozent",
            field=models.DecimalField(decimal_places=2, default=3, max_digits=6, verbose_name="Radar: Ignorieren unter %",
                                      help_text="Differenzen unter diesem Prozentsatz der Vergleichsbasis werden nicht gemeldet."),
        ),
        migrations.AddField(
            model_name="preisvorgabe", name="radar_ignorieren_euro",
            field=models.DecimalField(decimal_places=2, default=Decimal("0.50"), max_digits=8, verbose_name="Radar: Ignorieren unter €",
                                      help_text="Differenzen je Stück unter diesem Betrag werden nicht gemeldet."),
        ),
        migrations.CreateModel(
            name="RadarGesehen",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("ek_kalk", models.DecimalField(blank=True, decimal_places=4, max_digits=12, null=True)),
                ("ek_avg", models.DecimalField(blank=True, decimal_places=4, max_digits=12, null=True)),
                ("vk", models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True)),
                ("gesehen_am", models.DateTimeField(auto_now=True)),
                ("artikel", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="radar_gesehen", to="stammdaten.artikel")),
                ("mandant", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to="kern.mandant")),
            ],
            options={"verbose_name": "Radar: gesehen", "verbose_name_plural": "Radar: gesehen"},
        ),
        migrations.AddConstraint(
            model_name="radargesehen",
            constraint=models.UniqueConstraint(fields=("artikel", "mandant"), name="radar_gesehen_eindeutig"),
        ),
    ]
