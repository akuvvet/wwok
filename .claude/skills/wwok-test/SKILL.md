---
name: wwok-test
description: Führt die wwok-Prüfkette aus (check, Migrationen, Testsuite) und berichtet das Ergebnis. Vor jedem Commit und nach jeder Änderung verwenden.
---

# wwok testen

Im Projektordner `D:\Desktop\github\wwok`, mit dem venv (lokal SQLite, `DJANGO_DEBUG=true`, siehe README 11 und 19.5):

```bash
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test apps
```

Regeln
- Alle drei müssen grün sein, sonst nicht committen und nicht deployen.
- Einzelne App beim Entwickeln: `python manage.py test apps.verkauf`.
- Ergebnis ehrlich berichten: Anzahl Tests, Fehler, nicht gelaufene Teile.
- Neue Logik ohne Test gilt als unfertig.
