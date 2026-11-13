"""Kalender für Abrechnungen: gesetzliche Feiertage in NRW, Wochentage und die Stichtage 15. und Monatsletzter."""
import calendar
from datetime import date, timedelta
from functools import lru_cache


def ostersonntag(jahr: int) -> date:
    """Gregorianischer Ostersonntag (Spencer)."""
    a, b, c = jahr % 19, jahr // 100, jahr % 100
    d, e = b // 4, b % 4
    g = (8 * b + 13) // 25
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    monat, tag = divmod(h + l - 7 * m + 114, 31)
    return date(jahr, monat, tag + 1)


@lru_cache(maxsize=16)
def feiertage(jahr: int) -> dict:
    """Gesetzliche Feiertage in Nordrhein-Westfalen: {Datum: Name}."""
    ostern = ostersonntag(jahr)
    return {
        date(jahr, 1, 1): "Neujahr",
        ostern - timedelta(days=2): "Karfreitag",
        ostern + timedelta(days=1): "Ostermontag",
        date(jahr, 5, 1): "Tag der Arbeit",
        ostern + timedelta(days=39): "Christi Himmelfahrt",
        ostern + timedelta(days=50): "Pfingstmontag",
        ostern + timedelta(days=60): "Fronleichnam",
        date(jahr, 10, 3): "Tag der Deutschen Einheit",
        date(jahr, 11, 1): "Allerheiligen",
        date(jahr, 12, 25): "1. Weihnachtstag",
        date(jahr, 12, 26): "2. Weihnachtstag",
    }


def ist_frei(tag: date) -> bool:
    """Wochenende oder Feiertag; ein Feiertag am Wochenende zählt nur einmal."""
    return tag.weekday() >= 5 or tag in feiertage(tag.year)


def tage_zaehlen(von: date, bis: date) -> tuple:
    """(Wochentage, Wochenend- und Feiertage) von bis bis, beide Tage eingeschlossen."""
    alle = (bis - von).days + 1
    frei = sum(ist_frei(von + timedelta(days=i)) for i in range(alle))
    return alle - frei, frei


def monatsletzter(tag: date) -> date:
    return tag.replace(day=calendar.monthrange(tag.year, tag.month)[1])


def stichtag(tag: date) -> date:
    """Der Stichtag, in dessen Halbmonat der Tag liegt: 15. oder Monatsletzter."""
    return tag.replace(day=15) if tag.day <= 15 else monatsletzter(tag)


def naechster_stichtag(tag: date) -> date:
    return stichtag(stichtag(tag) + timedelta(days=1))


def halbmonat(tag: date) -> tuple:
    """(von, bis) des Halbmonats: 1.–15. oder 16.–Monatsletzter."""
    return (tag.replace(day=1), tag.replace(day=15)) if tag.day <= 15 else (tag.replace(day=16), monatsletzter(tag))


def ganzer_monat(tag: date) -> tuple:
    return tag.replace(day=1), monatsletzter(tag)


# ---- Frei wählbare Stichtage (wiederkehrende Rechnungen) -----------------------------------------------------------
# Stichtage als Tage im Monat (1–31); 31 bzw. „letzter“ = Monatsletzter, ein Tag über der Monatslänge wird auf den
# Monatsletzten gekürzt. Abgerechnet wird jeweils der Zeitraum seit dem vorigen Stichtag (vorigen Stichtag + 1 bis Stichtag).

LETZTER = 31
STANDARD_STICHTAGE = (15, LETZTER)
_LETZTER_WOERTER = {"letzter", "letzte", "ende", "monatsende", "ultimo", "l"}


def stichtage_parsen(text) -> tuple:
    """„10, 25“ oder „1; 15 letzter“ → sortierte Tage (31 = Monatsletzter). ValueError bei ungültiger Angabe."""
    tage = set()
    for teil in str(text or "").replace(";", ",").replace(",", " ").replace(".", " ").split():
        wort = teil.strip().casefold()
        if wort in _LETZTER_WOERTER:
            tage.add(LETZTER)
        elif wort.isdigit() and 1 <= int(wort) <= 31:
            tage.add(int(wort))
        else:
            raise ValueError(f"„{teil}“ ist kein Tag im Monat (1–31 oder „letzter“).")
    if not tage:
        raise ValueError("Bitte mindestens einen Stichtag angeben, z. B. „15, letzter“.")
    return tuple(sorted(tage))


def stichtage_text(tage) -> str:
    return ", ".join("letzter" if t == LETZTER else str(t) for t in tage)


def stichtage_im_monat(jahr: int, monat: int, tage) -> list:
    laenge = calendar.monthrange(jahr, monat)[1]
    return sorted({date(jahr, monat, min(t, laenge)) for t in tage})


def _monat_plus(jahr: int, monat: int, plus: int) -> tuple:
    gesamt = jahr * 12 + monat - 1 + plus
    return gesamt // 12, gesamt % 12 + 1


def stichtag_ab(tag: date, tage=STANDARD_STICHTAGE) -> date:
    """Der erste Stichtag am oder nach dem Tag."""
    for d in stichtage_im_monat(tag.year, tag.month, tage):
        if d >= tag:
            return d
    return stichtage_im_monat(*_monat_plus(tag.year, tag.month, 1), tage)[0]


def folgender_stichtag(tag: date, tage=STANDARD_STICHTAGE) -> date:
    return stichtag_ab(tag + timedelta(days=1), tage)


def voriger_stichtag(tag: date, tage=STANDARD_STICHTAGE) -> date:
    """Der letzte Stichtag vor dem Tag."""
    for d in reversed(stichtage_im_monat(tag.year, tag.month, tage)):
        if d < tag:
            return d
    return stichtage_im_monat(*_monat_plus(tag.year, tag.month, -1), tage)[-1]


def zeitraum(termin: date, tage=STANDARD_STICHTAGE) -> tuple:
    """(von, bis) der Abrechnung zum Stichtag: Tag nach dem vorigen Stichtag bis zum Stichtag."""
    return voriger_stichtag(termin, tage) + timedelta(days=1), termin


def monatsenden(von: date, bis: date) -> list:
    """Alle Monatsletzten im Zeitraum (beide Tage eingeschlossen)."""
    ergebnis, tag = [], monatsletzter(von)
    while tag <= bis:
        ergebnis.append(tag)
        tag = monatsletzter(tag + timedelta(days=1))
    return ergebnis
