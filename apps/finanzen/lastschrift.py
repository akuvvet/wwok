"""SEPA-Basis-Lastschrift (pain.008.001.02): Sammeldatei für Kundenrechnungen zum Einreichen bei der Bank, z. B. über StarMoney.

Voraussetzung je Kunde: IBAN, Mandatsreferenz und Mandatsdatum (Stammdaten → Steuer & Bank); je Mandant: IBAN und Gläubiger-ID."""
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from apps.einkauf.sepa import SepaFehler, iban, sepa_text

NS = "urn:iso:std:iso:20022:tech:xsd:pain.008.001.02"


def mandat_fehlt(kunde) -> str:
    """Was beim Kunden für eine Lastschrift fehlt (leer = vollständig)."""
    fehlt = []
    if not iban(kunde.iban):
        fehlt.append("IBAN")
    if not kunde.sepa_mandatsreferenz:
        fehlt.append("Mandatsreferenz")
    if not kunde.sepa_mandat_datum:
        fehlt.append("Mandatsdatum")
    return ", ".join(fehlt)


def sequenz(kunde, mandant) -> str:
    """FRST bei der ersten Lastschrift mit dieser Mandatsreferenz, danach RCUR."""
    from .models import Lastschrift
    schon = Lastschrift.objects.filter(mandant=mandant, beleg__kunde=kunde, aufgehoben=False,
                                       mandatsreferenz=kunde.sepa_mandatsreferenz).exists()
    return "RCUR" if schon else "FRST"


def erzeuge(mandant, zeilen, einzug):
    """Liefert (XML-Bytes, Nachrichten-ID). zeilen: Liste (Beleg, Betrag, Sequenz). Pro Sequenz eine Sammelbuchung."""
    if not iban(mandant.iban):
        raise SepaFehler(f"Mandant {mandant.kurzname} hat keine IBAN (Verwaltung → Mandanten).")
    if not mandant.glaeubiger_id:
        raise SepaFehler(f"Mandant {mandant.kurzname} hat keine Gläubiger-ID (Verwaltung → Mandanten).")
    if not zeilen:
        raise SepaFehler("Keine Rechnungen ausgewählt.")
    if einzug < timezone.localdate() + timedelta(days=1):
        raise SepaFehler("Das Einzugsdatum muss mindestens einen Tag in der Zukunft liegen (Bankarbeitstag).")
    for beleg, betrag, _ in zeilen:
        fehlt = mandat_fehlt(beleg.kunde)
        if fehlt:
            raise SepaFehler(f"{beleg.kunde.name1}: Mandat unvollständig ({fehlt}).")
        if betrag <= 0:
            raise SepaFehler(f"{beleg.nummer}: Betrag muss größer als 0 sein.")
    summe = sum((b for _, b, _ in zeilen), Decimal("0"))
    jetzt = timezone.now()
    msg_id = f"WWOKLS{mandant.kurzname}{jetzt:%Y%m%d%H%M%S}"[:35]

    ET.register_namespace("", NS)

    def q(name):
        return f"{{{NS}}}{name}"

    wurzel = ET.Element(q("Document"))
    init = ET.SubElement(wurzel, q("CstmrDrctDbtInitn"))
    kopf = ET.SubElement(init, q("GrpHdr"))
    ET.SubElement(kopf, q("MsgId")).text = msg_id
    ET.SubElement(kopf, q("CreDtTm")).text = jetzt.strftime("%Y-%m-%dT%H:%M:%S")
    ET.SubElement(kopf, q("NbOfTxs")).text = str(len(zeilen))
    ET.SubElement(kopf, q("CtrlSum")).text = f"{summe:.2f}"
    ET.SubElement(ET.SubElement(kopf, q("InitgPty")), q("Nm")).text = sepa_text(mandant.firmenname, 70)

    nach_sequenz = defaultdict(list)
    for z in zeilen:
        nach_sequenz[z[2]].append(z)
    laufend = 0
    for seq in ("FRST", "RCUR"):
        gruppe = nach_sequenz.get(seq)
        if not gruppe:
            continue
        teilsumme = sum((b for _, b, _ in gruppe), Decimal("0"))
        pmt = ET.SubElement(init, q("PmtInf"))
        ET.SubElement(pmt, q("PmtInfId")).text = f"{msg_id[:30]}-{seq}"[:35]
        ET.SubElement(pmt, q("PmtMtd")).text = "DD"
        ET.SubElement(pmt, q("BtchBookg")).text = "true"
        ET.SubElement(pmt, q("NbOfTxs")).text = str(len(gruppe))
        ET.SubElement(pmt, q("CtrlSum")).text = f"{teilsumme:.2f}"
        info = ET.SubElement(pmt, q("PmtTpInf"))
        ET.SubElement(ET.SubElement(info, q("SvcLvl")), q("Cd")).text = "SEPA"
        ET.SubElement(ET.SubElement(info, q("LclInstrm")), q("Cd")).text = "CORE"
        ET.SubElement(info, q("SeqTp")).text = seq
        ET.SubElement(pmt, q("ReqdColltnDt")).text = einzug.isoformat()
        ET.SubElement(ET.SubElement(pmt, q("Cdtr")), q("Nm")).text = sepa_text(mandant.firmenname, 70)
        ET.SubElement(ET.SubElement(ET.SubElement(pmt, q("CdtrAcct")), q("Id")), q("IBAN")).text = iban(mandant.iban)
        agt = ET.SubElement(ET.SubElement(pmt, q("CdtrAgt")), q("FinInstnId"))
        if mandant.bic:
            ET.SubElement(agt, q("BIC")).text = mandant.bic.replace(" ", "").upper()
        else:
            ET.SubElement(ET.SubElement(agt, q("Othr")), q("Id")).text = "NOTPROVIDED"
        ET.SubElement(pmt, q("ChrgBr")).text = "SLEV"
        schema = ET.SubElement(ET.SubElement(ET.SubElement(ET.SubElement(pmt, q("CdtrSchmeId")), q("Id")), q("PrvtId")), q("Othr"))
        ET.SubElement(schema, q("Id")).text = re.sub(r"\s+", "", mandant.glaeubiger_id).upper()
        ET.SubElement(ET.SubElement(schema, q("SchmeNm")), q("Prtry")).text = "SEPA"

        for beleg, betrag, _ in gruppe:
            laufend += 1
            k = beleg.kunde
            tx = ET.SubElement(pmt, q("DrctDbtTxInf"))
            ET.SubElement(ET.SubElement(tx, q("PmtId")), q("EndToEndId")).text = sepa_text(f"{beleg.nummer}", 35)
            ET.SubElement(tx, q("InstdAmt"), Ccy="EUR").text = f"{betrag:.2f}"
            mandat = ET.SubElement(ET.SubElement(tx, q("DrctDbtTx")), q("MndtRltdInf"))
            ET.SubElement(mandat, q("MndtId")).text = sepa_text(k.sepa_mandatsreferenz, 35)
            ET.SubElement(mandat, q("DtOfSgntr")).text = k.sepa_mandat_datum.isoformat()
            dagt = ET.SubElement(ET.SubElement(tx, q("DbtrAgt")), q("FinInstnId"))
            if k.bic:
                ET.SubElement(dagt, q("BIC")).text = k.bic.replace(" ", "").upper()
            else:
                ET.SubElement(ET.SubElement(dagt, q("Othr")), q("Id")).text = "NOTPROVIDED"
            ET.SubElement(ET.SubElement(tx, q("Dbtr")), q("Nm")).text = sepa_text(k.kontoinhaber or k.name1, 70)
            ET.SubElement(ET.SubElement(ET.SubElement(tx, q("DbtrAcct")), q("Id")), q("IBAN")).text = iban(k.iban)
            ET.SubElement(ET.SubElement(tx, q("RmtInf")), q("Ustrd")).text = sepa_text(
                f"Rechnung {beleg.nummer} vom {beleg.belegdatum:%d.%m.%Y}", 140)
    ET.indent(wurzel)
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(wurzel, encoding="utf-8"), msg_id
