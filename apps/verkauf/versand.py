"""Paketaufkleber (PDF, A6) und Versand-Mail zu einer Versendung."""
import io

from reportlab.graphics.barcode import code128
from reportlab.lib.pagesizes import A6
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

from .mail import senden_an


def empfaenger_zeilen(beleg) -> list:
    """Lieferanschrift des Belegs, sonst Rechnungsanschrift, sonst Anschrift des Kunden."""
    text = (beleg.versandanschrift or beleg.rechnungsanschrift or "").strip()
    if text:
        return [z.strip() for z in text.splitlines() if z.strip()]
    k = beleg.kunde
    return [z for z in (k.name1, k.name2, k.strasse, f"{k.plz} {k.ort}".strip()) if z]


def aufkleber_pdf(versand) -> bytes:
    """Interner Paketaufkleber mit Empfänger, Absender, Belegnummer und Barcode der Sendungsnummer.
    (Das amtliche DHL-Label mit Frankierung kommt aus der DHL-Online-Frankierung bzw. später per API.)"""
    beleg, m = versand.beleg, versand.beleg.mandant
    puffer = io.BytesIO()
    breite, hoehe = A6
    c = canvas.Canvas(puffer, pagesize=A6)
    c.setFont("Helvetica", 7)
    absender = " · ".join(z for z in (m.firmenname, m.strasse, f"{m.plz} {m.ort}".strip()) if z)
    c.drawString(8 * mm, hoehe - 9 * mm, absender[:70])
    c.line(8 * mm, hoehe - 11 * mm, breite - 8 * mm, hoehe - 11 * mm)
    y = hoehe - 25 * mm
    for i, zeile in enumerate(empfaenger_zeilen(beleg)[:6]):
        c.setFont("Helvetica-Bold" if i == 0 else "Helvetica", 14 if i == 0 else 12)
        c.drawString(10 * mm, y, zeile[:34])
        y -= 7 * mm
    c.setFont("Helvetica", 9)
    c.drawString(8 * mm, 38 * mm, f"{versand.get_dienst_display()}  ·  {beleg.get_art_display()} {beleg.nummer or ''}".strip())
    if versand.gewicht_kg:
        c.drawString(8 * mm, 33 * mm, f"Gewicht: {versand.gewicht_kg} kg")
    if versand.sendungsnummer:
        c.drawString(8 * mm, 28 * mm, f"Sendungsnummer: {versand.sendungsnummer}")
        barcode = code128.Code128(versand.sendungsnummer, barHeight=14 * mm, barWidth=0.35 * mm)
        barcode.drawOn(c, 8 * mm, 10 * mm)
    c.showPage()
    c.save()
    return puffer.getvalue()


def versandmail_text(versand):
    from .mail import anrede
    beleg = versand.beleg
    link = f"\nSendungsverfolgung: {versand.tracking_url}" if versand.tracking_url else ""
    # Brief: die Marken-ID ist keine Sendungsnummer, eine Verfolgung gibt es nicht
    nummer = "" if versand.dienst == "dp" else f"Sendungsnummer: {versand.sendungsnummer or '–'}{link}\n"
    betreff = f"Ihre Bestellung wurde versendet – {beleg.mandant.firmenname}"
    text = (f"{anrede(beleg)},\n\n"
            f"Ihre Ware zu {beleg.get_art_display()} {beleg.nummer or ''} wurde am {versand.versendet_am:%d.%m.%Y} mit "
            f"{versand.get_dienst_display()} versendet.\n"
            f"{nummer}\n"
            f"Mit freundlichen Grüßen\n{beleg.mandant.firmenname}")
    return betreff, text


def versandmail_senden(versand, an: str, benutzer=None):
    from django.utils import timezone
    betreff, text = versandmail_text(versand)
    protokoll = senden_an(versand.beleg.mandant, an, betreff, text, bezug=f"Versand {versand.beleg.nummer}", benutzer=benutzer)
    versand.mail_gesendet = timezone.now()
    versand.save(update_fields=["mail_gesendet", "geaendert_am"])
    return protokoll
