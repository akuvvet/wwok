"""Masterpasswort zum Löschen eines Mandanten (Wunsch des Nutzers 25.11.2026).

Gespeichert wird nur ein Hash, und zwar in der .env auf dem Server (WWOK_MASTERPASSWORT_HASH) – nicht in der Datenbank, damit kein
Benutzer der Verwaltung es ändern kann. Den Hash erzeugt `python manage.py masterpasswort_hash`. Ist keiner eingerichtet, lässt sich
kein Mandant löschen.
"""
import os

from django.contrib.auth.hashers import check_password

UMGEBUNG = "WWOK_MASTERPASSWORT_HASH"


def eingerichtet() -> bool:
    return bool(os.environ.get(UMGEBUNG, "").strip())


def pruefen(klartext: str) -> bool:
    gespeichert = os.environ.get(UMGEBUNG, "").strip()
    return bool(gespeichert and klartext and check_password(klartext, gespeichert))
