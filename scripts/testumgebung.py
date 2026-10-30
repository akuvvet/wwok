"""Startet die lokale Testumgebung: SQLite, Debug, Testzugang, http://127.0.0.1:8000.

Der Zugang steht in .lokal_testzugang (nicht im Repository) und wird bei jedem Start neu gesetzt.
"""
import os
import secrets
import sys
from pathlib import Path

BASIS = Path(__file__).resolve().parent.parent
BENUTZER = "claude_test"

os.chdir(BASIS)
sys.path.insert(0, str(BASIS))
os.environ.update(DJANGO_SETTINGS_MODULE="config.settings", DJANGO_DEBUG="true", DB_ENGINE="sqlite",
                  DJANGO_ALLOWED_HOSTS="localhost,127.0.0.1")

import django  # noqa: E402

django.setup()

from django.contrib.auth import get_user_model  # noqa: E402
from django.core.management import call_command  # noqa: E402


def vorbereiten():
    call_command("migrate", verbosity=0)
    benutzer, _ = get_user_model().objects.get_or_create(username=BENUTZER)
    passwort = secrets.token_urlsafe(18)
    benutzer.is_staff = benutzer.is_superuser = True
    benutzer.set_password(passwort)
    benutzer.save()
    (BASIS / ".lokal_testzugang").write_text(f"{BENUTZER}\n{passwort}\n", encoding="utf-8")


if os.environ.get("RUN_MAIN") != "true":  # der Neustart nach Codeänderungen bereitet nicht erneut vor
    vorbereiten()
call_command("runserver", "127.0.0.1:8000")
