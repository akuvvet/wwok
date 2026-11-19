"""Gemeinsame Suchsyntax für Kunden und Lieferanten (Wunsch des Nutzers 19.11.2026: eine Funktion statt zwei Kopien).

- Wörter: jedes Wort muss in einem der Suchfelder vorkommen, Reihenfolge egal
- ``*`` allein: alle
- ``Kos*``: Stern am Ende = Wortanfang (gilt wie bisher für alle Wörter der Eingabe)
- ``%`` wie SQL-LIKE, je Wort: ``Kos%`` beginnt mit, ``%markt%`` enthält, ``%GmbH`` endet mit, ``Ma%er`` beginnt mit „Ma“
  und endet mit „er“ (verankert am Anfang und Ende des Feldinhalts)

Die Artikelsuche hat ihre eigene, erweiterte Syntax in ``apps/stammdaten/suche.py`` (dort zusätzlich ``>5`` für den Bestand).
"""
from django.db.models import Q

from .suche import like_regex as _like  # dieselbe LIKE-Umwandlung samt Schutz vor sehr langsamen Mustern


def partner_filter(q: str, felder) -> Q:
    """Q-Filter für die Suche über ``felder`` (z. B. ``["nummer", "name1", "ort"]``). Leere Eingabe oder ``*`` = kein Filter."""
    q = (q or "").strip()
    filt = Q()
    if not q or q == "*":
        return filt
    praefix = q.endswith("*")
    for wort in q.rstrip("*").replace("*", " ").split():
        if "%" in wort:
            if not wort.strip("%"):
                continue  # nur Prozentzeichen: passt auf alles
            bedingung, wert = "iregex", _like(wort)
        else:
            bedingung, wert = ("istartswith" if praefix else "icontains"), wort
        filt &= Q(*[Q(**{f"{feld}__{bedingung}": wert}) for feld in felder], _connector=Q.OR)
    return filt
