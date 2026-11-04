from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("einkauf", "0018_status_laengen"),
    ]

    operations = [
        migrations.AlterField(
            model_name="preislistenprofil",
            name="sp_gewicht",
            field=models.CharField(blank=True, max_length=60, verbose_name="Spalte Gewicht (kg; g wird erkannt)"),
        ),
        migrations.AddField(
            model_name="preislistenprofil",
            name="sp_laenge",
            field=models.CharField(blank=True, max_length=60, verbose_name="Spalte Länge (cm; mm/m werden erkannt)"),
        ),
        migrations.AddField(
            model_name="preislistenprofil",
            name="sp_breite",
            field=models.CharField(blank=True, max_length=60, verbose_name="Spalte Breite (cm; mm/m werden erkannt)"),
        ),
        migrations.AddField(
            model_name="preislistenprofil",
            name="sp_hoehe",
            field=models.CharField(blank=True, max_length=60, verbose_name="Spalte Höhe (cm; mm/m werden erkannt)"),
        ),
    ]
