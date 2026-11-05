from django.db import migrations, models


def bestehende_online(apps, schema_editor):
    """Bestehende Kategorien bleiben online, damit beim nächsten Abgleich nichts ungewollt aus dem Shop fällt."""
    apps.get_model("stammdaten", "Kategorie").objects.update(online=True)


class Migration(migrations.Migration):

    dependencies = [
        ("stammdaten", "0027_preislistenartikel_masse"),
    ]

    operations = [
        migrations.AddField(
            model_name="kategorie",
            name="online",
            field=models.BooleanField(
                default=False, verbose_name="online",
                help_text="Nur Online-Kategorien (und deren Artikel) sind im Shop sichtbar. "
                          "Unterkategorien sind nur online, wenn auch alle übergeordneten online sind."),
        ),
        migrations.RunPython(bestehende_online, migrations.RunPython.noop),
    ]
