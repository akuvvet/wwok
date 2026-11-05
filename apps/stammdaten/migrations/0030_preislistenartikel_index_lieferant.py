from django.db import migrations, models


class Migration(migrations.Migration):
    """Index (lieferant, bezeichnung) für Sortierung/Lieferantenfilter der Preislistenartikel; verändert keine Daten."""

    dependencies = [
        ("stammdaten", "0029_kunde_anrede"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="preislistenartikel",
            options={"ordering": ["lieferant_id", "bezeichnung"], "verbose_name": "Preislistenartikel",
                     "verbose_name_plural": "Preislistenartikel"},
        ),
        migrations.AddIndex(
            model_name="preislistenartikel",
            index=models.Index(fields=["lieferant", "bezeichnung"], name="pla_lieferant_bez_idx"),
        ),
    ]
