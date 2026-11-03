"""Issue 12, Schritt 2: einstellbare Schutzgrenzen für Wareneingang und Preisimport. Nur neue Spalten mit Standardwerten."""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("stammdaten", "0024_vk_kalkulation_ek_quellen"),
    ]

    operations = [
        migrations.AddField(
            model_name="preisvorgabe", name="we_abweichung_prozent",
            field=models.DecimalField(decimal_places=2, default=50, max_digits=6, verbose_name="Wareneingang: Rückfrage ab Abweichung %",
                                      help_text="Weicht der Wareneingangspreis um mehr als das vom Ø-EK bzw. Import-EK ab, kommt vor dem Buchen eine Rückfrage."),
        ),
        migrations.AddField(
            model_name="preisvorgabe", name="import_quarantaene_anteil",
            field=models.DecimalField(decimal_places=2, default=20, max_digits=6, verbose_name="Import-Quarantäne: Anteil Artikel %",
                                      help_text="Ändern sich mehr als so viele Artikel eines Imports …"),
        ),
        migrations.AddField(
            model_name="preisvorgabe", name="import_quarantaene_aenderung",
            field=models.DecimalField(decimal_places=2, default=30, max_digits=6, verbose_name="Import-Quarantäne: Preisänderung %",
                                      help_text="… um mehr als diesen Wert, wird der gesamte Import zurückgehalten."),
        ),
        migrations.AddField(
            model_name="preisvorgabe", name="import_einzel_prozent",
            field=models.DecimalField(decimal_places=2, default=50, max_digits=6, verbose_name="Import: Einzelartikel zur Prüfung ab %",
                                      help_text="Weicht ein Preis um mehr als das vom bisherigen Preis ab, wird er nicht übernommen, sondern in die Prüfliste gestellt."),
        ),
    ]
