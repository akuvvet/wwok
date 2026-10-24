"""Importassistent: Format einer hochgeladenen Datei erkennen, Spalten zuordnen, Artikelpreise (EK/VK) einlesen."""
import xml.etree.ElementTree as ET
from decimal import Decimal

from apps.stammdaten.models import Artikel

from .models import Dateityp, Preislistenprofil
from .preislisten import PreislistenFehler, tabelle, text, zahl

VORSCHLAEGE = {
    "bestellnummer": ("bestellnummer", "best.-nr", "best.nr", "bestellnr", "artikelnummer", "artikel-nr", "art.-nr", "artnr", "sku",
                      "lieferantenartikelnummer", "item"),
    "herstellernummer": ("herstellernummer", "hersteller-nr", "herstellernr", "hersteller artikelnummer", "mpn", "part number",
                         "partnumber", "pn"),
    "ean": ("ean", "ean-code", "gtin", "barcode"),
    "bezeichnung": ("bezeichnung", "artikelbezeichnung", "beschreibung", "name", "titel", "produktname"),
    "hersteller": ("hersteller", "marke", "brand", "manufacturer"),
    "kategorie": ("kategorie", "warengruppe", "category", "gruppe"),
    "ek": ("ek", "ek-preis", "ek netto", "einkaufspreis", "nettopreis", "netto", "händlerpreis", "haendlerpreis", "preis", "price"),
    "bestand": ("bestand", "lagerbestand", "stock", "verfügbar", "verfuegbar", "menge"),
    "lieferzeit": ("lieferzeit", "delivery", "lieferzeit tage"),
    "gewicht": ("gewicht", "weight", "gewicht kg"),
}


def format_erkennen(dateiname: str, inhalt: bytes) -> dict:
    """Dateityp, Zeichensatz, Trennzeichen und (bei XML) das Element je Artikel aus der Datei ableiten."""
    name = dateiname.lower()
    if name.endswith(".xlsx"):
        return {"dateityp": Dateityp.XLSX}
    if name.endswith(".xls"):
        return {"dateityp": Dateityp.XLS}
    if name.endswith(".xml") or inhalt.lstrip()[:5] == b"<?xml":
        try:
            wurzel = ET.fromstring(inhalt)
            namen = [e.tag.rsplit("}", 1)[-1] for e in wurzel if len(e) or e.attrib]
            satz = max(set(namen), key=namen.count) if namen else ""
        except ET.ParseError:
            satz = ""
        return {"dateityp": Dateityp.XML, "xml_satz": satz}
    try:
        inhalt.decode("utf-8-sig")
        zeichensatz = "utf-8"
        zeilen = inhalt.decode("utf-8-sig").splitlines()[:10]
    except UnicodeDecodeError:
        zeichensatz, zeilen = "cp1252", inhalt.decode("cp1252").splitlines()[:10]
    kopf = "\n".join(zeilen)
    trenner = max((";", "\t", ",", "|"), key=kopf.count)
    return {"dateityp": Dateityp.CSV, "zeichensatz": zeichensatz, "trennzeichen": "\\t" if trenner == "\t" else trenner,
            "textbegrenzer": '"'}


def muster_profil(lieferant, erkannt: dict, **kw) -> Preislistenprofil:
    """Ungespeichertes Profil, mit dem sich die Datei lesen lässt (Assistent, Vorschau, Artikelpreise)."""
    vorgabe = {"dateityp": Dateityp.CSV, "zeichensatz": "utf-8", "trennzeichen": ";", "textbegrenzer": '"',
               "kopfzeile": True, "dezimaltrennzeichen": ","}
    return Preislistenprofil(lieferant=lieferant, **{**vorgabe, **erkannt, **kw})


def spalten_und_beispiele(profil, inhalt: bytes, anzahl: int = 6):
    """(Spaltennamen, Beispielzeilen). Ohne Kopfzeile heißen die Spalten 'Spalte 1', 'Spalte 2' …"""
    if profil.dateityp == Dateityp.XML:
        try:
            wurzel = ET.fromstring(inhalt)
        except ET.ParseError as fehler:
            raise PreislistenFehler(f"XML nicht lesbar: {fehler}")
        saetze = [e for e in wurzel.iter() if e.tag.rsplit("}", 1)[-1] == profil.xml_satz][:anzahl]
        if not saetze:
            raise PreislistenFehler("Das Element je Artikel wurde in der XML-Datei nicht gefunden.")
        namen = list(dict.fromkeys([k.rsplit("}", 1)[-1] for s in saetze for k in s.attrib]
                                   + [c.tag.rsplit("}", 1)[-1] for s in saetze for c in s]))
        beispiele = []
        for s in saetze:
            werte = {k.rsplit("}", 1)[-1]: v for k, v in s.attrib.items()}
            werte.update({c.tag.rsplit("}", 1)[-1]: (c.text or "").strip() for c in s})
            beispiele.append([werte.get(n, "") for n in namen])
        return namen, beispiele
    zeilen = tabelle(profil, inhalt)
    if not zeilen:
        raise PreislistenFehler("Die Datei ist leer.")
    breite = max(len(z) for z in zeilen[:50])
    if profil.kopfzeile:
        namen = [text(c) or f"Spalte {i + 1}" for i, c in enumerate(zeilen[0])] + [f"Spalte {i + 1}" for i in range(len(zeilen[0]), breite)]
        daten = zeilen[1:]
    else:
        namen, daten = [f"Spalte {i + 1}" for i in range(breite)], zeilen
    return namen, [[text(c) for c in z] for z in daten[:anzahl]]


def spalten_raten(namen: list) -> dict:
    """Passende Spalte je Feld anhand gebräuchlicher Überschriften vorschlagen."""
    ergebnis, belegt = {}, set()
    for feld, kandidaten in VORSCHLAEGE.items():
        for kandidat in kandidaten:
            treffer = next((n for n in namen if n.strip().lower() == kandidat and n not in belegt), None)
            if treffer:
                ergebnis[feld] = treffer
                belegt.add(treffer)
                break
    return ergebnis


def spaltenangabe(profil, auswahl: str) -> str:
    """Was im Profil gespeichert wird: Überschrift (mit Kopfzeile bzw. XML) oder die Spaltennummer."""
    if not auswahl:
        return ""
    if profil.kopfzeile or profil.dateityp == Dateityp.XML:
        return auswahl
    return auswahl.replace("Spalte ", "")


# ---- Artikelpreise (EK/VK) aus Excel/CSV --------------------------------------------------------------

PREISFELDER = {"ek": "Durchschnitts-EK (netto)", "vk1": "VK1", "vk2": "VK2", "vk3": "VK3"}
SCHLUESSEL = {"nummer": "Artikelnummer", "herstellernummer": "Herstellernummer", "ean": "EAN"}


def artikelpreise(profil, inhalt: bytes, schluessel: str, spalte_schluessel: str, felder: dict,
                  brutto: bool = False, anwenden: bool = False) -> dict:
    """Liest Preise je Artikel (Schlüssel: Artikelnummer, Herstellernummer oder EAN) und schreibt sie bei anwenden=True.
    felder: {'ek': Spalte, 'vk1': Spalte, ...}; VK-Preise werden bei brutto=True auf netto umgerechnet."""
    if schluessel not in SCHLUESSEL:
        raise PreislistenFehler("Unbekannter Schlüssel.")
    if profil.dateityp == Dateityp.XML:
        raise PreislistenFehler("XML wird für Artikelpreise nicht unterstützt.")
    namen, _ = spalten_und_beispiele(profil, inhalt, 1)
    zeilen = tabelle(profil, inhalt) if profil.dateityp != Dateityp.XML else []
    start = 1 if profil.kopfzeile else 0

    def index(name):
        if name not in namen:
            raise PreislistenFehler(f"Spalte '{name}' nicht in der Datei.")
        return namen.index(name)

    i_schluessel = index(spalte_schluessel)
    i_felder = {f: index(s) for f, s in felder.items() if s}
    if not i_felder:
        raise PreislistenFehler("Bitte mindestens eine Preisspalte zuordnen.")
    ergebnis = {"gelesen": 0, "geaendert": 0, "unveraendert": 0, "nicht_gefunden": 0, "fehler": 0, "zeilen": []}

    def merke(eintrag):
        if len(ergebnis["zeilen"]) < 200:
            ergebnis["zeilen"].append(eintrag)

    for nr, z in enumerate(zeilen[start:], start=start + 1):
        if not any(text(c) for c in z):
            continue
        ergebnis["gelesen"] += 1
        wert = text(z[i_schluessel]) if i_schluessel < len(z) else ""
        artikel = Artikel.objects.filter(**{"ean" if schluessel == "ean" else f"{schluessel}__iexact": wert}).select_related("steuersatz").first() if wert else None
        if artikel is None:
            ergebnis["nicht_gefunden"] += 1
            merke({"zeile": nr, "schluessel": wert, "grund": "Artikel nicht gefunden"})
            continue
        neu = {}
        try:
            for feld, i in i_felder.items():
                roh = text(z[i]) if i < len(z) else ""
                if roh == "":
                    continue
                preis = zahl(roh, profil.dezimaltrennzeichen)
                if brutto and feld != "ek":
                    preis = preis / (1 + artikel.steuersatz.prozent / 100)
                if preis < 0:
                    raise ValueError("Preis ist negativ.")
                neu[feld] = preis.quantize(Decimal("0.0001") if feld == "ek" else Decimal("0.01"))
        except ValueError as grund:
            ergebnis["fehler"] += 1
            merke({"zeile": nr, "schluessel": wert, "grund": str(grund)})
            continue
        aenderungen = {}
        for feld, preis in neu.items():
            attribut = "durchschnitts_ek" if feld == "ek" else feld
            alt = getattr(artikel, attribut)
            if alt != preis:
                aenderungen[attribut] = (alt, preis)
        if not aenderungen:
            ergebnis["unveraendert"] += 1
            continue
        ergebnis["geaendert"] += 1
        merke({"zeile": nr, "schluessel": wert, "artikel": artikel.nummer,
               "aenderungen": ", ".join(f"{PREISFELDER['ek' if k == 'durchschnitts_ek' else k]}: {a if a is not None else '–'} → {n}"
                                        for k, (a, n) in aenderungen.items())})
        if anwenden:
            for attribut, (_, n) in aenderungen.items():
                setattr(artikel, attribut, n)
            artikel.save()
    return ergebnis
