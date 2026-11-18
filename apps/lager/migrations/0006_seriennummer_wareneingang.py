"""Seriennummer merkt sich den Wareneingang (und damit den Lieferanten) für Reklamationen.

Nur die Spalte; der Altbestand wird in 0007 nachgetragen (getrennt, weil MariaDB eine Spaltenänderung nicht zurückrollt).
"""
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('einkauf', '0021_status_nicht_abgeschlossen'),
        ('lager', '0005_ek_herkunft_wareneingang'),
    ]

    operations = [
        migrations.AddField(
            model_name='seriennummer',
            name='wareneingang',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='seriennummern_im_eingang', to='einkauf.wareneingang', verbose_name='Wareneingang (Herkunft)'),
        ),
    ]
