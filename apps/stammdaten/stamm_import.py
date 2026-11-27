"""Import von Kategorien und Leistungskategorien aus CSV/Excel und Beispieldateien für alle Stammdaten-Importe
(Wunsch des Nutzers 26.11.2026). Wie beim Artikelimport: alles oder nichts, leere Zellen lassen vorhandene Werte stehen."""
import csv
import io

from django.db import DataError, IntegrityError, transaction

from apps.einkauf.preislisten import text

from .leistung_import import ImportFehler, datei_lesen, dezimal, ja_nein, kopfname, zelle_pruefen
from .models import Hersteller, Kategorie, Leistungskategorie

# Beispieldateien: Kopfzeile und eine Beispielzeile (bei Kategorien zwei, damit die Ebene „übergeordnet“ sichtbar wird).
# Zahlen stehen als Text mit Dezimalkomma in der CSV und als Zahl in Excel; Nummern und EAN bleiben in Excel Text.
BEISPIELE = {
    "artikel": (["Nummer", "Bezeichnung", "Erweiterte Bezeichnung", "Kategorie", "Hersteller", "Herstellernummer", "EAN", "EK",
                 "VK1", "VK2", "VK3", "UVP", "Einheit", "Steuersatz", "Gewicht", "Garantie", "Online", "nicht aktiv",
                 "Kostenstelle", "Abbildung 1", "Abbildung 2"],
                [["99001", "Netzteil 600W ATX 120mm Lüfter", "Leises ATX-Netzteil mit 120-mm-Lüfter", "Netzteile", "LC-Power",
                  "LC600H-12", "4260070120017", 18.5, 29.9, 27.9, 25.9, 34.9, "Stück", 19, 1.2, 24, "nein", "nein", "100",
                  "99001.jpg", ""]]),
    "leistungsartikel": (["Nummer", "Bezeichnung", "Erweiterte Bezeichnung", "Kategorie", "Kosten", "VK1", "VK2", "VK3", "Einheit",
                          "Steuersatz", "nicht aktiv"],
                         [["40098", "Montage PC", "Zusammenbau und Test eines PCs", "Werkstatt", 20, 35, "", "", "Std.", 19,
                           "nein"]]),
    "kategorie": (["Nummer", "Name", "Übergeordnet", "Aufschlag VK1", "Aufschlag VK2", "Aufschlag VK3", "Online"],
                  [["100", "Hardware", "", "", "", "", "nein"], ["110", "Netzteile", "100", 25, 20, 15, "nein"]]),
    "leistungskategorie": (["Nummer", "Name"], [["10", "Werkstatt"]]),
    "hersteller": (["Kennzeichen", "Name", "Online"], [["LCP", "LC-Power", "ja"]]),
}
TEXTSPALTEN = {"Kennzeichen", "Nummer", "EAN", "Herstellernummer", "Übergeordnet", "Kostenstelle"}


def beispiel(art: str, format_: str) -> bytes:
    kopf, zeilen = BEISPIELE[art]
    if format_ == "xlsx":
        from openpyxl import Workbook
        from openpyxl.styles import Font
        buch = Workbook()
        blatt = buch.active
        blatt.title = art.capitalize()
        blatt.append(kopf)
        for zelle in blatt[1]:
            zelle.font = Font(bold=True)
        for zeile in zeilen:
            blatt.append(zeile)
        for i, name in enumerate(kopf, start=1):
            spalte = blatt.cell(row=1, column=i).column_letter
            blatt.column_dimensions[spalte].width = max(12, min(40, len(name) + 4,
                                                                max(len(text(z[i - 1])) + 2 for z in zeilen)))
            if name in TEXTSPALTEN:
                for z in range(2, len(zeilen) + 2):
                    blatt.cell(row=z, column=i).number_format = "@"
        puffer = io.BytesIO()
        buch.save(puffer)
        return puffer.getvalue()
    puffer = io.StringIO()
    w = csv.writer(puffer, delimiter=";", lineterminator="\r\n")
    w.writerow(kopf)
    for zeile in zeilen:
        w.writerow([f"{z:.2f}".replace(".", ",") if isinstance(z, float) else z for z in zeile])
    return ("﻿" + puffer.getvalue()).encode("utf-8")


def _spalten(kopf, moegliche: dict, pflicht: dict) -> dict:
    namen = [kopfname(k) for k in kopf]
    spalten = {f: namen.index(n) for f, liste in moegliche.items() for n in reversed(liste) if n in namen}
    fehlend = [anzeige for feld, anzeige in pflicht.items() if feld not in spalten]
    if fehlend:
        raise ImportFehler(f"In der Kopfzeile fehlt: {', '.join(fehlend)}.")
    return spalten


def _zellen(zeile, spalten):
    def zelle(feld):
        i = spalten.get(feld)
        wert = zeile[i] if i is not None and i < len(zeile) else ""
        return wert.strip() if isinstance(wert, str) else ("" if wert is None else wert)
    return zelle


def _speichern(objekt):
    try:
        with transaction.atomic():
            objekt.full_clean(exclude=["eltern"]) if isinstance(objekt, Kategorie) else objekt.full_clean()
            objekt.save()
    except (IntegrityError, DataError):
        raise ValueError("Nummer oder Name gibt es schon.")
    except Exception as e:  # ValidationError: lesbar zusammenfassen
        meldungen = getattr(e, "message_dict", None)
        if meldungen is None:
            raise
        raise ValueError(" ".join(f"{objekt._meta.get_field(f).verbose_name if f != '__all__' else ''}: {' '.join(m)}".strip(": ")
                                  for f, m in meldungen.items()))


SPALTEN_KATEGORIE = {
    "nummer": ("nummer", "nr", "kategorienummer", "kategorienr", "id"),
    "name": ("name", "kategorie", "bezeichnung", "kategoriename"),
    "eltern": ("übergeordnet", "uebergeordnet", "ubergeordnet", "oberkategorie", "eltern", "elternkategorie", "parent"),
    "aufschlag_vk1": ("aufschlagvk1", "aufschlag1", "aufschlag"),
    "aufschlag_vk2": ("aufschlagvk2", "aufschlag2"),
    "aufschlag_vk3": ("aufschlagvk3", "aufschlag3"),
    "online": ("online", "shop"),
}


def _kategorie_suchen(wert, gemerkt):
    """Übergeordnete Kategorie über Nummer oder eindeutigen Namen, auch eine weiter oben in derselben Datei."""
    if wert.lower() in gemerkt:
        return gemerkt[wert.lower()]
    treffer = Kategorie.objects.filter(nummer__iexact=wert).first()
    if treffer is None:
        namen = list(Kategorie.objects.filter(name__iexact=wert)[:2])
        if len(namen) > 1:
            raise ValueError(f"Übergeordnete Kategorie „{wert}“ gibt es mehrfach – bitte die Nummer angeben.")
        treffer = namen[0] if namen else None
    if treffer is None:
        raise ValueError(f"Übergeordnete Kategorie „{wert}“ nicht gefunden (sie muss vorhanden sein oder weiter oben in der Datei stehen).")
    return treffer


def kategorien_importieren(dateiname: str, inhalt: bytes):
    """Artikelkategorien: vorhandene über die Nummer, sonst über Name und übergeordnete Kategorie; sonst neu (offline, solange
    „Online“ nicht ja ist). Gibt (neu, geändert, unverändert) zurück."""
    zeilen = datei_lesen(dateiname, inhalt)
    if not zeilen:
        raise ImportFehler("Die Datei ist leer.")
    spalten = _spalten(zeilen[0], SPALTEN_KATEGORIE, {"name": "Name"})
    zaehler, fehler, gemerkt = {"neu": 0, "geaendert": 0, "gleich": 0}, [], {}
    with transaction.atomic():
        for nr, zeile in enumerate(zeilen[1:], start=2):
            if not any(text(w) for w in zeile):
                continue
            zelle = _zellen(zeile, spalten)
            try:
                for feld in ("nummer", "name", "eltern"):
                    zelle_pruefen("kategorie" if feld != "nummer" else "nummer", zelle(feld))
                name, nummer, eltern_wert = text(zelle("name")), text(zelle("nummer")), text(zelle("eltern"))
                if not name:
                    raise ValueError("Der Name fehlt.")
                eltern = _kategorie_suchen(eltern_wert, gemerkt) if eltern_wert else None
                k = Kategorie.objects.filter(nummer__iexact=nummer).first() if nummer else None
                if k is None:
                    k = Kategorie.objects.filter(name__iexact=name, eltern=eltern).first()
                neu = k is None
                if neu:
                    k = Kategorie(nummer=nummer or None)
                vorher = (k.nummer, k.name, k.eltern_id, k.aufschlag_vk1, k.aufschlag_vk2, k.aufschlag_vk3, k.online)
                k.name = name
                if nummer:
                    k.nummer = nummer
                if eltern_wert or neu:
                    if eltern is not None and not neu and eltern.pk in k.mit_unterkategorien():
                        raise ValueError(f"„{name}“ kann nicht unter „{eltern.name}“ stehen (eigene Unterkategorie).")
                    k.eltern = eltern
                for feld in ("aufschlag_vk1", "aufschlag_vk2", "aufschlag_vk3"):
                    if zelle(feld) != "":
                        setattr(k, feld, dezimal(zelle(feld), feld.replace("_", " "), 2))
                if zelle("online") != "":
                    k.online = ja_nein(zelle, "online")
                geaendert = vorher != (k.nummer, k.name, k.eltern_id, k.aufschlag_vk1, k.aufschlag_vk2, k.aufschlag_vk3, k.online)
                if neu or geaendert:
                    _speichern(k)
                for schluessel in (name.lower(), (nummer or "").lower()):
                    if schluessel:
                        gemerkt[schluessel] = k
                zaehler["neu" if neu else "geaendert" if geaendert else "gleich"] += 1
            except ValueError as e:
                fehler.append(f"Zeile {nr}: {e}")
        if fehler:
            transaction.set_rollback(True)
    if fehler:
        raise ImportFehler("Nichts importiert. Bitte diese Zeilen in der Datei korrigieren:", fehler)
    return zaehler["neu"], zaehler["geaendert"], zaehler["gleich"]


SPALTEN_LEISTUNGSKATEGORIE = {
    "nummer": ("nummer", "nr", "kategorienummer", "kategorienr", "id"),
    "name": ("name", "kategorie", "leistungskategorie", "bezeichnung"),
}
SPALTEN_HERSTELLER = {
    "nummer": ("kennzeichen", "id", "nummer", "nr", "herstellernummer", "herstellerid", "kurzzeichen"),
    "name": ("name", "hersteller", "herstellername", "bezeichnung", "marke"),
    "online": ("online", "shop"),
}


def _einfach_importieren(dateiname: str, inhalt: bytes, modell, spalten_def: dict, schluessel: str):
    """Leistungskategorien und Hersteller: vorhandene über die Nummer bzw. das Kennzeichen (dann wird der Name übernommen),
    sonst über den Namen; sonst neu. Gibt (neu, geändert, unverändert) zurück."""
    zeilen = datei_lesen(dateiname, inhalt)
    if not zeilen:
        raise ImportFehler("Die Datei ist leer.")
    spalten = _spalten(zeilen[0], spalten_def, {"name": "Name"})
    mit_online = "online" in spalten
    zaehler, fehler, gesehen = {"neu": 0, "geaendert": 0, "gleich": 0}, [], set()
    with transaction.atomic():
        for nr, zeile in enumerate(zeilen[1:], start=2):
            if not any(text(w) for w in zeile):
                continue
            zelle = _zellen(zeile, spalten)
            try:
                zelle_pruefen("nummer", zelle("nummer"))
                zelle_pruefen("kategorie", zelle("name"))
                name, nummer = text(zelle("name")), text(zelle("nummer"))
                if not name:
                    raise ValueError("Der Name fehlt.")
                k = (modell.objects.filter(**{f"{schluessel}__iexact": nummer}).first() if nummer else None) \
                    or modell.objects.filter(name__iexact=name).first()
                if k is not None and k.pk in gesehen:
                    raise ValueError(f"„{name}“ steht doppelt in der Datei.")
                neu = k is None
                k = k or modell()
                vorher = (getattr(k, schluessel), k.name, getattr(k, "online", None))
                k.name = name
                if nummer:
                    setattr(k, schluessel, nummer)
                if mit_online and zelle("online") != "":
                    k.online = ja_nein(zelle, "online")
                geaendert = vorher != (getattr(k, schluessel), k.name, getattr(k, "online", None))
                if neu or geaendert:
                    _speichern(k)
                gesehen.add(k.pk)
                zaehler["neu" if neu else "geaendert" if geaendert else "gleich"] += 1
            except ValueError as e:
                fehler.append(f"Zeile {nr}: {e}")
        if fehler:
            transaction.set_rollback(True)
    if fehler:
        raise ImportFehler("Nichts importiert. Bitte diese Zeilen in der Datei korrigieren:", fehler)
    return zaehler["neu"], zaehler["geaendert"], zaehler["gleich"]


def leistungskategorien_importieren(dateiname: str, inhalt: bytes):
    return _einfach_importieren(dateiname, inhalt, Leistungskategorie, SPALTEN_LEISTUNGSKATEGORIE, "nummer")


def hersteller_importieren(dateiname: str, inhalt: bytes):
    return _einfach_importieren(dateiname, inhalt, Hersteller, SPALTEN_HERSTELLER, "kennzeichen")
