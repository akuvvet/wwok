"""Eingehende E-Rechnungen lesen: XRechnung/ZUGFeRD (CII) und UBL als XML, ZUGFeRD/Factur-X als PDF mit XML-Anhang."""
import io
import xml.etree.ElementTree as ET
from datetime import datetime
from decimal import Decimal, InvalidOperation


class ERechnungFehler(Exception):
    pass


def lokal(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def finde(knoten, *pfad):
    """Erstes Element, dessen Namensfolge (ohne Namensraum) dem Pfad entspricht; Zwischenebenen dürfen fehlen."""
    if knoten is None:
        return None
    aktuell = [knoten]
    for name in pfad:
        naechste = []
        for k in aktuell:
            naechste += [e for e in k.iter() if e is not k and lokal(e.tag) == name]
        if not naechste:
            return None
        aktuell = naechste
    return aktuell[0]


def text(knoten, *pfad) -> str:
    e = finde(knoten, *pfad)
    return (e.text or "").strip() if e is not None else ""


def betrag(wert: str) -> Decimal:
    try:
        return Decimal(wert)
    except InvalidOperation:
        return Decimal("0")


def datum(wert):
    wert = (wert or "").strip()
    for fmt, laenge in (("%Y%m%d", 8), ("%Y-%m-%d", 10)):
        try:
            return datetime.strptime(wert[:laenge], fmt).date()
        except ValueError:
            pass
    return None


def kind(wurzel, name):
    return next((e.text.strip() for e in wurzel if lokal(e.tag) == name and e.text), "")


def lese_xml(inhalt: bytes) -> dict:
    try:
        wurzel = ET.fromstring(inhalt)
    except ET.ParseError as fehler:
        raise ERechnungFehler(f"XML nicht lesbar: {fehler}")
    art = lokal(wurzel.tag)
    if art == "CrossIndustryInvoice":
        verkaeufer = finde(wurzel, "SellerTradeParty")
        summen = finde(wurzel, "SpecifiedTradeSettlementHeaderMonetarySummation")
        daten = {
            "nummer": text(wurzel, "ExchangedDocument", "ID"),
            "datum": datum(text(wurzel, "ExchangedDocument", "IssueDateTime", "DateTimeString")),
            "faellig": datum(text(wurzel, "SpecifiedTradePaymentTerms", "DueDateDateTime", "DateTimeString")),
            "lieferant": text(verkaeufer, "Name"),
            "ust_id": text(verkaeufer, "SpecifiedTaxRegistration", "ID"),
            "iban": text(wurzel, "PayeePartyCreditorFinancialAccount", "IBANID"),
            "netto": betrag(text(summen, "TaxBasisTotalAmount")),
            "steuer": betrag(text(summen, "TaxTotalAmount")),
            "brutto": betrag(text(summen, "GrandTotalAmount")),
        }
    elif art in ("Invoice", "CreditNote"):
        verkaeufer = finde(wurzel, "AccountingSupplierParty")
        summen = finde(wurzel, "LegalMonetaryTotal")
        daten = {
            "nummer": kind(wurzel, "ID"),
            "datum": datum(kind(wurzel, "IssueDate")),
            "faellig": datum(kind(wurzel, "DueDate")),
            "lieferant": text(verkaeufer, "PartyName", "Name") or text(verkaeufer, "PartyLegalEntity", "RegistrationName"),
            "ust_id": text(verkaeufer, "PartyTaxScheme", "CompanyID"),
            "iban": text(wurzel, "PayeeFinancialAccount", "ID"),
            "netto": betrag(text(summen, "TaxExclusiveAmount")),
            "steuer": betrag(text(wurzel, "TaxTotal", "TaxAmount")),
            "brutto": betrag(text(summen, "TaxInclusiveAmount")),
        }
    else:
        raise ERechnungFehler("Kein XRechnung/ZUGFeRD-Format erkannt (CII oder UBL erwartet).")
    if not daten["nummer"] or daten["datum"] is None or not daten["brutto"]:
        raise ERechnungFehler("Rechnungsnummer, Datum oder Gesamtbetrag fehlen in der Datei.")
    if not daten["netto"]:
        daten["netto"] = daten["brutto"] - daten["steuer"]
    return daten


def lese_pdf(inhalt: bytes) -> dict:
    try:
        from pypdf import PdfReader
        anhaenge = PdfReader(io.BytesIO(inhalt)).attachments
    except Exception as fehler:
        raise ERechnungFehler(f"PDF nicht lesbar: {fehler}")
    for name, teile in anhaenge.items():
        if name.lower().endswith(".xml"):
            return lese_xml(teile[0])
    raise ERechnungFehler("Im PDF ist keine ZUGFeRD-/Factur-X-XML eingebettet.")


def lese(dateiname: str, inhalt: bytes) -> dict:
    return lese_pdf(inhalt) if dateiname.lower().endswith(".pdf") else lese_xml(inhalt)
