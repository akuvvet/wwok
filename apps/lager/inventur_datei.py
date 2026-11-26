"""Zählliste drucken, exportieren und gezählte Mengen aus CSV/Excel einlesen (Wunsch des Nutzers 26.11.2026).

Druck und Export enthalten immer die ganze gefilterte Liste (nicht nur die angezeigte Seite). Der Import bucht nichts selbst:
Er liefert die gezählten Mengen je Artikel für eine Vorschau, gebucht wird erst dort mit „Differenzen buchen“."""
import csv
import io
from decimal import Decimal, InvalidOperation

from django.utils import timezone
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from apps.einkauf.preislisten import text
from apps.verkauf.pdf import absatz

KOPF = ["Lagerplatz", "Artikel", "Bezeichnung", "Soll", "gezählt"]
SPALTE_ARTIKEL = {"artikel", "artikelnummer", "artikelnr", "artikelnr.", "art.-nr.", "art.nr.", "artnr", "nummer", "nr", "nr."}
SPALTE_GEZAEHLT = {"gezählt", "gezaehlt", "gezählte menge", "gezaehlte menge", "ist", "istmenge", "ist-menge", "ist menge",
                   "zählung", "zaehlung", "menge", "anzahl"}


class InventurImportFehler(Exception):
    pass


def _menge(wert: Decimal) -> str:
    return f"{wert.normalize():f}".replace(".", ",")


def csv_export(zeilen) -> bytes:
    """Semikolon und Dezimalkomma, UTF-8 mit BOM: öffnet sich in Excel direkt richtig; „gezählt“ ist zum Ausfüllen leer."""
    puffer = io.StringIO()
    w = csv.writer(puffer, delimiter=";", lineterminator="\r\n")
    w.writerow(KOPF)
    for z in zeilen:
        w.writerow([z["platz"], z["artikel"].nummer, z["artikel"].bezeichnung, _menge(z["soll"]), ""])
    return ("﻿" + puffer.getvalue()).encode("utf-8")


def xlsx_export(zeilen, mandant) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font
    buch = Workbook()
    blatt = buch.active
    blatt.title = "Zählliste"
    blatt.append(KOPF)
    for zelle in blatt[1]:
        zelle.font = Font(bold=True)
    for z in zeilen:
        blatt.append([z["platz"], z["artikel"].nummer, z["artikel"].bezeichnung, float(z["soll"]), None])
    for spalte, breite in zip("ABCDE", (14, 16, 60, 10, 10)):
        blatt.column_dimensions[spalte].width = breite
    blatt.freeze_panes = "A2"
    puffer = io.BytesIO()
    buch.save(puffer)
    return puffer.getvalue()


def zaehlliste_pdf(zeilen, mandant, filter_text="") -> bytes:
    """Nur die Liste: Kopf mit Mandant und Datum, Tabelle mit leerer Spalte zum Eintragen, unten Gezählt von/am."""
    basis = getSampleStyleSheet()["Normal"]
    normal = ParagraphStyle("n", parent=basis, fontName="Helvetica", fontSize=9, leading=11)
    klein = ParagraphStyle("k", parent=normal, fontSize=8, textColor=colors.grey)
    fett = ParagraphStyle("f", parent=normal, fontName="Helvetica-Bold")
    titel = ParagraphStyle("t", parent=normal, fontName="Helvetica-Bold", fontSize=14, leading=18)
    jetzt = timezone.localtime()

    def seite(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(colors.grey)
        canvas.drawString(15 * mm, 10 * mm, f"Zählliste {mandant} · {jetzt:%d.%m.%Y %H:%M}")
        canvas.drawRightString(A4[0] - 15 * mm, 10 * mm, f"Seite {doc.page}")
        canvas.restoreState()

    puffer = io.BytesIO()
    doc = SimpleDocTemplate(puffer, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=15 * mm, bottomMargin=18 * mm,
                            title=f"Zählliste {mandant}", author=str(mandant))
    inhalt = [Paragraph(f"Zählliste · Mandant {mandant}", titel),
              absatz(f"Stand {jetzt:%d.%m.%Y %H:%M} · {len(zeilen)} Artikel" + (f" · {filter_text}" if filter_text else ""), klein),
              Spacer(1, 4 * mm)]
    daten = [[absatz(t, fett) for t in KOPF]]
    for z in zeilen:
        daten.append([absatz(z["platz"], normal), absatz(z["artikel"].nummer, normal), absatz(z["artikel"].bezeichnung, normal),
                      _menge(z["soll"]), ""])
    t = Table(daten, colWidths=[22 * mm, 24 * mm, 100 * mm, 16 * mm, 18 * mm], repeatRows=1)
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("FONT", (0, 1), (-1, -1), "Helvetica", 9),
                           ("ALIGN", (3, 0), (3, -1), "RIGHT"), ("LINEBELOW", (0, 0), (-1, 0), 0.8, colors.black),
                           ("LINEBELOW", (0, 1), (-1, -1), 0.25, colors.lightgrey),
                           ("BOX", (4, 1), (4, -1), 0.5, colors.grey), ("INNERGRID", (4, 1), (4, -1), 0.5, colors.grey),
                           ("TOPPADDING", (0, 1), (-1, -1), 5), ("BOTTOMPADDING", (0, 1), (-1, -1), 5)]))
    inhalt += [t, Spacer(1, 12 * mm),
               absatz("Gezählt von: ______________________________     Datum: ______________     Unterschrift: ______________________",
                      normal)]
    doc.build(inhalt, onFirstPage=seite, onLaterPages=seite)
    return puffer.getvalue()


def _kopf(wert) -> str:
    return " ".join(text(wert).lower().split())


def _zahl(wert) -> Decimal:
    if isinstance(wert, (int, float)) and not isinstance(wert, bool):
        return Decimal(str(wert))
    t = text(wert).replace(" ", "").replace("\xa0", "")
    if "," in t:
        t = t.replace(".", "").replace(",", ".")
    try:
        d = Decimal(t)
    except InvalidOperation:
        raise ValueError(f"keine Zahl: „{text(wert)}“")
    if not d.is_finite():
        raise ValueError(f"keine Zahl: „{text(wert)}“")
    return d


def import_lesen(dateiname: str, inhalt: bytes):
    """Gezählte Mengen aus der Datei: ({artikelnummer: menge}, [Fehler], {artikelnummer: Anzahl Zeilen}).
    Die Kopfzeile muss eine Spalte für die Artikelnummer (Artikel, Artikelnummer, Nummer …) und eine für die Zählung (gezählt, Ist,
    Menge …) haben; die exportierte Zählliste passt so, wie sie ist. Leere Zählzellen werden übersprungen. Steht ein Artikel in
    mehreren Zeilen (z. B. an zwei Lagerplätzen gezählt), werden die Mengen addiert."""
    from apps.stammdaten.leistung_import import ImportFehler, datei_lesen
    try:
        zeilen = datei_lesen(dateiname, inhalt)
    except ImportFehler as fehler:
        raise InventurImportFehler(str(fehler))
    for i, zeile in enumerate(zeilen[:10]):  # Kopfzeile in den ersten Zeilen suchen (Titelzeilen davor erlaubt)
        namen = [_kopf(z) for z in zeile]
        sp_artikel = next((j for j, n in enumerate(namen) if n in SPALTE_ARTIKEL), None)
        sp_menge = next((j for j, n in enumerate(namen) if n in SPALTE_GEZAEHLT), None)
        if sp_artikel is not None and sp_menge is not None:
            break
    else:
        raise InventurImportFehler("Keine Kopfzeile gefunden: Die Datei braucht eine Spalte „Artikel“ (Artikelnummer) und eine "
                                   "Spalte „gezählt“ (oder „Ist“, „Menge“). Am einfachsten die exportierte Zählliste ausfüllen.")
    mengen, fehler, anzahl = {}, [], {}
    for nr, zeile in enumerate(zeilen[i + 1:], start=i + 2):
        nummer = text(zeile[sp_artikel]) if sp_artikel < len(zeile) else ""
        roh = zeile[sp_menge] if sp_menge < len(zeile) else ""
        if text(roh) == "":
            continue
        if not nummer:
            fehler.append(f"Zeile {nr}: Menge ohne Artikelnummer")
            continue
        try:
            menge = _zahl(roh)
        except ValueError as f:
            fehler.append(f"Zeile {nr} ({nummer}): {f}")
            continue
        if menge < 0:
            fehler.append(f"Zeile {nr} ({nummer}): negative Menge")
            continue
        mengen[nummer] = mengen.get(nummer, Decimal("0")) + menge
        anzahl[nummer] = anzahl.get(nummer, 0) + 1
    return mengen, fehler, anzahl
