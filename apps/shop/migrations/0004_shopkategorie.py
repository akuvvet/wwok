import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("shop", "0003_kundenpreis"),
        ("stammdaten", "0028_kategorie_online"),
    ]

    operations = [
        migrations.CreateModel(
            name="ShopKategorie",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("kategorie_id_extern", models.CharField(max_length=64)),
                ("im_shop_aktiv", models.BooleanField(default=True)),
                ("kategorie", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="shop_zuordnungen", to="stammdaten.kategorie")),
                ("shop", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="kategorien", to="shop.shop")),
            ],
            options={
                "verbose_name": "Shop-Kategorie",
                "verbose_name_plural": "Shop-Kategorien",
                "constraints": [models.UniqueConstraint(fields=("shop", "kategorie"), name="shopkategorie_eindeutig")],
            },
        ),
    ]
