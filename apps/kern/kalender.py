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
