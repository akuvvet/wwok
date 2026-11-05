from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('shop', '0004_shopkategorie'),
    ]

    operations = [
        migrations.AddIndex(
            model_name='syncprotokoll',
            index=models.Index(fields=['shop', '-zeitpunkt'], name='syncprotokoll_shop_zeit'),
        ),
    ]
