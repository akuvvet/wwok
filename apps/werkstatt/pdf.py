"""Annahmeschein und Abholschein als PDF."""
from decimal import Decimal
from io import BytesIO
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from apps.kern.pdfhilfe import logo_elemente
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from apps.verkauf.models import runden
from apps.verkauf.pdf import absatz, euro, zahl


def schein_pdf(reparatur, abholung=False) -> bytes:
    m, k = reparatur.mandant, reparatur.kunde
    basis = getSampleStyleSheet()["Normal"]
    normal = ParagraphStyle("n", parent=basis, fontName="Helvetica", fontSize=10, leading=14)
    klein = ParagraphStyle("k", parent=normal, fontSize=7.5, leading=10, textColor=colors.grey)
    titel = ParagraphStyle("t", parent=normal, fontName="Helvetica-Bold", fontSize=15, leading=19)

    def seite(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(colors.grey)
        canvas.drawCentredString(A4[0] / 2, 16 * mm, f"{m.firmenname} · {m.strasse} · {m.plz} {m.ort} · {m.telefon} · {m.email}")
        canvas.restoreState()

    puffer = BytesIO()
    doc = SimpleDocTemplate(puffer, pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm, topMargin=15 * mm,
                            bottomMargin=28 * mm, title=reparatur.nummer, author=m.firmenname)
    anschrift = "\n".join(t for t in (k.name1, k.name2, k.strasse, f"{k.plz} {k.ort}") if t)
    daten = [["Auftragsnummer", reparatur.nummer], ["Angenommen am", reparatur.angenommen_am.strftime("%d.%m.%Y")],
             ["Gerät", reparatur.geraet], ["Seriennummer", reparatur.seriennummer or "–"],
             ["Zubehör", reparatur.zubehoer or "–"], ["Garantiefall", "ja" if reparatur.garantie else "nein"]]
    tab = Table([[absatz(a, klein), absatz(b, normal)] for a, b in daten], colWidths=[38 * mm, 120 * mm])
    inhalt = [*logo_elemente(m), absatz(f"{m.firmenname} · {m.strasse} · {m.plz} {m.ort}", klein), Spacer(1, 4 * mm),
              absatz(anschrift, normal), Spacer(1, 8 * mm),
              Paragraph("Abholschein" if abholung else "Annahmeschein Reparatur", titel), Spacer(1, 4 * mm), tab,
              Spacer(1, 5 * mm), absatz("Fehlerbeschreibung:", klein), absatz(reparatur.fehler, normal), Spacer(1, 5 * mm)]
    if abholung and reparatur.positionen.exists():
        zeilen = [["Position", "Menge", "Preis", "Netto"]]
        for p in reparatur.positionen.all():
            zeilen.append([absatz(p.bezeichnung, normal), zahl(p.menge), euro(p.einzelpreis), euro(p.netto)])
        netto = reparatur.summe_netto
        steuer = sum((runden(p.netto * p.steuer_prozent / 100) for p in reparatur.positionen.all()), Decimal("0"))
        if reparatur.garantie:
            zeilen.append([absatz("Garantiefall – keine Berechnung", normal), "", "", euro(Decimal("0"))])
        else:
            zeilen += [["", "", "Netto", euro(netto)], ["", "", "Steuer", euro(steuer)], ["", "", "Gesamt", euro(netto + steuer)]]
        t = Table(zeilen, colWidths=[90 * mm, 20 * mm, 30 * mm, 30 * mm], repeatRows=1)
        t.setStyle(TableStyle([("FONT", (0, 0), (-1, -1), "Helvetica", 9), ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
                               ("LINEBELOW", (0, 0), (-1, 0), 0.8, colors.black)]))
        inhalt += [t, Spacer(1, 6 * mm)]
    inhalt += [Spacer(1, 14 * mm), absatz("Ich bestätige, " + ("das Gerät vollständig und in funktionsfähigem Zustand zurückerhalten zu haben." if abholung
               else "das oben beschriebene Gerät zur Reparatur übergeben zu haben.") , normal), Spacer(1, 16 * mm),
               absatz("_______________________________            _______________________________\nDatum, Unterschrift Kunde                                         Datum, Unterschrift " + escape(m.kurzname), klein)]
    doc.build(inhalt, onFirstPage=seite, onLaterPages=seite)
    return puffer.getvalue()
