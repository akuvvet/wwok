from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("stammdaten", "0026_preis_radar"),
    ]

    operations = [
        migrations.AddField(
            model_name="preislistenartikel",
            name="laenge_cm",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=8, null=True, verbose_name="Länge (cm)"),
        ),
        migrations.AddField(
            model_name="preislistenartikel",
            name="breite_cm",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=8, null=True, verbose_name="Breite (cm)"),
        ),
        migrations.AddField(
            model_name="preislistenartikel",
            name="hoehe_cm",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=8, null=True, verbose_name="Höhe (cm)"),
        ),
    ]
