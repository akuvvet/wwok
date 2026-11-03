"""Der Import-EK ist maßgebend (README 18): Artikel, deren EK aus dem Wareneingang bzw. als Ø-EK aus Mention stammt, bekommen
einmalig den günstigsten EK aus den Lieferantenpreislisten. Ohne Preisliste bleibt der bisherige EK bis zum nächsten Import."""
import django.core.validators
from django.db import migrations, models
from django.utils import timezone


def auf_preisliste_umstellen(apps, schema_editor):
    Artikel = apps.get_model("stammdaten", "Artikel")
    ArtikelLieferant = apps.get_model("stammdaten", "ArtikelLieferant")
    # nur Artikel mit einem Ø-EK; ohne ihn folgt der EK ohnehin direkt der Preisliste
    kandidaten = set(Artikel.objects.filter(typ="A", ek_herkunft__in=["wareneingang", ""], durchschnitts_ek__isnull=False)
                     .values_list("pk", flat=True))
    best = {}
    for al in ArtikelLieferant.objects.filter(artikel_id__in=kandidaten, ek__gt=0).order_by("artikel_id", "ek").only("artikel_id", "ek"):
        best.setdefault(al.artikel_id, al.ek)  # nach EK sortiert: der erste je Artikel ist der günstigste
    jetzt, geaendert = timezone.now(), []
    for a in Artikel.objects.filter(pk__in=list(best)):
        a.durchschnitts_ek, a.ek_herkunft, a.ek_geaendert_am = best[a.pk], "import", jetzt
        geaendert.append(a)
    Artikel.objects.bulk_update(geaendert, ["durchschnitts_ek", "ek_herkunft", "ek_geaendert_am"], batch_size=500)


class Migration(migrations.Migration):

    dependencies = [
        ('stammdaten', '0022_artikelbild_galerie'),
        ('lager', '0005_ek_herkunft_wareneingang'),  # setzt vorher die Herkunft „Wareneingang“ bei Altdaten
    ]

    operations = [
        migrations.AlterField(
            model_name='artikel',
            name='durchschnitts_ek',
            field=models.DecimalField(blank=True, decimal_places=4, editable=False, max_digits=12, null=True, verbose_name='EK netto (Import)'),
        ),
        migrations.AlterField(
            model_name='artikel',
            name='ek_manuell',
            field=models.DecimalField(blank=True, decimal_places=2, help_text='Basis, solange es keinen Import-EK aus einer Preisliste gibt.', max_digits=12, null=True, validators=[django.core.validators.MinValueValidator(0)], verbose_name='EK-Preis netto'),
        ),
        migrations.RunPython(auf_preisliste_umstellen, migrations.RunPython.noop),
    ]
