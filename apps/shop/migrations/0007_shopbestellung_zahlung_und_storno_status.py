from django.db import migrations, models


def bisherige_zahlungen_merken(apps, schema_editor):
    """Bestellungen, auf die schon eine Zahlung gebucht wurde (z. B. beim Import als bezahlt), gelten als gebucht,
    damit das Nachziehen nicht ein zweites Mal bucht. Es wird nichts gelöscht."""
    ShopBestellung = apps.get_model("shop", "ShopBestellung")
    Zahlung = apps.get_model("finanzen", "Zahlung")
    gebucht = set(Zahlung.objects.filter(beleg__in=ShopBestellung.objects.values("beleg")).values_list("beleg_id", flat=True))
    ShopBestellung.objects.filter(beleg_id__in=gebucht).update(zahlung_gebucht=True)


class Migration(migrations.Migration):

    dependencies = [
        ("shop", "0006_shopkategorie_angelegt"),
        ("finanzen", "0011_lastschrift_zahlung"),
    ]

    operations = [
        migrations.AddField(
            model_name="shopbestellung",
            name="zahlung_gebucht",
            field=models.BooleanField(default=False, help_text="Die Zahlung wird genau einmal gebucht, sobald der Shop die Bestellung als bezahlt meldet.", verbose_name="Zahlung aus dem Shop gebucht"),
        ),
        migrations.AddField(
            model_name="shopbestellung",
            name="im_shop_storniert_am",
            field=models.DateTimeField(blank=True, help_text="Nur ein Hinweis: In wwok wird nichts automatisch storniert.", null=True, verbose_name="im Shop storniert am"),
        ),
        migrations.RunPython(bisherige_zahlungen_merken, migrations.RunPython.noop),
    ]
