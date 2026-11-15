"""Zwischenablage für hochgeladene Preislisten (Vorschau → Import).

Die Dateien enthalten Einkaufspreise und liegen deshalb NICHT unter MEDIA_ROOT (das liefert der Webserver ohne Anmeldung aus),
sondern unter settings.PRIVAT_ROOT. Sie werden nach dem Import gelöscht; was liegen bleibt (abgebrochene Vorschau), räumt
aufraeumen() nach einem Tag weg."""
import os
import time
import uuid
from pathlib import Path

from django.conf import settings

HALTBAR_SEKUNDEN = 24 * 3600


def _ordner() -> Path:
    return Path(settings.PRIVAT_ROOT) / "preislisten"


def _pfad(token: str) -> Path:
    return _ordner() / os.path.basename(token or "")  # basename: kein Ausbruch aus dem Ordner über „../“


def aufraeumen(jetzt=None) -> int:
    """Löscht liegen gebliebene Dateien, die älter als ein Tag sind, und alles aus der früheren, öffentlich erreichbaren Ablage
    (MEDIA_ROOT/tmp/preislisten). Gibt die Anzahl der gelöschten Dateien zurück."""
    jetzt = jetzt or time.time()
    geloescht = 0
    for ordner, alles in ((_ordner(), False), (Path(settings.MEDIA_ROOT) / "tmp" / "preislisten", True)):
        if not ordner.is_dir():
            continue
        for datei in ordner.iterdir():
            try:
                if datei.is_file() and (alles or jetzt - datei.stat().st_mtime > HALTBAR_SEKUNDEN):
                    datei.unlink()
                    geloescht += 1
            except OSError:  # gleichzeitig von einem anderen Aufruf gelöscht oder gesperrt: beim nächsten Mal
                continue
    return geloescht


def speichern(inhalt: bytes, name: str) -> str:
    """Legt die Datei ab und gibt das Token zurück (zufällig, enthält den Dateinamen für die spätere Anzeige)."""
    aufraeumen()
    ordner = _ordner()
    ordner.mkdir(parents=True, exist_ok=True)
    token = f"{uuid.uuid4().hex}__{os.path.basename(name or 'datei')}"
    (ordner / token).write_bytes(inhalt)
    return token


def lesen(token: str):
    """(Inhalt, Dateiname) oder None, wenn die Datei nicht (mehr) vorhanden ist."""
    pfad = _pfad(token)
    if not token or not pfad.is_file():
        return None
    return pfad.read_bytes(), token.split("__", 1)[-1]


def loeschen(token: str) -> None:
    if token:
        _pfad(token).unlink(missing_ok=True)
