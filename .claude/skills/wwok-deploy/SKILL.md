---
name: wwok-deploy
description: Liefert dem Nutzer die Deploy-Schritte für den wwok-Server (Plesk, gunicorn) als kopierfertige Einzelblöcke. Nur auf Auftrag verwenden.
---

# Deploy auf erp.okaycomputer.de

Voraussetzung: Skill `wwok-test` grün, Änderungen committet, Nutzer hat Push freigegeben.

Der Nutzer führt die Serverbefehle selbst per SSH aus (`root@87.106.224.198`) und schickt Ausgaben zurück. Einzelne Bash-Blöcke geben, im Ordner `/var/www/vhosts/okaycomputer.de/erp.okaycomputer.de/app`:

```bash
git pull
```

```bash
chown -R shopokaycomputer:psacln . && chmod 600 .env
```

```bash
sudo -u shopokaycomputer ../venv/bin/python manage.py migrate
```

```bash
systemctl restart wwok && systemctl status wwok --no-pager
```

Bei neuen statischen Dateien zusätzlich `collectstatic`. Fehler im Journal: `journalctl -u wwok -n 50 --no-pager`.

Regeln
- `.env` nie ausgeben; nur Variablennamen prüfen (`cut -d= -f1 .env`).
- Neue Geheimnisse (SMTP_PASSWORD_<MANDANT>, PREISLISTE_PW_<ID>, SHOP_SECRET_<ID>) trägt der Nutzer selbst in die `.env` ein.
- Vor Migrationen mit Datenänderung: Datenbanksicherung anraten.
