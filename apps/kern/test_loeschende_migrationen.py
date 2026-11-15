"""Prüfbericht B1/E8: Der Deploy hält an, wenn eine neue Migration Daten löschen würde."""
import importlib.util
import tempfile
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

_spec = importlib.util.spec_from_file_location("loeschende_migrationen", Path(settings.BASE_DIR) / "deploy" / "loeschende_migrationen.py")
pruefung = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pruefung)

KOPF = "from django.db import migrations, models\n\nclass Migration(migrations.Migration):\n    operations = [\n"


class LoeschendeMigrationenTest(SimpleTestCase):
    def test_harmlose_migration_geht_durch(self):
        text = KOPF + "        migrations.AddField(model_name='beleg', name='x', field=models.CharField(max_length=5)),\n" \
                      "        migrations.RunSQL('UPDATE verkauf_beleg SET x = 1'),\n    ]\n"
        self.assertEqual(pruefung.befunde(text), [])

    def test_spalte_tabelle_und_sql_werden_gemeldet(self):
        self.assertEqual(len(pruefung.befunde(KOPF + "        migrations.RemoveField(model_name='beleg', name='x'),\n    ]\n")), 1)
        self.assertEqual(len(pruefung.befunde(KOPF + "        migrations.DeleteModel(name='Alt'),\n    ]\n")), 1)
        self.assertEqual(len(pruefung.befunde(KOPF + "        migrations.RunSQL(\n            'drop table alt'),\n    ]\n")), 1)
        self.assertEqual(len(pruefung.befunde(KOPF + "        migrations.RunSQL('DELETE FROM alt WHERE 1=1'),\n    ]\n")), 1)

    def test_freigabe_mit_grund_laesst_durch_ohne_grund_nicht(self):
        loeschen = KOPF + "        migrations.RemoveField(model_name='beleg', name='x'),\n    ]\n"
        self.assertEqual(pruefung.befunde("# Datenverlust freigegeben: Feld x ersetzt durch y (Auftrag vom 15.11.2026)\n" + loeschen), [])
        self.assertEqual(len(pruefung.befunde("# Datenverlust freigegeben:\n" + loeschen)), 1)

    def test_aufruf_mit_dateien(self):
        with tempfile.TemporaryDirectory() as ordner:
            gut, schlecht = Path(ordner) / "0001_gut.py", Path(ordner) / "0002_schlecht.py"
            gut.write_text(KOPF + "    ]\n", encoding="utf-8")
            schlecht.write_text(KOPF + "        migrations.DeleteModel(name='Alt'),\n    ]\n", encoding="utf-8")
            self.assertEqual(pruefung.main([str(gut)]), 0)
            self.assertEqual(pruefung.main([str(gut), str(schlecht), str(Path(ordner) / "fehlt.py")]), 1)
