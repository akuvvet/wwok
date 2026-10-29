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
