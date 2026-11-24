"""Mahnung als PDF."""
from io import BytesIO
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from apps.kern.pdfhilfe import logo_elemente
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from apps.verkauf.pdf import absatz, euro


def mahnung_text(mahnung) -> str:
    from .mahntext import text_fuer
    return text_fuer(mahnung)


def fusszeile(m):
    """Seitenfuß mit Firmendaten des Mandanten (wie auf Mahnungen)."""
    def seite(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(colors.grey)
        zeilen = [" · ".join(t for t in (m.firmenname, m.strasse, f"{m.plz} {m.ort}".strip()) if t),
                  " · ".join(t for t in (f"USt-ID {m.ust_id}" if m.ust_id else "", f"IBAN {m.iban}" if m.iban else "",
                                         f"BIC {m.bic}" if m.bic else "") if t),
                  " · ".join(t for t in (m.telefon, m.email) if t)]
        for i, zeile in enumerate(zeilen):
            canvas.drawCentredString(A4[0] / 2, 16 * mm - i * 3.5 * mm, zeile)
        canvas.drawRightString(A4[0] - 20 * mm, 16 * mm - 3 * 3.5 * mm, f"Seite {doc.page}")
        canvas.restoreState()
    return seite


def kontoauszug_pdf(mandant, partner, art, zeilen, saldo, saldo_text, stichtag=None, zeitraum_text="") -> bytes:
    """Kontoauszug (Kunde oder Lieferant) als PDF: Anschrift, Tabelle Datum/Buchung/Betrag/Saldo, Schlusssaldo.
    `art` ist „Kunden“ oder „Lieferanten“; Verrechnungen (Betrag None) stehen in Klammern ohne Saldowirkung."""
    from django.utils import timezone
    m, stichtag = mandant, stichtag or timezone.localdate()
    basis = getSampleStyleSheet()["Normal"]
    normal = ParagraphStyle("n", parent=basis, fontName="Helvetica", fontSize=10, leading=14)
    zelle = ParagraphStyle("z", parent=normal, fontSize=9, leading=11)
    klein = ParagraphStyle("k", parent=normal, fontSize=7.5, leading=10, textColor=colors.grey)
    titel = ParagraphStyle("t", parent=normal, fontName="Helvetica-Bold", fontSize=14, leading=18)
    puffer = BytesIO()
    doc = SimpleDocTemplate(puffer, pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm, topMargin=15 * mm,
                            bottomMargin=28 * mm, title=f"{art}kontoauszug {partner.nummer}", author=m.firmenname)
    anschrift = "\n".join(t for t in (partner.name1, partner.name2, partner.strasse,
                                      f"{partner.plz} {partner.ort}".strip()) if t)
    absender = " · ".join(t for t in (m.firmenname, m.strasse, f"{m.plz} {m.ort}".strip()) if t)
    inhalt = [*logo_elemente(m), absatz(absender, klein), Spacer(1, 4 * mm),
              absatz(anschrift, normal), Spacer(1, 6 * mm),
              absatz(f"Stand: {stichtag:%d.%m.%Y} · {art}-Nr. {partner.nummer}"
                     + (f" · Zeitraum: {zeitraum_text}" if zeitraum_text else ""), klein), Spacer(1, 4 * mm),
              Paragraph(escape(f"{art}kontoauszug"), titel), Spacer(1, 5 * mm)]
    daten = [["Datum", "Buchung", "Betrag", "Saldo"]]
    for z in zeilen:
        if z.get("anfang"):  # Anfangssaldo des Zeitraums
            betrag = ""
        else:
            betrag = f"({euro(z['info'])})" if z["betrag"] is None else euro(z["betrag"])
        daten.append([z["datum"].strftime("%d.%m.%Y"), absatz(z["text"], zelle), betrag, euro(z["saldo"])])
    if len(daten) == 1:
        daten.append(["", absatz("Keine Buchungen.", zelle), "", ""])
    daten.append(["", absatz(saldo_text, ParagraphStyle("zb", parent=zelle, fontName="Helvetica-Bold")), "", euro(saldo)])
    t = Table(daten, colWidths=[22 * mm, 96 * mm, 26 * mm, 26 * mm], repeatRows=1)
    t.setStyle(TableStyle([("FONT", (0, 0), (-1, -1), "Helvetica", 9), ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 9),
                           ("ALIGN", (2, 0), (3, -1), "RIGHT"), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                           ("LINEBELOW", (0, 0), (-1, 0), 0.8, colors.black),
                           ("FONT", (3, -1), (3, -1), "Helvetica-Bold", 9), ("LINEABOVE", (0, -1), (-1, -1), 0.8, colors.black)]))
    inhalt.append(t)
    inhalt += [Spacer(1, 4 * mm), absatz("Beträge in Klammern sind Verrechnungen zwischen Belegen und ändern den Saldo nicht.", klein)]
    seite = fusszeile(m)
    doc.build(inhalt, onFirstPage=seite, onLaterPages=seite)
    return puffer.getvalue()


def zahlungsavis_pdf(mandant, lieferant, avis) -> bytes:
    """Zahlungsavis an den Lieferanten (Wunsch des Nutzers 20.11.2026): welche Rechnungen mit welchem Betrag bezahlt werden,
    mit verrechneten Gutschriften und Skonto; Summe = Überweisungsbetrag."""
    m, datum, summe = mandant, avis["datum"], avis["summe"]
    basis = getSampleStyleSheet()["Normal"]
    normal = ParagraphStyle("n", parent=basis, fontName="Helvetica", fontSize=10, leading=14)
    zelle = ParagraphStyle("z", parent=normal, fontSize=9, leading=11)
    fett = ParagraphStyle("zb", parent=zelle, fontName="Helvetica-Bold")
    klein = ParagraphStyle("k", parent=normal, fontSize=7.5, leading=10, textColor=colors.grey)
    titel = ParagraphStyle("t", parent=normal, fontName="Helvetica-Bold", fontSize=14, leading=18)
    puffer = BytesIO()
    doc = SimpleDocTemplate(puffer, pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm, topMargin=15 * mm,
                            bottomMargin=28 * mm, title=f"Zahlungsavis {lieferant.nummer}", author=m.firmenname)
    anschrift = "\n".join(t for t in (lieferant.name1, lieferant.name2, lieferant.strasse,
                                      f"{lieferant.plz} {lieferant.ort}".strip()) if t)
    absender = " · ".join(t for t in (m.firmenname, m.strasse, f"{m.plz} {m.ort}".strip()) if t)
    konto = f" auf Ihr Konto IBAN {lieferant.iban}" if lieferant.iban else ""
    inhalt = [*logo_elemente(m), absatz(absender, klein), Spacer(1, 4 * mm), absatz(anschrift, normal), Spacer(1, 6 * mm),
              absatz(f"Datum: {timezone_heute():%d.%m.%Y} · Lieferanten-Nr. {lieferant.nummer}", klein), Spacer(1, 4 * mm),
              Paragraph("Zahlungsavis", titel), Spacer(1, 5 * mm),
              absatz(f"Sehr geehrte Damen und Herren,\n\nam {datum:%d.%m.%Y} überweisen wir Ihnen{konto} den Betrag von "
                     f"{euro(summe['zahlung'])} € für die folgenden Rechnungen:", normal), Spacer(1, 5 * mm)]
    daten = [["Rechnung", "Datum", "Betrag", "Gutschrift/Teilz.", "Skonto/Abzug", "Zahlung"]]
    for z in avis["zeilen"]:
        r = z["rechnung"]
        gs = "\n".join(f"{nr}: {euro(b)}" for nr, b in z["gutschriften"])
        hinweis = chr(10) + z["hinweis"] if z.get("hinweis") else ""  # Teilzahlung: Rest bleibt offen
        daten.append([absatz(r.rechnungsnummer + hinweis, zelle), r.datum.strftime("%d.%m.%Y"), euro(z["brutto"]),
                      absatz(gs, zelle) if gs else "", euro(z["skonto"]) if z["skonto"] else "", euro(z["zahlung"])])
    daten.append([absatz("Summe", fett), "", euro(summe["brutto"]), euro(summe["gutschriften"]) if summe["gutschriften"] else "",
                  euro(summe["skonto"]) if summe["skonto"] else "", euro(summe["zahlung"])])
    t = Table(daten, colWidths=[38 * mm, 22 * mm, 25 * mm, 32 * mm, 26 * mm, 27 * mm], repeatRows=1)
    t.setStyle(TableStyle([("FONT", (0, 0), (-1, -1), "Helvetica", 9), ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 9),
                           ("ALIGN", (2, 0), (-1, -1), "RIGHT"), ("ALIGN", (3, 1), (3, -1), "LEFT"), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                           ("LINEBELOW", (0, 0), (-1, 0), 0.8, colors.black), ("LINEABOVE", (0, -1), (-1, -1), 0.8, colors.black),
                           ("FONT", (0, -1), (-1, -1), "Helvetica-Bold", 9)]))
    inhalt += [t, Spacer(1, 6 * mm),
               absatz("Gutschrift/Teilz.: mit der Rechnung verrechnete Gutschriften von Ihnen bzw. schon geleistete Teilzahlungen. Skonto/Abzug: Skonto innerhalb der "
                      "Zahlungsfrist bzw. sonstige Differenz zum Rechnungsbetrag.", klein),
               Spacer(1, 6 * mm), absatz(f"Mit freundlichen Grüßen\n{m.firmenname}", normal)]
    seite = fusszeile(m)
    doc.build(inhalt, onFirstPage=seite, onLaterPages=seite)
    return puffer.getvalue()


def timezone_heute():
    from django.utils import timezone
    return timezone.localdate()


def mahnung_pdf(mahnung) -> bytes:
    beleg, m = mahnung.beleg, mahnung.beleg.mandant
    basis = getSampleStyleSheet()["Normal"]
    normal = ParagraphStyle("n", parent=basis, fontName="Helvetica", fontSize=10, leading=14)
    klein = ParagraphStyle("k", parent=normal, fontSize=7.5, leading=10, textColor=colors.grey)
    titel = ParagraphStyle("t", parent=normal, fontName="Helvetica-Bold", fontSize=14, leading=18)

    def seite(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(colors.grey)
        zeilen = [f"{m.firmenname} · {m.strasse} · {m.plz} {m.ort}",
                  " · ".join(t for t in (f"USt-ID {m.ust_id}" if m.ust_id else "", f"IBAN {m.iban}" if m.iban else "",
                                         f"BIC {m.bic}" if m.bic else "") if t),
                  " · ".join(t for t in (m.telefon, m.email) if t)]
        for i, zeile in enumerate(zeilen):
            canvas.drawCentredString(A4[0] / 2, 16 * mm - i * 3.5 * mm, zeile)
        canvas.restoreState()

    puffer = BytesIO()
    doc = SimpleDocTemplate(puffer, pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm, topMargin=15 * mm,
                            bottomMargin=28 * mm, title=str(mahnung), author=m.firmenname)
    k = beleg.kunde
    anschrift = beleg.rechnungsanschrift or "\n".join(t for t in (k.name1, k.name2, k.strasse, f"{k.plz} {k.ort}") if t)
    gesamt = mahnung.offener_betrag + mahnung.gebuehr
    inhalt = [*logo_elemente(m), absatz(f"{m.firmenname} · {m.strasse} · {m.plz} {m.ort}", klein), Spacer(1, 4 * mm),
              absatz(anschrift, normal), Spacer(1, 6 * mm),
              absatz(f"Datum: {mahnung.datum:%d.%m.%Y} · Kunden-Nr. {k.nummer}", klein), Spacer(1, 4 * mm),
              Paragraph(escape(f"{mahnung.bezeichnung} zu Rechnung {beleg.nummer}"), titel), Spacer(1, 6 * mm),
              absatz(mahnung_text(mahnung), normal), Spacer(1, 6 * mm)]
    zeilen = [["Rechnung", f"{beleg.nummer} vom {beleg.belegdatum:%d.%m.%Y}"],
              ["Rechnungsbetrag", euro(beleg.summe_brutto)], ["Offener Betrag", euro(mahnung.offener_betrag)]]
    if mahnung.gebuehr:
        zeilen.append(["Mahngebühr", euro(mahnung.gebuehr)])
    zeilen += [["Zu zahlen", euro(gesamt)], ["Zahlbar bis", mahnung.frist.strftime("%d.%m.%Y")]]
    t = Table(zeilen, colWidths=[45 * mm, 60 * mm], hAlign="LEFT")
    t.setStyle(TableStyle([("FONT", (0, 0), (-1, -1), "Helvetica", 10), ("ALIGN", (1, 1), (1, -1), "RIGHT"),
                           ("FONT", (0, 4), (-1, 4), "Helvetica-Bold", 10), ("LINEABOVE", (0, 4), (-1, 4), 0.8, colors.black)]))
    inhalt.append(t)
    doc.build(inhalt, onFirstPage=seite, onLaterPages=seite)
    return puffer.getvalue()
