"""Positionen aus einer CSV- oder Excel-Datei in einen Beleg einlesen. Die Spalten werden an der Kopfzeile erkannt.
Alles oder nichts: Bei einem Fehler in einer Zeile wird nichts übernommen. Preis, Menge und Zusatztext kommen aus der
Zeile, denn dieselbe Nummer darf mit verschiedenen Preisen vorkommen (Wochentag, Wochenende). Die Spalte pos zählt
nicht, die Positionen werden lückenlos neu nummeriert."""
import re
from decimal import Decimal

from django.db import transaction

from apps.einkauf.preislisten import text
from apps.stammdaten.leistung_import import ImportFehler, datei_lesen, geld, kopfname, zelle_pruefen
from apps.stammdaten.models import Artikel

from .models import Beleg, Belegart, BelegGesperrt, Position, runden

SPALTEN = {
    "nummer": ("nummer", "nr", "artikelnummer", "artikelnr", "artnr"),
    "bezeichnung": ("benennung", "bezeichnung", "artikelbezeichnung", "leistung"),
    "preis": ("netto", "einzelpreis", "einzelpreisnetto", "nettopreis", "preis", "vk", "vkpreis", "vknetto", "verkaufspreis"),
    "menge": ("menge", "anzahl"),
    "gesamt": ("gesamtnetto", "nettogesamt", "gesamt", "summe"),
    "rabatt": ("rabatt", "rabattprozent", "rabattin", "rabattsatz"),
    "zusatz": ("zusatzpos", "zusatztext", "zusatz"),
    "kennzeichen": ("kennzeichen", "typ"),
}
BELEGARTEN = (Belegart.ANGEBOT, Belegart.AUFTRAG, Belegart.RECHNUNG, Belegart.PROFORMA)
MAX_ZEILEN = 1000
MENGE_KOMMA = re.compile(r"\d+(,\d{1,3})?")
MENGE_PUNKT = re.compile(r"\d+\.\d{1,2}")


def spalten_finden(kopf) -> dict:
    namen = [kopfname(k) for k in kopf]
    spalten = {}
    for feld, moegliche in SPALTEN.items():
        for name in moegliche:
            if name in namen:
                spalten[feld] = namen.index(name)
                break
    if sum(name in SPALTEN["preis"] for name in namen) > 1:  # sonst würde z. B. Netto als Zeilenbetrag still zum Einzelpreis
        raise ImportFehler("Die Kopfzeile ist mehrdeutig: mehrere Preisspalten. Bitte den Einzelpreis netto und den Zeilenbetrag gesamtnetto nennen.")
    # Eine Preis- oder Rabattspalte unter fremdem Namen darf nicht still entfallen
    unbekannt = [text(k) for k, n in zip(kopf, namen)
                 if "preis" not in spalten and (any(w in n for w in ("preis", "vk")) or "netto" in n and "gesamt" not in n)
                 or "rabatt" not in spalten and any(w in n for w in ("rabatt", "nachlass"))]
    if unbekannt:
        raise ImportFehler(f"Spalte nicht erkannt: {', '.join(unbekannt)}. Bitte netto bzw. Rabatt als Überschrift verwenden.")
    fehlend = [n for n, feld in (("Nummer", "nummer"), ("Menge", "menge")) if feld not in spalten]
    if fehlend:
        raise ImportFehler(f"In der Kopfzeile fehlt: {', '.join(fehlend)}.")
    return spalten


def menge_lesen(wert) -> Decimal:
    """Menge größer 0 mit höchstens 3 Nachkommastellen. Rechenreste aus Excel-Formeln fallen weg; 1.000 ist mehrdeutig
    und wird abgelehnt."""
    if isinstance(wert, (int, float)):
        menge = Decimal(str(round(wert, 6)))
    else:
        t = text(wert)
        if MENGE_KOMMA.fullmatch(t):
            menge = Decimal(t.replace(",", "."))
        elif MENGE_PUNKT.fullmatch(t):
            menge = Decimal(t)
        else:
            raise ValueError(f"Menge ungültig: '{t}'")
    if not Decimal("0") < menge < Decimal("1E7") or menge != menge.quantize(Decimal("0.001")):
        raise ValueError(f"Menge ungültig: '{text(wert)}'")
    return menge


def _position(beleg, zelle, artikel_merken):
    """Baut die Position aus einer Zeile, ohne zu speichern."""
    from .views import vorschlag_preis
    nummer = text(zelle("nummer"))
    if not nummer:
        raise ValueError("Die Nummer fehlt.")
    schluessel = nummer.lower()
    if schluessel not in artikel_merken:
        artikel_merken[schluessel] = Artikel.objects.filter(nummer__iexact=nummer).select_related("steuersatz").first()
    artikel = artikel_merken[schluessel]
    if artikel is None:
        raise ValueError(f"Nummer {nummer} ist nicht angelegt.")
    if artikel.nicht_aktiv:
        raise ValueError(f"{nummer} ist nicht aktiv.")
    if artikel.typ == "G":
        raise ValueError(f"{nummer} ist eine Gruppe, Gruppen werden im Beleg erfasst.")
    kennzeichen = text(zelle("kennzeichen")).upper()
    if kennzeichen and kennzeichen != artikel.typ:
        raise ValueError(f"Kennzeichen {kennzeichen} passt nicht: {nummer} ist {artikel.get_typ_display()}.")
    bezeichnung = text(zelle("bezeichnung")) or artikel.bezeichnung
    if len(bezeichnung) > 255:
        raise ValueError("Die Bezeichnung ist zu lang.")
    preis = geld(zelle("preis")) if zelle("preis") != "" else vorschlag_preis(artikel, beleg.kunde)
    if preis is None:
        raise ValueError(f"Für {nummer} gibt es keinen Preis.")
    rabatt = geld(zelle("rabatt")) if zelle("rabatt") != "" else Decimal("0")
    if rabatt > 100:
        raise ValueError("Der Rabatt muss zwischen 0 und 100 % liegen.")
    p = Position(beleg=beleg, typ=artikel.typ, artikel=artikel, bezeichnung=bezeichnung, zusatztext=text(zelle("zusatz")),
                 menge=menge_lesen(zelle("menge")), einzelpreis=preis, rabatt_prozent=rabatt,
                 steuer_prozent=artikel.steuersatz.prozent)
    if zelle("gesamt") != "" and geld(zelle("gesamt")) != p.netto_gesamt:
        gesamt, gerechnet = (str(w).replace(".", ",") for w in (geld(zelle("gesamt")), p.netto_gesamt))
        raise ValueError(f"Gesamtnetto {gesamt} passt nicht zu Netto × Menge = {gerechnet}.")
    return p


def positionen_importieren(beleg, dateiname: str, inhalt: bytes) -> tuple:
    """Hängt die Positionen der Datei an den Beleg an. Gibt (Anzahl, Netto der neuen Positionen) zurück."""
    if beleg.positionen_gesperrt:
        raise BelegGesperrt(f"{beleg} ist abgeschlossen – Positionen lassen sich nicht mehr ändern.")
    if beleg.art not in BELEGARTEN:
        raise ImportFehler(f"In eine {beleg.get_art_display()} lassen sich keine Positionen importieren.")
    zeilen = datei_lesen(dateiname, inhalt)
    if not zeilen:
        raise ImportFehler("Die Datei ist leer.")
    if sum(1 for z in zeilen[1:] if any(text(w) for w in z)) > MAX_ZEILEN:
        raise ImportFehler(f"Die Datei hat mehr als {MAX_ZEILEN} Zeilen.")
    spalten = spalten_finden(zeilen[0])

    fehler, neue, artikel_merken = [], [], {}
    for nr, zeile in enumerate(zeilen[1:], start=2):
        if not any(text(w) for w in zeile):
            continue
        if any(text(w) for w in zeile[len(zeilen[0]):]):
            fehler.append(f"Zeile {nr}: Mehr Spalten als in der Kopfzeile. Bitte das Trennzeichen prüfen.")
            continue

        def zelle(feld):
            i = spalten.get(feld)
            wert = zeile[i] if i is not None and i < len(zeile) else ""
            return wert.strip() if isinstance(wert, str) else ("" if wert is None else wert)

        try:
            for feld in spalten:
                zelle_pruefen(feld, zelle(feld))
            neue.append(_position(beleg, zelle, artikel_merken))
        except ValueError as e:
            fehler.append(f"Zeile {nr}: {e}")
    if fehler:
        raise ImportFehler("Nichts importiert. Bitte diese Zeilen in der Datei korrigieren:", fehler)
    if not neue:
        raise ImportFehler("Die Datei enthält keine Positionen.")
    with transaction.atomic():
        # Einlesen dauert: Die Rechnung kann inzwischen in einem anderen Tab abgeschlossen sein
        beleg.abgeschlossen = Beleg.objects.select_for_update().values_list("abgeschlossen", flat=True).get(pk=beleg.pk)
        if beleg.positionen_gesperrt:
            raise BelegGesperrt(f"{beleg} ist abgeschlossen – Positionen lassen sich nicht mehr ändern.")
        for p in neue:
            p.save()
        beleg.berechne()
    return len(neue), runden(sum((p.netto_gesamt for p in neue), Decimal("0")))
