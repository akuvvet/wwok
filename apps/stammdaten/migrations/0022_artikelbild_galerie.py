import apps.stammdaten.models
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('stammdaten', '0021_artikel_ek_manuell'),
    ]

    operations = [
        migrations.AddField(
            model_name='artikelbild',
            name='alt_text',
            field=models.CharField(blank=True, help_text='Beschreibung für Shop und Suchmaschinen.', max_length=150, verbose_name='Alt-Text'),
        ),
        migrations.AddField(
            model_name='artikelbild',
            name='vorschau',
            field=models.ImageField(blank=True, editable=False, max_length=255, upload_to=apps.stammdaten.models.vorschau_pfad, verbose_name='Vorschaubild'),
        ),
        migrations.AlterField(
            model_name='artikelbild',
            name='bild',
            field=models.ImageField(max_length=255, upload_to=apps.stammdaten.models.bild_pfad, verbose_name='Bild'),
        ),
    ]
