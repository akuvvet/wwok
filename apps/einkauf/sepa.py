"""SEPA-Überweisungsdatei (pain.001.001.03) für Lieferantenzahlungen, z. B. zum Einlesen in StarMoney."""
import re
import xml.etree.ElementTree as ET
from decimal import Decimal

from django.utils import timezone

NS = "urn:iso:std:iso:20022:tech:xsd:pain.001.001.03"
UMLAUTE = {"ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue", "ß": "ss", "&": "+"}


def sepa_text(text: str, laenge: int) -> str:
    for a, b in UMLAUTE.items():
        text = (text or "").replace(a, b)
    return re.sub(r"[^A-Za-z0-9/?:().,'+ \-]", " ", text)[:laenge].strip() or "-"


def iban(text: str) -> str:
    return re.sub(r"\s+", "", text or "").upper()


class SepaFehler(Exception):
    pass


def erzeuge(mandant, rechnungen, ausfuehrung=None) -> bytes:
    """Eine Sammelüberweisung; Betrag mit Skonto, wenn die Frist am Ausführungstag noch läuft."""
    ausfuehrung = ausfuehrung or timezone.localdate()
    if not iban(mandant.iban):
        raise SepaFehler(f"Mandant {mandant.kurzname} hat keine IBAN (Verwaltung → Mandanten).")
    zeilen = []
    for r in rechnungen:
        if not iban(r.lieferant.iban):
            raise SepaFehler(f"Lieferant {r.lieferant.name1} hat keine IBAN.")
        zeilen.append((r, r.zahlbetrag(ausfuehrung)))
    if not zeilen:
        raise SepaFehler("Keine Rechnungen ausgewählt.")
    summe = sum((b for _, b in zeilen), Decimal("0"))
    jetzt = timezone.now()
    msg_id = f"WWOK{mandant.kurzname}{jetzt:%Y%m%d%H%M%S}"[:35]

    ET.register_namespace("", NS)

    def q(name):
        return f"{{{NS}}}{name}"

    wurzel = ET.Element(q("Document"))
    init = ET.SubElement(wurzel, q("CstmrCdtTrfInitn"))
    kopf = ET.SubElement(init, q("GrpHdr"))
    ET.SubElement(kopf, q("MsgId")).text = msg_id
    ET.SubElement(kopf, q("CreDtTm")).text = jetzt.strftime("%Y-%m-%dT%H:%M:%S")
    ET.SubElement(kopf, q("NbOfTxs")).text = str(len(zeilen))
    ET.SubElement(kopf, q("CtrlSum")).text = f"{summe:.2f}"
    ET.SubElement(ET.SubElement(kopf, q("InitgPty")), q("Nm")).text = sepa_text(mandant.firmenname, 70)

    pmt = ET.SubElement(init, q("PmtInf"))
    ET.SubElement(pmt, q("PmtInfId")).text = msg_id
    ET.SubElement(pmt, q("PmtMtd")).text = "TRF"
    ET.SubElement(pmt, q("BtchBookg")).text = "true"
    ET.SubElement(pmt, q("NbOfTxs")).text = str(len(zeilen))
    ET.SubElement(pmt, q("CtrlSum")).text = f"{summe:.2f}"
    ET.SubElement(ET.SubElement(ET.SubElement(pmt, q("PmtTpInf")), q("SvcLvl")), q("Cd")).text = "SEPA"
    ET.SubElement(pmt, q("ReqdExctnDt")).text = ausfuehrung.isoformat()
    ET.SubElement(ET.SubElement(pmt, q("Dbtr")), q("Nm")).text = sepa_text(mandant.firmenname, 70)
    ET.SubElement(ET.SubElement(ET.SubElement(pmt, q("DbtrAcct")), q("Id")), q("IBAN")).text = iban(mandant.iban)
    agt = ET.SubElement(ET.SubElement(pmt, q("DbtrAgt")), q("FinInstnId"))
    if mandant.bic:
        ET.SubElement(agt, q("BIC")).text = mandant.bic.replace(" ", "").upper()
    else:
        ET.SubElement(ET.SubElement(agt, q("Othr")), q("Id")).text = "NOTPROVIDED"
    ET.SubElement(pmt, q("ChrgBr")).text = "SLEV"

    for i, (r, betrag) in enumerate(zeilen, start=1):
        tx = ET.SubElement(pmt, q("CdtTrfTxInf"))
        ET.SubElement(ET.SubElement(tx, q("PmtId")), q("EndToEndId")).text = sepa_text(f"{msg_id[-8:]}-{i}", 35)
        ET.SubElement(ET.SubElement(tx, q("Amt")), q("InstdAmt"), Ccy="EUR").text = f"{betrag:.2f}"
        cagt = ET.SubElement(ET.SubElement(tx, q("CdtrAgt")), q("FinInstnId"))
        if r.lieferant.bic:
            ET.SubElement(cagt, q("BIC")).text = r.lieferant.bic.replace(" ", "").upper()
        else:
            ET.SubElement(ET.SubElement(cagt, q("Othr")), q("Id")).text = "NOTPROVIDED"
        ET.SubElement(ET.SubElement(tx, q("Cdtr")), q("Nm")).text = sepa_text(r.lieferant.kontoinhaber or r.lieferant.name1, 70)
        ET.SubElement(ET.SubElement(ET.SubElement(tx, q("CdtrAcct")), q("Id")), q("IBAN")).text = iban(r.lieferant.iban)
        text = f"RE {r.rechnungsnummer}" + (f" abzgl. {r.skonto_prozent}% Skonto" if betrag != r.brutto else "")
        ET.SubElement(ET.SubElement(tx, q("RmtInf")), q("Ustrd")).text = sepa_text(text, 140)
    ET.indent(wurzel)
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(wurzel, encoding="utf-8")
