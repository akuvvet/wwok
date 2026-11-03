"""VK-Kalkulation mit mehreren EK-Quellen (Issue 12, Schritt 1): EK-Felder und Festpreis-Kennzeichen pro Mandant.
Der bisherige „EK-Preis netto“ (ek_manuell) wird als EK2 in beide Mandanten kopiert. Der Artikelwert bleibt unverändert, es gehen keine Daten verloren.
Bestehende feste VK (vk1–vk3) werden je Mandant auf „fest“ gesetzt, damit sich kein Preis ungewollt ändert."""
import django.core.validators
from django.db import migrations, models
from django.utils import timezone


def ek2_und_vk_fix_uebernehmen(apps, schema_editor):
    Artikel = apps.get_model("stammdaten", "Artikel")
    ArtikelMandant = apps.get_model("stammdaten", "ArtikelMandant")
    Mandant = apps.get_model("kern", "Mandant")
    mandanten = list(Mandant.objects.all())
    vorhanden = {(a, m) for a, m in ArtikelMandant.objects.values_list("artikel_id", "mandant_id")}
    jetzt = timezone.now()
    neu = []
    for a in Artikel.objects.filter(typ="A").only("pk", "ek_manuell", "vk1", "vk2", "vk3", "ek_geaendert_am"):
        if a.ek_manuell is None and a.vk1 is None and a.vk2 is None and a.vk3 is None:
            continue
        for m in mandanten:
            if (a.pk, m.pk) in vorhanden:
                continue
            neu.append(ArtikelMandant(artikel_id=a.pk, mandant_id=m.pk))
    ArtikelMandant.objects.bulk_create(neu, batch_size=500)
    for a in Artikel.objects.filter(typ="A").only("pk", "ek_manuell", "vk1", "vk2", "vk3", "ek_geaendert_am"):
        felder = {"vk_fix1": a.vk1 is not None, "vk_fix2": a.vk2 is not None, "vk_fix3": a.vk3 is not None}
        if a.ek_manuell is not None:
            felder.update(ek2=a.ek_manuell, ek2_datum=a.ek_geaendert_am or jetzt)
        if any(felder.values()):
            ArtikelMandant.objects.filter(artikel_id=a.pk).update(**felder)


class Migration(migrations.Migration):

    dependencies = [
        ('kern', '0001_initial'),
        ('stammdaten', '0023_import_ek_massgebend'),
    ]

    operations = [
        migrations.AddField(model_name='artikelmandant', name='ek_we',
                            field=models.DecimalField(blank=True, decimal_places=4, editable=False, max_digits=12, null=True, verbose_name='EK letzter Wareneingang')),
        migrations.AddField(model_name='artikelmandant', name='ek_avg',
                            field=models.DecimalField(blank=True, decimal_places=4, editable=False, max_digits=12, null=True, verbose_name='Ø-EK (gleitend)')),
        migrations.AddField(model_name='artikelmandant', name='ek2',
                            field=models.DecimalField(blank=True, decimal_places=4, max_digits=12, null=True, validators=[django.core.validators.MinValueValidator(0)], verbose_name='EK2 (manuell)')),
        migrations.AddField(model_name='artikelmandant', name='ek2_datum',
                            field=models.DateTimeField(blank=True, editable=False, null=True, verbose_name='EK2 geändert am')),
        migrations.AddField(model_name='artikelmandant', name='ek2_fix',
                            field=models.BooleanField(default=False, verbose_name='EK2 gilt immer als Kalkulationsbasis')),
        migrations.AddField(model_name='artikelmandant', name='vk_fix1',
                            field=models.BooleanField(default=False, verbose_name='VK1 fest')),
        migrations.AddField(model_name='artikelmandant', name='vk_fix2',
                            field=models.BooleanField(default=False, verbose_name='VK2 fest')),
        migrations.AddField(model_name='artikelmandant', name='vk_fix3',
                            field=models.BooleanField(default=False, verbose_name='VK3 fest')),
        migrations.AddField(model_name='artikelmandant', name='warnungen_aus',
                            field=models.BooleanField(default=False, verbose_name='keine Warnungen im Preis-Radar')),
        migrations.AddField(model_name='preisvorgabe', name='rundung_99',
                            field=models.BooleanField(default=False, help_text='Aus: kaufmännisch auf 2 Nachkommastellen.', verbose_name='VK auf ,99 aufrunden')),
        migrations.RunPython(ek2_und_vk_fix_uebernehmen, migrations.RunPython.noop),
    ]
