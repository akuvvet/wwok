"""Gemeinsame Bausteine für die PDF-Ausdrucke (Belege, Mahnungen, Bestellungen, Werkstatt)."""
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.platypus import Image, Spacer


def logo_elemente(mandant) -> list:
    """Logo des Mandanten oben rechts (höchstens 60 x 22 mm); ohne Logo oder bei fehlender Datei leer, der Druck läuft weiter."""
    if not mandant.logo:
        return []
    try:
        ImageReader(mandant.logo.path)  # liest die Datei jetzt, damit ein fehlendes Bild hier und nicht beim Aufbau scheitert
        return [Image(mandant.logo.path, width=60 * mm, height=22 * mm, kind="proportional", hAlign="RIGHT"), Spacer(1, 3 * mm)]
    except Exception:
        return []
