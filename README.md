# wwok – Web-Warenwirtschaft

Webbasierte Warenwirtschaft für **OKAY Computer** und **ZONE Handel** (Ersatz für Mention).
Mandantenfähig, GoBD-orientiert, Zugriff nur per HTTPS im Browser.

Anforderungen: siehe [`docs/pflichtenheft.md`](docs/pflichtenheft.md).

## Technik

| Bereich | Wahl |
| --- | --- |
| Sprache | Python 3.10+ |
| Framework | Django 5.1 |
| Datenbank | MySQL / MariaDB (lokal geht auch SQLite) |
| Server | Strato V-Server mit Plesk, Subdomain `erp.okaycomputer.de` |

## Ordnerstruktur

```
wwok/
├── apps/
│   ├── kern/          Mandanten, Basisklassen, Änderungsprotokoll (GoBD)
│   └── stammdaten/    Kunden, Lieferanten, Artikel, Zahlungsbedingungen, Steuersätze
├── config/            Django-Einstellungen, URLs
├── deploy/            Anleitung für Plesk / Server
├── docs/              Pflichtenheft und weitere Doku
├── static/            CSS, JS, Bilder
├── templates/         HTML-Vorlagen
├── .env.example       Vorlage für Zugangsdaten
└── requirements.txt
```

Geplante weitere Module: `verkauf`, `einkauf`, `lager`, `finanzen`, `werkstatt`, `versand`, `schnittstellen`, `auswertungen`.

## Lokal starten (Windows)

```powershell
cd D:\Desktop\github\wwok
py -m venv .venv
.venv\Scripts\activate
pip install Django Pillow
copy .env.example .env
```

In `.env` für lokal eintragen:

```
DJANGO_DEBUG=true
DB_ENGINE=sqlite
DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1
```

Dann:

```powershell
py manage.py migrate
py manage.py createsuperuser
py manage.py runserver
```

Aufrufen: <http://127.0.0.1:8000/verwaltung/>

> `mysqlclient` wird lokal nur gebraucht, wenn du auch lokal MySQL nutzt.

## Regeln für das Repository

- Die `.env` mit Passwörtern kommt **nie** ins Repository.
- `main` = Stand, der auf dem Server läuft. Entwicklung in eigenen Branches.
- Jede Änderung an Modellen braucht eine Migration (`py manage.py makemigrations`), die mit eingecheckt wird.
- Echte Kunden- oder Belegdaten nie einchecken.

## Stand

- [x] Grundgerüst, Einstellungen über `.env`
- [x] Mandanten, Änderungsprotokoll (unveränderlich), letzte Änderung mit Benutzer
- [x] Stammdaten Kunde, Lieferant, Artikel laut Feldlisten im Pflichtenheft
- [ ] Anmeldung mit 2FA
- [ ] Eigene Masken (statt Django-Admin)
- [ ] Verkaufsabwicklung
- [ ] Einkauf, Lager, Finanzen, Werkstatt, Versand, Schnittstellen

## Erstes Hochladen zu GitHub

Repository `akuvvet/wwok` auf GitHub als **privat** anlegen (ohne README, ohne .gitignore). Dann im Ordner:

```powershell
cd D:\Desktop\github\wwok
git init -b main
git add .
git commit -m "Grundgerüst wwok: Kern, Stammdaten, Pflichtenheft"
git remote add origin https://github.com/akuvvet/wwok.git
git push -u origin main
```
