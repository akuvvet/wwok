from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("stammdaten", "0028_kategorie_online"),
    ]

    operations = [
        migrations.AddField(
            model_name="kunde",
            name="anrede",
            field=models.CharField(
                blank=True,
                choices=[("Firma", "Firma"), ("Herr", "Herr"), ("Frau", "Frau")],
                max_length=10,
                verbose_name="Anrede",
            ),
        ),
    ]
