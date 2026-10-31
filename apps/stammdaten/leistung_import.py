"""Leistungsartikel aus einer CSV- oder Excel-Datei einlesen. Die Spalten werden an der Kopfzeile erkannt.
Alles oder nichts: Bei einem Fehler in einer Zeile wird nichts importiert."""
import math
import re
from datetime import date, time, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from types import SimpleNamespace

from django.core.exceptions import ValidationError
from django.db import DataError, IntegrityError, transaction
from django.db.models import Q

from apps.einkauf.assistent import format_erkennen
from apps.einkauf.models import Dateityp
from apps.einkauf.preislisten import tabelle, text, zahl

from .models import Artikel, Artikeltyp, Leistungskategorie, Steuersatz

SPALTEN = {
    "nummer": ("nummer", "nr", "artikelnummer", "artikelnr", "artnr", "leistungsnummer", "leistungsnr"),
    "bezeichnung": ("bezeichnung", "leistung", "benennung", "artikelbezeichnung", "name"),
    "bezeichnung_erweitert": ("erweitertebenennung", "erweitertebezeichnung", "beschreibung", "langtext"),
    "kategorie": ("kategorie", "leistungskategorie", "warengruppe", "gruppe"),
    "kosten": ("kosten", "aufwand", "ek", "ekpreis", "eknetto", "einkaufspreis"),
    "vk1": ("vk1", "vkpreis1", "vk1netto", "vk", "vkpreis", "vknetto", "verkaufspreis", "preis"),
    "vk2": ("vk2", "vkpreis2", "vk2netto"),
    "vk3": ("vk3", "vkpreis3", "vk3netto"),
    "mengeneinheit": ("mengeneinheit", "einheit", "me"),
    "steuersatz": ("steuersatz", "mwst", "ust", "steuer"),
    "nicht_aktiv": ("nichtaktiv", "inaktiv"),
}
GELDFELDER = ("kosten", "vk1", "vk2", "vk3")
TEXTFELDER = ("bezeichnung", "bezeichnung_erweitert", "mengeneinheit")
EINZEILIG = ("nummer", "bezeichnung", "kategorie", "mengeneinheit")
JA = {"1", "1.0", "1,0", "ja", "j", "x", "y", "yes", "true", "wahr"}
NEIN = {"0", "0.0", "0,0", "nein", "n", "no", "false", "falsch"}
DEUTSCH = re.compile(r"[1-9]\d{0,2}(\.\d{3})+(,\d{1,2})?|\d+(,\d{1,2})?")
MIT_PUNKT = re.compile(r"\d+\.\d{1,2}")
EXCEL_FEHLER = re.compile(r"#(N/A|NULL!|DIV/0!|VALUE!|REF!|NAME\?|NUM!)", re.IGNORECASE)


class ImportFehler(Exception):
    def __init__(self, meldung, fehler=()):
        super().__init__(meldung)
        self.fehler = list(fehler)


def _kopfname(wert) -> str:
    return "".join(c for c in text(wert).lower() if c.isalnum())


def spalten_finden(kopf) -> dict:
    namen = [_kopfname(k) for k in kopf]
    spalten = {}
    for feld, moegliche in SPALTEN.items():
        for name in moegliche:
            if name in namen:
                spalten[feld] = namen.index(name)
                break
    fehlend = [n for n, feld in (("Nummer", "nummer"), ("Bezeichnung", "bezeichnung")) if feld not in spalten]
    if fehlend:
        raise ImportFehler(f"In der Kopfzeile fehlt: {', '.join(fehlend)}.")
    return spalten


def zelle_pruefen(feld, wert):
    """Lehnt ab, was Excel statt eines Werts liefert: Datum, Wahrheitswert, Fehlerzelle. Ein Zeilenumbruch in einem
    einzeiligen Feld kommt meist von einem offenen Anführungszeichen, das die Folgezeilen verschluckt."""
    if ((isinstance(wert, bool) and feld != "nicht_aktiv") or isinstance(wert, (date, time, timedelta))
            or (isinstance(wert, float) and not math.isfinite(wert)) or (isinstance(wert, str) and EXCEL_FEHLER.fullmatch(wert))):
        raise ValueError(f"Spalte {feld}: Inhalt ungültig: '{text(wert)}'")
    if feld in EINZEILIG and isinstance(wert, str) and "\n" in wert:
        raise ValueError(f"Spalte {feld}: Zeilenumbruch in der Zelle, vermutlich fehlt ein Anführungszeichen.")


def geld(wert) -> Decimal:
    """Betrag aus einer Zelle: Zahl aus Excel oder Text wie 1.234,56 € oder 12.50. Alles Mehrdeutige wird abgelehnt,
    damit kein Preis still um den Faktor 1000 falsch wird; negative Beträge sind nicht erlaubt."""
    if isinstance(wert, (int, float)) and not isinstance(wert, bool):
        betrag = Decimal(str(wert))
    else:
        t = re.sub(r"€|EUR|\s", "", text(wert))
        if DEUTSCH.fullmatch(t):
            betrag = Decimal(t.replace(".", "").replace(",", "."))
        elif MIT_PUNKT.fullmatch(t):
            betrag = Decimal(t)
        else:
            raise ValueError(f"Betrag ungültig: '{text(wert)}'")
    if not betrag.is_finite() or betrag < 0 or betrag >= Decimal("1E10"):
        raise ValueError(f"Betrag ungültig: '{text(wert)}'")
    return betrag.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def steuersatz_finden(wert, saetze):
    """Steuersatz über die Mention-ID (1, 2) oder die Prozentzahl (19, 7 %)."""
    try:
        d = zahl(text(wert).replace("%", ""), ",")
        treffer = next((s for s in saetze if s.nummer is not None and d == s.nummer), None)
        treffer = treffer or next((s for s in saetze if d == s.prozent), None)
    except (ValueError, InvalidOperation):
        treffer = None
    if treffer is None:
        raise ValueError(f"Steuersatz unbekannt: '{text(wert)}'")
    return treffer


def datei_lesen(dateiname: str, inhalt: bytes) -> list:
    try:
        format_ = format_erkennen(dateiname, inhalt)
        if format_["dateityp"] == Dateityp.XML:
            raise ImportFehler("Bitte eine CSV- oder Excel-Datei verwenden.")
        return tabelle(SimpleNamespace(**format_), inhalt)
    except ImportFehler:
        raise
    except Exception:  # Bibliotheken für Excel und CSV melden kaputte Dateien mit jeweils eigenen Ausnahmen
        raise ImportFehler("Die Datei lässt sich nicht lesen. Bitte eine CSV- oder Excel-Datei verwenden.")


def kategorie_holen(wert, gemerkt, neue):
    """Vorhandene Kategorie über die Nummer, sonst den Namen (Vergleich durch die Datenbank, wie bei der Eindeutigkeit), oder neu
    anlegen. Eine reine Zahl ist die Mention-Nummer; der Name lässt sich danach in der Verwaltung ändern."""
    schluessel = wert.lower()
    if schluessel not in gemerkt:
        kategorie = (Leistungskategorie.objects.filter(nummer__iexact=wert).first()
                     or Leistungskategorie.objects.filter(name__iexact=wert).first())
        if kategorie is None:
            kategorie = Leistungskategorie.objects.create(name=wert, nummer=wert if wert.isdigit() and len(wert) <= 20 else None)
            neue.append(kategorie)
        gemerkt[schluessel] = kategorie
    return gemerkt[schluessel]


def _zeile_uebernehmen(a, zelle, saetze):
    """Trägt die gefüllten Zellen in den Artikel ein; leere Zellen lassen den vorhandenen Wert stehen."""
    for feld in TEXTFELDER:
        if zelle(feld):
            setattr(a, feld, text(zelle(feld)))
    for feld in GELDFELDER:
        if zelle(feld) != "":
            setattr(a, feld, geld(zelle(feld)))
    if zelle("nicht_aktiv") != "":
        wert = text(zelle("nicht_aktiv")).lower()
        if wert not in JA | NEIN:
            raise ValueError(f"Spalte nicht_aktiv: Inhalt ungültig: '{wert}'")
        a.nicht_aktiv = wert in JA
    if zelle("steuersatz") != "":
        a.steuersatz = steuersatz_finden(zelle("steuersatz"), saetze)


def importieren(dateiname: str, inhalt: bytes):
    """Gibt (neu, geändert, unverändert, neue Kategorien) zurück."""
    zeilen = datei_lesen(dateiname, inhalt)
    if not zeilen:
        raise ImportFehler("Die Datei ist leer.")
    spalten = spalten_finden(zeilen[0])
    saetze = list(Steuersatz.objects.all())

    def zellen(zeile):
        def zelle(feld):
            i = spalten.get(feld)
            wert = zeile[i] if i is not None and i < len(zeile) else ""
            return wert.strip() if isinstance(wert, str) else ("" if wert is None else wert)
        return zelle

    fehler, gesehen, geplant = [], set(), []
    for nr, zeile in enumerate(zeilen[1:], start=2):
        if not any(text(w) for w in zeile):
            continue
        zelle = zellen(zeile)
        if any(text(w) for w in zeile[len(zeilen[0]):]):
            fehler.append(f"Zeile {nr}: Mehr Spalten als in der Kopfzeile. Bitte das Trennzeichen prüfen.")
            continue
        try:
            for feld in spalten:
                zelle_pruefen(feld, zelle(feld))
        except ValueError as e:
            fehler.append(f"Zeile {nr}: {e}")
            continue
        nummer = text(zelle("nummer"))
        if not nummer:
            fehler.append(f"Zeile {nr}: Die Nummer fehlt.")
            continue
        a = Artikel.objects.filter(nummer__iexact=nummer).first()  # die Datenbank vergleicht wie bei ihrer Eindeutigkeit
        schluessel = a.nummer.lower() if a else nummer.lower()
        if schluessel in gesehen:
            fehler.append(f"Zeile {nr}: Nummer {nummer} steht doppelt in der Datei.")
            continue
        gesehen.add(schluessel)
        neu = a is None
        if not neu and a.typ != Artikeltyp.L:
            fehler.append(f"Zeile {nr}: {nummer} ist schon als {a.get_typ_display()} angelegt.")
            continue
        if neu:
            a = Artikel(nummer=nummer, typ=Artikeltyp.L, steuersatz=saetze[0] if saetze else None)
        vorher = {f: getattr(a, f) for f in (*TEXTFELDER, *GELDFELDER, "nicht_aktiv", "steuersatz_id")}
        try:
            _zeile_uebernehmen(a, zelle, saetze)
            kategorie = text(zelle("kategorie"))
            if neu and not a.bezeichnung:
                raise ValueError("Die Bezeichnung fehlt.")
            if neu and a.steuersatz_id is None:
                raise ValueError("Es ist noch kein Steuersatz angelegt.")
            a.clean_fields(exclude=["steuersatz", "leistungskategorie"])
        except ValueError as e:
            fehler.append(f"Zeile {nr}: {e}")
            continue
        except ValidationError as e:
            fehler.append(f"Zeile {nr}: " + " ".join(f"{a._meta.get_field(f).verbose_name}: {' '.join(m)}"
                                                     for f, m in e.message_dict.items()))
            continue
        if len(kategorie) > 150:
            fehler.append(f"Zeile {nr}: Der Kategoriename ist zu lang.")
            continue
        geaendert = neu or any(getattr(a, f) != w for f, w in vorher.items())
        geplant.append((a, kategorie, neu, geaendert))
    if fehler:
        raise ImportFehler("Nichts importiert. Bitte diese Zeilen in der Datei korrigieren:", fehler)

    zaehler = {"neu": 0, "geaendert": 0, "gleich": 0}
    kategorien, neue_kategorien = {}, []
    try:
        with transaction.atomic():
            for a, kategorie, neu, geaendert in geplant:
                if kategorie:
                    eintrag = kategorie_holen(kategorie, kategorien, neue_kategorien)
                    if eintrag.pk != a.leistungskategorie_id:
                        a.leistungskategorie, geaendert = eintrag, True
                if geaendert:
                    a.save()
                zaehler["neu" if neu else "geaendert" if geaendert else "gleich"] += 1
    except (IntegrityError, DataError):  # z. B. Büro und Buro: für die Datenbank kann das derselbe Eintrag sein
        raise ImportFehler("Nichts importiert: Zwei Nummern oder Kategorien gelten für die Datenbank als gleich. Bitte die Schreibweise in der Datei prüfen.")
    return zaehler["neu"], zaehler["geaendert"], zaehler["gleich"], len(neue_kategorien)
