from django.db import migrations, models


def hoechste(nummern) -> int:
    return max((int(n) for n in nummern if n.isdigit() and len(n) <= 18), default=0)


def vorbelegen(apps, schema_editor):
    """Die Zähler starten beim bisherigen Stand, damit die Nummern nahtlos weiterlaufen."""
    Stammnummer = apps.get_model("stammdaten", "Stammnummer")
    Artikel = apps.get_model("stammdaten", "Artikel")
    for typ in ("A", "L", "G"):
        Stammnummer.objects.create(art=typ, letzte_nummer=hoechste(Artikel.objects.filter(typ=typ).values_list("nummer", flat=True)))
    for art, modell in (("K", "Kunde"), ("LF", "Lieferant")):
        Stammnummer.objects.create(art=art, letzte_nummer=hoechste(apps.get_model("stammdaten", modell).objects.values_list("nummer", flat=True)))


class Migration(migrations.Migration):

    dependencies = [
        ("stammdaten", "0019_ust_id_pruefung"),
    ]

    operations = [
        migrations.CreateModel(
            name="Stammnummer",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("art", models.CharField(choices=[("A", "Artikel (Typ A)"), ("L", "Leistungen (Typ L)"), ("G", "Gruppenartikel (Typ G)"), ("K", "Kunden"), ("LF", "Lieferanten")], max_length=2, unique=True, verbose_name="Art")),
                ("letzte_nummer", models.PositiveBigIntegerField(default=0, verbose_name="zuletzt vergeben")),
            ],
            options={"verbose_name": "Stammnummer", "verbose_name_plural": "Stammnummern"},
        ),
        migrations.RunPython(vorbelegen, migrations.RunPython.noop),
    ]
