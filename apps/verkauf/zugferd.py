"""Ausgehende E-Rechnung: ZUGFeRD/Factur-X (Profil EN 16931) als eingebettete XML im Beleg-PDF.

Gilt für abgeschlossene Rechnungen und Gutschriften. Der Empfänger kann die XML maschinell auslesen, das PDF bleibt lesbar.
Hinweis: Das PDF ist kein zertifiziertes PDF/A-3. Vor dem ersten Einsatz mit einem Prüfwerkzeug (z. B. Mustang oder KoSIT-Validator)
und mit dem Steuerberater abstimmen."""
import io
import xml.etree.ElementTree as ET
from decimal import ROUND_HALF_UP, Decimal

from .models import Belegart

NS = {
    "rsm": "urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100",
    "ram": "urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100",
    "udt": "urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100",
}
LEITLINIE = "urn:cen.eu:en16931:2017"
CENT = Decimal("0.01")


class ERechnungAusgangFehler(ValueError):
    pass


def _betrag(wert, stellen=2) -> str:
    return f"{Decimal(wert).quantize(Decimal(1).scaleb(-stellen), rounding=ROUND_HALF_UP):f}"


def _datum(d) -> str:
    return d.strftime("%Y%m%d")


def _kategorie(prozent) -> str:
    return "S" if Decimal(prozent) > 0 else "Z"


def xml_erzeugen(beleg) -> bytes:
    """CII-XML (EN 16931) zu einer abgeschlossenen Rechnung oder Gutschrift."""
    if beleg.art not in (Belegart.RECHNUNG, Belegart.GUTSCHRIFT) or not beleg.abgeschlossen or not beleg.nummer:
        raise ERechnungAusgangFehler("Eine E-Rechnung gibt es nur zu abgeschlossenen Rechnungen und Gutschriften.")
    m, k = beleg.mandant, beleg.kunde
    if not (m.ust_id or m.steuernummer):
        raise ERechnungAusgangFehler(f"Mandant {m.kurzname}: USt-ID oder Steuernummer fehlt (Verwaltung → Mandanten).")
    if not (m.strasse and m.plz and m.ort):
        raise ERechnungAusgangFehler(f"Mandant {m.kurzname}: Anschrift unvollständig (Verwaltung → Mandanten).")
    if not (k.strasse and k.plz and k.ort):
        raise ERechnungAusgangFehler(f"Kunde {k.name1}: Anschrift unvollständig (Straße, PLZ, Ort).")
    lastschrift = bool(beleg.zahlungsbedingung and beleg.zahlungsbedingung.lastschrift)
    if lastschrift and not (k.iban and k.sepa_mandatsreferenz):
        raise ERechnungAusgangFehler(f"Kunde {k.name1}: Lastschrift ohne IBAN oder Mandatsreferenz.")
    if not lastschrift and not m.iban:
        raise ERechnungAusgangFehler(f"Mandant {m.kurzname}: IBAN fehlt (Verwaltung → Mandanten).")

    for praefix, uri in NS.items():
        ET.register_namespace(praefix, uri)

    def q(praefix, name):
        return f"{{{NS[praefix]}}}{name}"

    def kind(eltern, praefix, name, text=None, **attribute):
        e = ET.SubElement(eltern, q(praefix, name), **attribute)
        if text is not None:
            e.text = str(text)
        return e

    wurzel = ET.Element(q("rsm", "CrossIndustryInvoice"))
    kontext = kind(wurzel, "rsm", "ExchangedDocumentContext")
    kind(kind(kontext, "ram", "GuidelineSpecifiedDocumentContextParameter"), "ram", "ID", LEITLINIE)
    dokument = kind(wurzel, "rsm", "ExchangedDocument")
    kind(dokument, "ram", "ID", beleg.nummer)
    kind(dokument, "ram", "TypeCode", "380" if beleg.art == Belegart.RECHNUNG else "381")
    kind(kind(dokument, "ram", "IssueDateTime"), "udt", "DateTimeString", _datum(beleg.belegdatum), format="102")

    handel = kind(wurzel, "rsm", "SupplyChainTradeTransaction")
    positionen = [p for p in beleg.positionen.select_related("artikel").order_by("pos", "id") if not p.gruppe_id]
    for nr, p in enumerate(positionen, start=1):
        zeile = kind(handel, "ram", "IncludedSupplyChainTradeLineItem")
        kind(kind(zeile, "ram", "AssociatedDocumentLineDocument"), "ram", "LineID", nr)
        produkt = kind(zeile, "ram", "SpecifiedTradeProduct")
        if p.artikelnummer:
            kind(produkt, "ram", "SellerAssignedID", p.artikelnummer)
        kind(produkt, "ram", "Name", p.bezeichnung[:200] or "Position")
        gesamt = p.netto_gesamt
        stueckpreis = (gesamt / p.menge) if p.menge else Decimal("0")
        kind(kind(kind(zeile, "ram", "SpecifiedLineTradeAgreement"), "ram", "NetPriceProductTradePrice"),
             "ram", "ChargeAmount", _betrag(stueckpreis, 4))
        kind(kind(zeile, "ram", "SpecifiedLineTradeDelivery"), "ram", "BilledQuantity", _betrag(p.menge, 3), unitCode="C62")
        abrechnung = kind(zeile, "ram", "SpecifiedLineTradeSettlement")
        steuer = kind(abrechnung, "ram", "ApplicableTradeTax")
        kind(steuer, "ram", "TypeCode", "VAT")
        kind(steuer, "ram", "CategoryCode", _kategorie(p.steuer_prozent))
        kind(steuer, "ram", "RateApplicablePercent", _betrag(p.steuer_prozent))
        kind(kind(abrechnung, "ram", "SpecifiedTradeSettlementLineMonetarySummation"), "ram", "LineTotalAmount", _betrag(gesamt))

    # Steuerbasis je Satz wie in Beleg.berechne(): Positionen plus Nebenkosten auf dem höchsten Satz
    saetze = {}
    for p in positionen:
        saetze[p.steuer_prozent] = saetze.get(p.steuer_prozent, Decimal("0")) + p.netto_gesamt
    nebenkosten = beleg.versandkosten + beleg.verpackung + beleg.versicherung
    summe_positionen = sum(saetze.values(), Decimal("0"))
    nebensatz = max(saetze, default=Decimal("19"))
    if nebenkosten:
        saetze[nebensatz] = saetze.get(nebensatz, Decimal("0")) + nebenkosten

    vereinbarung = kind(handel, "ram", "ApplicableHeaderTradeAgreement")
    kind(vereinbarung, "ram", "BuyerReference", beleg.referenz or k.nummer)
    verkaeufer = kind(vereinbarung, "ram", "SellerTradeParty")
    kind(verkaeufer, "ram", "Name", m.firmenname)
    if m.telefon or m.email:
        kontakt = kind(verkaeufer, "ram", "DefinedTradeContact")
        kind(kontakt, "ram", "PersonName", m.firmenname)
        if m.telefon:
            kind(kind(kontakt, "ram", "TelephoneUniversalCommunication"), "ram", "CompleteNumber", m.telefon)
        if m.email:
            kind(kind(kontakt, "ram", "EmailURIUniversalCommunication"), "ram", "URIID", m.email)
    adresse = kind(verkaeufer, "ram", "PostalTradeAddress")
    kind(adresse, "ram", "PostcodeCode", m.plz)
    kind(adresse, "ram", "LineOne", m.strasse)
    kind(adresse, "ram", "CityName", m.ort)
    kind(adresse, "ram", "CountryID", m.land or "DE")
    if m.email:
        kind(kind(verkaeufer, "ram", "URIUniversalCommunication"), "ram", "URIID", m.email, schemeID="EM")
    if m.ust_id:
        kind(kind(verkaeufer, "ram", "SpecifiedTaxRegistration"), "ram", "ID", m.ust_id.replace(" ", ""), schemeID="VA")
    if m.steuernummer:
        kind(kind(verkaeufer, "ram", "SpecifiedTaxRegistration"), "ram", "ID", m.steuernummer, schemeID="FC")
    kaeufer = kind(vereinbarung, "ram", "BuyerTradeParty")
    kind(kaeufer, "ram", "Name", (k.name1 + (" " + k.name2 if k.name2 else ""))[:200])
    adresse = kind(kaeufer, "ram", "PostalTradeAddress")
    kind(adresse, "ram", "PostcodeCode", k.plz)
    kind(adresse, "ram", "LineOne", k.strasse)
    kind(adresse, "ram", "CityName", k.ort)
    kind(adresse, "ram", "CountryID", k.land or "DE")
    if k.email:
        kind(kind(kaeufer, "ram", "URIUniversalCommunication"), "ram", "URIID", k.email, schemeID="EM")
    if k.ust_id:
        kind(kind(kaeufer, "ram", "SpecifiedTaxRegistration"), "ram", "ID", k.ust_id.replace(" ", ""), schemeID="VA")

    kind(handel, "ram", "ApplicableHeaderTradeDelivery")

    abrechnung = kind(handel, "ram", "ApplicableHeaderTradeSettlement")
    if lastschrift and m.glaeubiger_id:
        kind(abrechnung, "ram", "CreditorReferenceID", m.glaeubiger_id.replace(" ", ""))
    kind(abrechnung, "ram", "PaymentReference", beleg.nummer)
    kind(abrechnung, "ram", "InvoiceCurrencyCode", "EUR")
    zahlart = kind(abrechnung, "ram", "SpecifiedTradeSettlementPaymentMeans")
    if lastschrift:
        kind(zahlart, "ram", "TypeCode", "59")
        kind(kind(zahlart, "ram", "PayerPartyDebtorFinancialAccount"), "ram", "IBANID", k.iban.replace(" ", "").upper())
    else:
        kind(zahlart, "ram", "TypeCode", "58")
        konto = kind(zahlart, "ram", "PayeePartyCreditorFinancialAccount")
        kind(konto, "ram", "IBANID", m.iban.replace(" ", "").upper())
        if m.bic:
            kind(kind(zahlart, "ram", "PayeeSpecifiedCreditorFinancialInstitution"), "ram", "BICID", m.bic.replace(" ", "").upper())

    steuer_summe = Decimal("0")
    for satz, basis in sorted(saetze.items()):
        betrag = (basis * satz / 100).quantize(CENT, rounding=ROUND_HALF_UP)
        steuer_summe += betrag
        t = kind(abrechnung, "ram", "ApplicableTradeTax")
        kind(t, "ram", "CalculatedAmount", _betrag(betrag))
        kind(t, "ram", "TypeCode", "VAT")
        kind(t, "ram", "BasisAmount", _betrag(basis))
        kind(t, "ram", "CategoryCode", _kategorie(satz))
        kind(t, "ram", "RateApplicablePercent", _betrag(satz))
    if nebenkosten:
        zuschlag = kind(abrechnung, "ram", "SpecifiedTradeAllowanceCharge")
        kind(kind(zuschlag, "ram", "ChargeIndicator"), "udt", "Indicator", "true")
        kind(zuschlag, "ram", "ActualAmount", _betrag(nebenkosten))
        kind(zuschlag, "ram", "Reason", "Versand, Verpackung und Versicherung")
        t = kind(zuschlag, "ram", "CategoryTradeTax")
        kind(t, "ram", "TypeCode", "VAT")
        kind(t, "ram", "CategoryCode", _kategorie(nebensatz))
        kind(t, "ram", "RateApplicablePercent", _betrag(nebensatz))

    bedingungen = kind(abrechnung, "ram", "SpecifiedTradePaymentTerms")
    if beleg.zahlungstext:
        kind(bedingungen, "ram", "Description", beleg.zahlungstext[:500])
    if beleg.faellig_am:
        kind(kind(bedingungen, "ram", "DueDateDateTime"), "udt", "DateTimeString", _datum(beleg.faellig_am), format="102")
    if lastschrift:
        kind(bedingungen, "ram", "DirectDebitMandateID", k.sepa_mandatsreferenz)

    netto = summe_positionen + nebenkosten
    brutto = netto + steuer_summe
    summen = kind(abrechnung, "ram", "SpecifiedTradeSettlementHeaderMonetarySummation")
    kind(summen, "ram", "LineTotalAmount", _betrag(summe_positionen))
    kind(summen, "ram", "ChargeTotalAmount", _betrag(nebenkosten))
    kind(summen, "ram", "TaxBasisTotalAmount", _betrag(netto))
    kind(summen, "ram", "TaxTotalAmount", _betrag(steuer_summe), currencyID="EUR")
    kind(summen, "ram", "GrandTotalAmount", _betrag(brutto))
    kind(summen, "ram", "DuePayableAmount", _betrag(brutto))

    ET.indent(wurzel)
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(wurzel, encoding="utf-8")


XMP = """<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about="" xmlns:fx="urn:factur-x:pdfa:CrossIndustryDocument:invoice:1p0#">
   <fx:DocumentType>INVOICE</fx:DocumentType>
   <fx:DocumentFileName>factur-x.xml</fx:DocumentFileName>
   <fx:Version>1.0</fx:Version>
   <fx:ConformanceLevel>EN 16931</fx:ConformanceLevel>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>"""


def pdf_einbetten(pdf: bytes, xml: bytes) -> bytes:
    """Hängt die XML als factur-x.xml (Beziehung „Data“) an das PDF und ergänzt die Factur-X-Metadaten."""
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import ArrayObject, DecodedStreamObject, NameObject

    schreiber = PdfWriter(clone_from=PdfReader(io.BytesIO(pdf)))
    schreiber.add_attachment("factur-x.xml", xml)
    wurzel = schreiber._root_object
    eintraege = wurzel["/Names"]["/EmbeddedFiles"]["/Names"]
    spec = eintraege[-1]
    spec_obj = spec.get_object()
    spec_obj[NameObject("/AFRelationship")] = NameObject("/Data")
    ein = spec_obj["/EF"]["/F"].get_object()
    ein[NameObject("/Subtype")] = NameObject("/text#2Fxml")
    wurzel[NameObject("/AF")] = ArrayObject([spec])
    meta = DecodedStreamObject()
    meta.set_data(XMP.encode("utf-8"))
    meta[NameObject("/Type")] = NameObject("/Metadata")
    meta[NameObject("/Subtype")] = NameObject("/XML")
    wurzel[NameObject("/Metadata")] = schreiber._add_object(meta)
    ausgabe = io.BytesIO()
    schreiber.write(ausgabe)
    return ausgabe.getvalue()


def beleg_pdf_mit_xml(beleg):
    """(PDF-Bytes, Hinweis): bei abgeschlossenen Rechnungen/Gutschriften mit eingebetteter E-Rechnung, sonst normales PDF.
    Kann die XML nicht erzeugt werden (z. B. fehlende USt-ID), kommt das normale PDF mit einem Hinweistext zurück."""
    from .pdf import beleg_pdf
    pdf = beleg_pdf(beleg)
    if beleg.art not in (Belegart.RECHNUNG, Belegart.GUTSCHRIFT) or not beleg.abgeschlossen:
        return pdf, ""
    try:
        return pdf_einbetten(pdf, xml_erzeugen(beleg)), ""
    except ERechnungAusgangFehler as fehler:
        return pdf, f"Ohne E-Rechnung (ZUGFeRD): {fehler}"
