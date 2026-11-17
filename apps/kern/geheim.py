"""Zugangsdaten, die in der Verwaltung eingetragen werden: verschlüsselt in der Datenbank, nie im Klartext.

Der Schlüssel wird aus DJANGO_SECRET_KEY abgeleitet. Ändert sich dieser, sind die gespeicherten Werte nicht mehr
lesbar und müssen in der Verwaltung neu eingetragen werden.
"""
import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


def _fernet() -> Fernet:
    roh = hashlib.sha256(("wwok-geheimnis:" + settings.SECRET_KEY).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(roh))


def verschluesseln(klartext: str) -> str:
    return _fernet().encrypt(klartext.encode()).decode() if klartext else ""


def entschluesseln(geheim: str) -> str:
    """Klartext; leer, wenn nichts gespeichert ist oder der Wert mit einem anderen Schlüssel verschlüsselt wurde."""
    if not geheim:
        return ""
    try:
        return _fernet().decrypt(geheim.encode()).decode()
    except (InvalidToken, ValueError):
        return ""
