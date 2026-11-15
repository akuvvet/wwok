"""Hält den automatischen Deploy an, wenn eine neue Migration Daten löschen würde (Prüfbericht B1/E8).

Aufruf: python deploy/loeschende_migrationen.py <Migrationsdatei> [...]
Gemeldet werden RemoveField, DeleteModel und RunSQL mit DROP/DELETE/TRUNCATE. Eine Migration geht nur durch, wenn sie eine
Zeile „# Datenverlust freigegeben: <Grund>“ enthält – die trägt man erst ein, wenn der Nutzer das Löschen ausdrücklich
beauftragt hat. Nicht erkannt werden Löschungen in RunPython-Funktionen und verkürzte Felder (AlterField)."""
import re
import sys
from pathlib import Path

FREIGABE = re.compile(r"^[ \t]*#[ \t]*Datenverlust freigegeben:[ \t]*\S+", re.MULTILINE)
MUSTER = [
    (re.compile(r"migrations\.RemoveField\s*\("), "RemoveField (Spalte wird gelöscht)"),
    (re.compile(r"migrations\.DeleteModel\s*\("), "DeleteModel (Tabelle wird gelöscht)"),
    (re.compile(r"migrations\.RunSQL\s*\((?:(?!migrations\.)[\s\S])*?\b(DROP|DELETE|TRUNCATE)\b", re.IGNORECASE),
     "RunSQL mit DROP/DELETE/TRUNCATE"),
]


def befunde(text: str) -> list:
    """Gründe, warum der Text einer Migration Daten löschen würde (leer = unbedenklich oder freigegeben)."""
    if FREIGABE.search(text):
        return []
    return [grund for muster, grund in MUSTER if muster.search(text)]


def main(dateien) -> int:
    fehler = 0
    for name in dateien:
        pfad = Path(name)
        if not pfad.is_file() or pfad.name == "__init__.py":
            continue
        for grund in befunde(pfad.read_text(encoding="utf-8")):
            print(f"ANGEHALTEN: {name}: {grund}")
            fehler += 1
    if fehler:
        print("Diese Migration würde Daten löschen. Der Deploy läuft erst, wenn der Nutzer das Löschen beauftragt hat und in der "
              "Migration eine Zeile „# Datenverlust freigegeben: <Grund>“ steht.")
    return 1 if fehler else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
