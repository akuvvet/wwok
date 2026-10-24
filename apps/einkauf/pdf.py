"""Lieferantenbestellung als PDF."""
from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from apps.verkauf.pdf import absatz, euro, zahl


def bestellung_pdf(bestellung) -> bytes:
    m, lf = bestellung.mandant, bestellung.lieferant
    basis = getSampleStyleSheet()["Normal"]
    normal = ParagraphStyle("n", parent=basis, fontName="Helvetica", fontSize=9, leading=12)
    klein = ParagraphStyle("k", parent=normal, fontSize=7.5, leading=10, textColor=colors.grey)
    titel = ParagraphStyle("t", parent=normal, fontName="Helvetica-Bold", fontSize=14, leading=18)
    fett = ParagraphStyle("f", parent=normal, fontName="Helvetica-Bold")

    def seite(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(colors.grey)
        canvas.drawCentredString(A4[0] / 2, 16 * mm, f"{m.firmenname} · {m.strasse} · {m.plz} {m.ort} · {m.telefon} · {m.email}")
        canvas.restoreState()

    kundennr = next((d.unsere_kundennummer for d in lf.mandantendaten.all() if d.mandant_id == m.pk), "")
    puffer = BytesIO()
    doc = SimpleDocTemplate(puffer, pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm, topMargin=15 * mm,
                            bottomMargin=28 * mm, title=bestellung.nummer, author=m.firmenname)
    anschrift = "\n".join(t for t in (lf.name1, lf.name2, lf.strasse, f"{lf.plz} {lf.ort}") if t)
    daten = [["Bestellnummer", bestellung.nummer], ["Datum", bestellung.datum.strftime("%d.%m.%Y")]]
    if kundennr:
        daten.append(["Unsere Kundennr.", kundennr])
    rechts = Table([[absatz(a, klein), absatz(b, normal)] for a, b in daten], colWidths=[30 * mm, 40 * mm])
    kopf = Table([[absatz(anschrift, normal), rechts]], colWidths=[100 * mm, 70 * mm])
    kopf.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    inhalt = [absatz(f"{m.firmenname} · {m.strasse} · {m.plz} {m.ort}", klein), Spacer(1, 4 * mm), kopf, Spacer(1, 10 * mm),
              Paragraph(f"Bestellung {bestellung.nummer}", titel), Spacer(1, 4 * mm)]
    zeilen = [[absatz(t, fett) for t in ("Pos.", "Best.-Nr.", "Bezeichnung", "Menge", "EK netto", "Netto")]]
    for i, p in enumerate(bestellung.positionen.select_related("artikel"), start=1):
        zeilen.append([str(i), absatz(p.bestellnummer or p.nummer, normal), absatz(p.bezeichnung, normal),
                       zahl(p.menge), euro(p.ek), euro(p.netto)])
    t = Table(zeilen, colWidths=[10 * mm, 30 * mm, 70 * mm, 16 * mm, 22 * mm, 22 * mm], repeatRows=1)
    t.setStyle(TableStyle([("FONT", (0, 1), (-1, -1), "Helvetica", 9), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                           ("ALIGN", (3, 0), (-1, -1), "RIGHT"), ("LINEBELOW", (0, 0), (-1, 0), 0.8, colors.black),
                           ("LINEBELOW", (0, 1), (-1, -1), 0.25, colors.lightgrey)]))
    inhalt += [t, Spacer(1, 4 * mm), absatz(f"Summe netto: {euro(bestellung.summe_netto)}", fett)]
    if bestellung.bemerkung:
        inhalt += [Spacer(1, 4 * mm), absatz(bestellung.bemerkung, normal)]
    doc.build(inhalt, onFirstPage=seite, onLaterPages=seite)
    return puffer.getvalue()
