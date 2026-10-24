# Einrichtung auf dem Strato V-Server (Plesk)

## 1. Subdomain anlegen

Plesk → Websites & Domains → okaycomputer.de → **Subdomain hinzufügen**

| Feld | Wert |
| --- | --- |
| Subdomain | `erp` |
| Dokumentenstamm | `/erp.okaycomputer.de/public` |

Danach SSL/TLS-Zertifikate → **Let's Encrypt** für `erp.okaycomputer.de` ausstellen und „HTTP auf HTTPS umleiten“ aktivieren.

Der Programmcode liegt **außerhalb** des Dokumentenstamms:

```
/var/www/vhosts/okaycomputer.de/erp.okaycomputer.de/
├── app/        ← Git-Checkout von akuvvet/wwok
├── public/     ← nur static/ und medien/ (vom Webserver ausgeliefert)
└── venv/
```

## 2. Datenbank

Plesk → Datenbanken → **Datenbank hinzufügen**: Name `wwok`, eigener Benutzer `wwok`, starkes Passwort.
Nicht die Shopware-Datenbank verwenden.

## 3. Code holen und einrichten (per SSH)

```bash
cd /var/www/vhosts/okaycomputer.de/erp.okaycomputer.de
git clone git@github.com:akuvvet/wwok.git app
python3 -m venv venv
source venv/bin/activate
pip install -r app/requirements.txt
cp app/.env.example app/.env
nano app/.env        # Werte eintragen, DJANGO_DEBUG=false
cd app
python manage.py migrate
python manage.py collectstatic --noinput
python manage.py createsuperuser
```

Geheimschlüssel erzeugen:

```bash
python -c "import secrets; print(secrets.token_urlsafe(50))"
```

Für `mysqlclient` muss auf dem Server ggf. `libmysqlclient-dev` (bzw. `default-libmysqlclient-dev`) und `pkg-config` installiert sein.

## 4. Anwendung starten

Gunicorn als systemd-Dienst `/etc/systemd/system/wwok.service`:

```ini
[Unit]
Description=wwok Warenwirtschaft
After=network.target

[Service]
User=SYSTEMBENUTZER_DER_SUBDOMAIN
WorkingDirectory=/var/www/vhosts/okaycomputer.de/erp.okaycomputer.de/app
ExecStart=/var/www/vhosts/okaycomputer.de/erp.okaycomputer.de/venv/bin/gunicorn config.wsgi:application --bind 127.0.0.1:8010 --workers 3
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable --now wwok
```

## 5. Webserver weiterleiten

Plesk → erp.okaycomputer.de → Apache & nginx-Einstellungen → **Zusätzliche nginx-Anweisungen**:

```nginx
location /static/ { alias /var/www/vhosts/okaycomputer.de/erp.okaycomputer.de/app/staticfiles/; }
location /medien/ { alias /var/www/vhosts/okaycomputer.de/erp-daten/medien/; }
location / {
    proxy_pass http://127.0.0.1:8010;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto https;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
}
```

Optional zusätzlich: Zugriff nur aus dem Büro-Netz (`allow BÜRO-IP; deny all;`).

## 6. Aktualisieren

```bash
cd /var/www/vhosts/okaycomputer.de/erp.okaycomputer.de/app
git pull
source ../venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py collectstatic --noinput
systemctl restart wwok
```

## 7. Sicherung

Plesk-Backup täglich für die Subdomain **und** die Datenbank `wwok`, zusätzlich extern ablegen.
