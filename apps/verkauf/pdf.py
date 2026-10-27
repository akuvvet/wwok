"""Belegdruck als PDF (DIN A4, Positionen, Summen, Zahlungstext, Firmendaten im Fuß)."""
from io import BytesIO
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .models import Belegart


def euro(wert) -> str:
    return f"{wert:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".") + " €"


def zahl(wert) -> str:
    return f"{wert.normalize():f}".replace(".", ",")


def absatz(text, stil):
    return Paragraph(escape(text or "").replace("\n", "<br/>"), stil)


def beleg_pdf(beleg) -> bytes:
    m = beleg.mandant
    basis = getSampleStyleSheet()["Normal"]
    normal = ParagraphStyle("n", parent=basis, fontName="Helvetica", fontSize=9, leading=12)
    klein = ParagraphStyle("k", parent=normal, fontSize=7.5, leading=10, textColor=colors.grey)
    titel = ParagraphStyle("t", parent=normal, fontName="Helvetica-Bold", fontSize=14, leading=18)
    fett = ParagraphStyle("f", parent=normal, fontName="Helvetica-Bold")
    entwurf = not beleg.nummer or (beleg.art in (Belegart.RECHNUNG, Belegart.GUTSCHRIFT) and not beleg.abgeschlossen)

    def seite(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(colors.grey)
        zeilen = [
            f"{m.firmenname} · {m.strasse} · {m.plz} {m.ort}",
            " · ".join(t for t in (f"USt-ID {m.ust_id}" if m.ust_id else "",
                                   f"Steuernr. {m.steuernummer}" if m.steuernummer else "",
                                   f"IBAN {m.iban}" if m.iban else "", f"BIC {m.bic}" if m.bic else "") if t),
            " · ".join(t for t in (m.telefon, m.email) if t),
        ]
        for i, zeile in enumerate(zeilen):
            canvas.drawCentredString(A4[0] / 2, 16 * mm - i * 3.5 * mm, zeile)
        canvas.drawRightString(A4[0] - 20 * mm, A4[1] - 12 * mm, f"Seite {doc.page}")
        if entwurf:
            canvas.setFont("Helvetica-Bold", 60)
            canvas.setFillColor(colors.Color(0.85, 0.85, 0.85, alpha=0.5))
            canvas.translate(A4[0] / 2, A4[1] / 2)
            canvas.rotate(45)
            canvas.drawCentredString(0, 0, "ENTWURF")
        canvas.restoreState()

    puffer = BytesIO()
    doc = SimpleDocTemplate(puffer, pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm,
                            topMargin=15 * mm, bottomMargin=28 * mm, title=str(beleg), author=m.firmenname)
    inhalt = [absatz(f"{m.firmenname} · {m.strasse} · {m.plz} {m.ort}", klein), Spacer(1, 4 * mm)]

    k = beleg.kunde
    anschrift = beleg.rechnungsanschrift or "\n".join(
        t for t in (k.name1, k.name2, k.strasse, f"{k.plz} {k.ort}") if t)
    daten = [["Kunden-Nr.", k.nummer], ["Datum", beleg.belegdatum.strftime("%d.%m.%Y")]]
    if beleg.nummer:
        daten.insert(0, [f"{beleg.get_art_display()}-Nr.", beleg.nummer])
    if beleg.referenz:
        daten.append(["Ihre Referenz", beleg.referenz])
    if beleg.ansprechpartner:
        daten.append(["Ansprechpartner", str(beleg.ansprechpartner)])
    if beleg.vorgaenger and beleg.vorgaenger.nummer:
        daten.append(["Bezug", f"{beleg.vorgaenger.get_art_display()} {beleg.vorgaenger.nummer}"])
    rechte_spalte = Table([[absatz(a, klein), absatz(b, normal)] for a, b in daten], colWidths=[28 * mm, 40 * mm])
    kopf = Table([[absatz(anschrift, normal), rechte_spalte]], colWidths=[100 * mm, 70 * mm])
    kopf.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    inhalt += [kopf, Spacer(1, 10 * mm),
               Paragraph(escape(f"{beleg.get_art_display()} {beleg.nummer or ''}".strip()), titel), Spacer(1, 4 * mm)]

    if beleg.vortext:
        inhalt += [absatz(beleg.vortext, normal), Spacer(1, 4 * mm)]
    zeilen = [[absatz(t, fett) for t in ("Pos.", "Artikel", "Bezeichnung", "Menge", "Preis", "Rabatt", "Netto")]]
    for p in beleg.positionen.select_related("artikel", "preisliste_artikel"):
        text = p.bezeichnung + (f"\n{p.zusatztext}" if p.zusatztext else "")
        if p.lieferschein_id and not p.gruppe_id:
            text += f"\nLieferschein {p.lieferschein.nummer} vom {p.lieferschein.belegdatum:%d.%m.%Y}"
        seriennummern = [sn.nummer for sn in p.seriennummern.all()] or (
            [sn.nummer for sn in p.quellposition.seriennummern.all()] if p.quellposition_id else [])
        if seriennummern:
            text += "\nSeriennummer(n): " + ", ".join(seriennummern)
        keys = [v.key.key for v in p.esd_vergaben.filter(status="aktiv").order_by("id").select_related("key")] or (
            [v.key.key for v in p.quellposition.esd_vergaben.filter(status="aktiv").order_by("id").select_related("key")]
            if p.quellposition_id else [])
        if keys:
            text += "\nLizenzschlüssel: " + ", ".join(keys)
        if p.gruppe_id:  # Unterartikel einer Gruppe: eingerückt, ohne Einzelpreise
            zeilen.append(["", absatz(p.artikelnummer, normal), absatz("   – " + text, normal),
                           zahl(p.menge), "", "", ""])
            continue
        zeilen.append([str(p.pos), absatz(p.artikelnummer, normal), absatz(text, normal),
                       zahl(p.menge), euro(p.einzelpreis), f"{zahl(p.rabatt_prozent)} %" if p.rabatt_prozent else "",
                       euro(p.netto_gesamt)])
    tabelle = Table(zeilen, colWidths=[12 * mm, 24 * mm, 58 * mm, 17 * mm, 21 * mm, 16 * mm, 22 * mm], repeatRows=1)
    tabelle.setStyle(TableStyle([
        ("FONT", (0, 1), (-1, -1), "Helvetica", 9), ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN", (3, 0), (-1, -1), "RIGHT"), ("LINEBELOW", (0, 0), (-1, 0), 0.8, colors.black),
        ("LINEBELOW", (0, 1), (-1, -1), 0.25, colors.lightgrey)]))
    inhalt += [tabelle, Spacer(1, 4 * mm)]

    summen = []
    for name, wert in (("Versandkosten netto", beleg.versandkosten), ("Verpackung netto", beleg.verpackung),
                       ("Versicherung netto", beleg.versicherung)):
        if wert:
            summen.append([name, euro(wert)])
    summen += [["Summe netto", euro(beleg.summe_netto)], ["Umsatzsteuer", euro(beleg.summe_steuer)],
               ["Gutschriftbetrag" if beleg.art == Belegart.GUTSCHRIFT else "Gesamtbetrag", euro(beleg.summe_brutto)]]
    st = Table(summen, colWidths=[40 * mm, 30 * mm], hAlign="RIGHT")
    st.setStyle(TableStyle([("ALIGN", (1, 0), (1, -1), "RIGHT"), ("FONT", (0, 0), (-1, -1), "Helvetica", 9),
                            ("FONT", (0, -1), (-1, -1), "Helvetica-Bold", 10),
                            ("LINEABOVE", (0, -1), (-1, -1), 0.8, colors.black)]))
    inhalt += [st, Spacer(1, 6 * mm)]
    if beleg.versandanschrift and beleg.versandanschrift != beleg.rechnungsanschrift:
        inhalt += [absatz("Lieferanschrift:", fett), absatz(beleg.versandanschrift, normal), Spacer(1, 3 * mm)]
    if beleg.versandart:
        inhalt += [absatz(f"Versandart: {beleg.versandart}", normal)]
    if beleg.art == Belegart.PROFORMA:
        inhalt += [Spacer(1, 3 * mm), absatz("Diese Proforma-Rechnung ist keine Rechnung im Sinne des Umsatzsteuergesetzes. "
                                             "Sie dient der Vorabinformation bzw. der Zahlung im Voraus; die Rechnung folgt mit der Lieferung.", fett)]
    if beleg.zahlungstext and beleg.art != Belegart.LIEFERSCHEIN:
        inhalt += [Spacer(1, 3 * mm), absatz(beleg.zahlungstext, normal)]
    if beleg.schlusstext:
        inhalt += [Spacer(1, 3 * mm), absatz(beleg.schlusstext, normal)]
    doc.build(inhalt, onFirstPage=seite, onLaterPages=seite)
    return puffer.getvalue()
