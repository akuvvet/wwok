from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("shop", "0007_shopbestellung_zahlung_und_storno_status"),
    ]

    operations = [
        migrations.AddField(
            model_name="shopartikel",
            name="kategorie_ids",
            field=models.JSONField(blank=True, default=list, help_text="Alle von wwok zugeordneten Kategorien im Shop (Haupt- und weitere)"),
        ),
    ]
