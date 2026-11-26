"""Ausgehende E-Rechnung: ZUGFeRD/Factur-X (Profil EN 16931) als eingebettete XML im Beleg-PDF.

Gilt für abgeschlossene Rechnungen und Gutschriften. Der Empfänger kann die XML maschinell auslesen, das PDF bleibt lesbar.
Das PDF ist ein PDF/A-3B (apps/kern/pdfa.py: eingebettete Schrift, sRGB-Farbprofil, XMP mit Factur-X-Schema)."""
import xml.etree.ElementTree as ET
from decimal import ROUND_HALF_UP, Decimal

from .models import Belegart

NS = {
    "rsm": "urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100",
    "ram": "urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100",
    "udt": "urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100",
}
LEITLINIE = "urn:cen.eu:en16931:2017"
GESCHAEFTSPROZESS = "urn:fdc:peppol.eu:2017:poacc:billing:01:1.0"
LEITLINIE_XRECHNUNG = "urn:cen.eu:en16931:2017#compliant#urn:xeinkauf.de:kosit:xrechnung_3.0"
CENT = Decimal("0.01")


class ERechnungAusgangFehler(ValueError):
    pass


def anzahlung_summe(beleg) -> Decimal:
    """Auf die Rechnung verrechnete Anzahlungen (0 bei Gutschriften)."""
    if beleg.art != Belegart.RECHNUNG:
        return Decimal("0")
    from apps.finanzen.services import angerechnet_im_druck
    return angerechnet_im_druck(beleg)


def _betrag(wert, stellen=2) -> str:
    return f"{Decimal(wert).quantize(Decimal(1).scaleb(-stellen), rounding=ROUND_HALF_UP):f}"


def _datum(d) -> str:
    return d.strftime("%Y%m%d")


def _kategorie(prozent) -> str:
    return "S" if Decimal(prozent) > 0 else "Z"


# Steuerart des Kunden EG-Ausland / Drittland (Entscheidung des Nutzers 26.11.2026): Kategorie und Befreiungsgrund (BT-120/121)
BEFREIUNG = {
    "K": ("VATEX-EU-IC", "Steuerfreie innergemeinschaftliche Lieferung"),
    "AE": ("VATEX-EU-AE", "Steuerschuldnerschaft des Leistungsempfängers"),
    "G": ("VATEX-EU-G", "Steuerfreie Ausfuhrlieferung"),
}


def kategorie_der_position(beleg, typ, prozent) -> str:
    """S/Z im Inland; ohne MwSt: EG-Lieferung K, EG-Leistung (Typ L) AE, Drittland G."""
    if not beleg.steuerfrei:
        return _kategorie(prozent)
    if beleg.steuerart == 2:
        return "AE" if typ == "L" else "K"
    return "G"


def kaeuferanschrift(beleg) -> dict:
    """Käufer der E-Rechnung: die am Beleg geführte Anschrift; Altbelege ohne diese Felder fallen auf den Kundenstamm zurück."""
    if beleg.kaeufer_ort:
        return {"name": beleg.kaeufer_name or beleg.kunde.name1, "strasse": beleg.kaeufer_strasse, "plz": beleg.kaeufer_plz,
                "ort": beleg.kaeufer_ort, "land": beleg.kaeufer_land or "DE"}
    k = beleg.kunde
    return {"name": k.name1 + (" " + k.name2 if k.name2 else ""), "strasse": k.strasse, "plz": k.plz, "ort": k.ort, "land": k.land or "DE"}


def mandant_luecken(m) -> tuple:
    """(fehlt für ZUGFeRD, fehlt zusätzlich für XRechnung) – Verkäuferangaben wie in xml_erzeugen geprüft."""
    zugferd = [t for t, ok in (("USt-ID oder Steuernummer", m.ust_id or m.steuernummer), ("Straße", m.strasse), ("PLZ", m.plz),
                               ("Ort", m.ort), ("IBAN", m.iban)) if not ok]
    xrechnung = [t for t, ok in (("Telefon", m.telefon), ("E-Mail", m.email)) if not ok]
    return zugferd, xrechnung


def liefertag(beleg):
    """Liefer-/Leistungsdatum für die E-Rechnung: das der Rechnung; bei mehreren Lieferscheinen der letzte; bei einer
    Gutschrift das der zugrunde liegenden Rechnung."""
    if beleg.art == Belegart.GUTSCHRIFT:
        vorgaenger = beleg.vorgaenger if beleg.vorgaenger_id else None
        return liefertag(vorgaenger) if vorgaenger is not None and vorgaenger.art == Belegart.RECHNUNG else None
    datum = beleg.lieferdatum
    if datum is None and beleg.pk:
        from django.db.models import Max
        datum = beleg.positionen.filter(lieferschein__isnull=False).aggregate(d=Max("lieferschein__belegdatum"))["d"]
    return datum


def xml_erzeugen(beleg, xrechnung=False) -> bytes:
    """CII-XML (EN 16931) zu einer abgeschlossenen Rechnung oder Gutschrift; mit xrechnung=True nach dem deutschen
    Standard XRechnung (für Behörden): Leitweg-ID (am Kunden) und Verkäuferkontakt sind dann Pflicht."""
    if beleg.art not in (Belegart.RECHNUNG, Belegart.GUTSCHRIFT) or not beleg.abgeschlossen or not beleg.nummer:
        raise ERechnungAusgangFehler("Eine E-Rechnung gibt es nur zu abgeschlossenen Rechnungen und Gutschriften.")
    m, k = beleg.mandant, beleg.kunde
    if not (m.ust_id or m.steuernummer):
        raise ERechnungAusgangFehler(f"Mandant {m.kurzname}: USt-ID oder Steuernummer fehlt (Verwaltung → Mandanten).")
    if not (m.strasse and m.plz and m.ort):
        raise ERechnungAusgangFehler(f"Mandant {m.kurzname}: Anschrift unvollständig (Verwaltung → Mandanten).")
    kaeufer = kaeuferanschrift(beleg)
    if not (kaeufer["strasse"] and kaeufer["plz"] and kaeufer["ort"]):
        raise ERechnungAusgangFehler(f"Kunde {k.name1}: Anschrift unvollständig (Straße, PLZ, Ort).")
    if xrechnung:
        if not k.leitweg_id.strip():
            raise ERechnungAusgangFehler(f"XRechnung: Die Leitweg-ID fehlt – bitte beim Kunden {k.name1} eintragen (Kunde bearbeiten → Steuer & Bank).")
        if not (m.telefon and m.email):
            raise ERechnungAusgangFehler(f"XRechnung: Mandant {m.kurzname} braucht Telefon und E-Mail (Verwaltung → Mandanten).")
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
    if xrechnung:  # BT-23 Geschäftsprozess, in XRechnung Pflicht (PEPPOL-EN16931-R001); steht vor der Leitlinie
        kind(kind(kontext, "ram", "BusinessProcessSpecifiedDocumentContextParameter"), "ram", "ID", GESCHAEFTSPROZESS)
    kind(kind(kontext, "ram", "GuidelineSpecifiedDocumentContextParameter"), "ram", "ID", LEITLINIE_XRECHNUNG if xrechnung else LEITLINIE)
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
        einheit = p.preiseinheit or 1
        stueckpreis = (gesamt / p.menge * einheit) if p.menge else Decimal("0")  # Preis je Preiseinheit (BT-146), nach Rabatt
        preis = kind(kind(zeile, "ram", "SpecifiedLineTradeAgreement"), "ram", "NetPriceProductTradePrice")
        kind(preis, "ram", "ChargeAmount", _betrag(stueckpreis, 4))
        if einheit > 1:  # Preis gilt je n Stück: Preisbasismenge (BT-149) mit Einheit wie die Menge
            kind(preis, "ram", "BasisQuantity", _betrag(Decimal(einheit), 3), unitCode="C62")
        kind(kind(zeile, "ram", "SpecifiedLineTradeDelivery"), "ram", "BilledQuantity", _betrag(p.menge, 3), unitCode="C62")
        abrechnung = kind(zeile, "ram", "SpecifiedLineTradeSettlement")
        steuer = kind(abrechnung, "ram", "ApplicableTradeTax")
        kind(steuer, "ram", "TypeCode", "VAT")
        kind(steuer, "ram", "CategoryCode", kategorie_der_position(beleg, p.typ, p.steuer_prozent))
        kind(steuer, "ram", "RateApplicablePercent", _betrag(p.steuer_prozent))
        kind(kind(abrechnung, "ram", "SpecifiedTradeSettlementLineMonetarySummation"), "ram", "LineTotalAmount", _betrag(gesamt))

    # Steuerbasis je Satz wie in Beleg.berechne(): Positionen plus Nebenkosten auf dem höchsten Satz
    # Schlüssel (Kategorie, Satz): ohne MwSt können Lieferung (K) und Leistung (AE) mit 0 % nebeneinander stehen
    saetze = {}
    for p in positionen:
        schluessel = (kategorie_der_position(beleg, p.typ, p.steuer_prozent), p.steuer_prozent)
        saetze[schluessel] = saetze.get(schluessel, Decimal("0")) + p.netto_gesamt
    nebenkosten = beleg.versandkosten + beleg.verpackung + beleg.versicherung
    summe_positionen = sum(saetze.values(), Decimal("0"))
    saetze, rabatte = beleg.nach_rabatt(saetze)  # Belegrabatt als Nachlass je Steuersatz
    rabatt_summe = sum(rabatte.values(), Decimal("0"))
    if beleg.steuerfrei:  # Versand folgt der Ware; nur Leistungen: wie die Leistung
        kats = {kat for kat, _ in saetze}
        nebensatz = ("K" if "K" in kats or not kats else "AE", Decimal("0")) if beleg.steuerart == 2 else ("G", Decimal("0"))
    else:
        hoechst = max((satz for _, satz in saetze), default=Decimal("19"))
        nebensatz = (_kategorie(hoechst), hoechst)
    if nebenkosten:
        saetze[nebensatz] = saetze.get(nebensatz, Decimal("0")) + nebenkosten

    vereinbarung = kind(handel, "ram", "ApplicableHeaderTradeAgreement")
    kind(vereinbarung, "ram", "BuyerReference", k.leitweg_id.strip() if xrechnung else (beleg.referenz or k.nummer))
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
    kaeufer_xml = kind(vereinbarung, "ram", "BuyerTradeParty")
    kind(kaeufer_xml, "ram", "Name", kaeufer["name"][:200])
    adresse = kind(kaeufer_xml, "ram", "PostalTradeAddress")
    kind(adresse, "ram", "PostcodeCode", kaeufer["plz"])
    kind(adresse, "ram", "LineOne", kaeufer["strasse"])
    kind(adresse, "ram", "CityName", kaeufer["ort"])
    kind(adresse, "ram", "CountryID", kaeufer["land"])
    if k.email:
        kind(kind(kaeufer_xml, "ram", "URIUniversalCommunication"), "ram", "URIID", k.email, schemeID="EM")
    if k.ust_id:
        kind(kind(kaeufer_xml, "ram", "SpecifiedTaxRegistration"), "ram", "ID", k.ust_id.replace(" ", ""), schemeID="VA")

    lieferung = kind(handel, "ram", "ApplicableHeaderTradeDelivery")
    lieferdatum = liefertag(beleg)  # BT-72; ein leeres Element mögen die Prüfregeln nicht (PEPPOL-EN16931-R008)
    if lieferdatum:
        ereignis = kind(kind(lieferung, "ram", "ActualDeliverySupplyChainEvent"), "ram", "OccurrenceDateTime")
        kind(ereignis, "udt", "DateTimeString", _datum(lieferdatum), format="102")

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
    for (kat, satz), basis in sorted(saetze.items(), key=lambda e: (e[0][1], e[0][0])):
        betrag = (basis * satz / 100).quantize(CENT, rounding=ROUND_HALF_UP)
        steuer_summe += betrag
        t = kind(abrechnung, "ram", "ApplicableTradeTax")
        kind(t, "ram", "CalculatedAmount", _betrag(betrag))
        kind(t, "ram", "TypeCode", "VAT")
        if kat in BEFREIUNG:
            kind(t, "ram", "ExemptionReason", BEFREIUNG[kat][1])
        kind(t, "ram", "BasisAmount", _betrag(basis))
        kind(t, "ram", "CategoryCode", kat)
        if kat in BEFREIUNG:
            kind(t, "ram", "ExemptionReasonCode", BEFREIUNG[kat][0])
        kind(t, "ram", "RateApplicablePercent", _betrag(satz))
    for (kat, satz), betrag in sorted(rabatte.items(), key=lambda e: (e[0][1], e[0][0])):
        if not betrag:
            continue
        nachlass = kind(abrechnung, "ram", "SpecifiedTradeAllowanceCharge")
        kind(kind(nachlass, "ram", "ChargeIndicator"), "udt", "Indicator", "false")
        kind(nachlass, "ram", "ActualAmount", _betrag(betrag))
        kind(nachlass, "ram", "Reason", f"Belegrabatt {_betrag(beleg.rabatt_prozent)} %")
        t = kind(nachlass, "ram", "CategoryTradeTax")
        kind(t, "ram", "TypeCode", "VAT")
        kind(t, "ram", "CategoryCode", kat)
        kind(t, "ram", "RateApplicablePercent", _betrag(satz))
    if nebenkosten:
        zuschlag = kind(abrechnung, "ram", "SpecifiedTradeAllowanceCharge")
        kind(kind(zuschlag, "ram", "ChargeIndicator"), "udt", "Indicator", "true")
        kind(zuschlag, "ram", "ActualAmount", _betrag(nebenkosten))
        kind(zuschlag, "ram", "Reason", "Versand, Verpackung und Versicherung")
        t = kind(zuschlag, "ram", "CategoryTradeTax")
        kind(t, "ram", "TypeCode", "VAT")
        kind(t, "ram", "CategoryCode", nebensatz[0])
        kind(t, "ram", "RateApplicablePercent", _betrag(nebensatz[1]))

    bedingungen = kind(abrechnung, "ram", "SpecifiedTradePaymentTerms")
    if beleg.zahlungstext:
        kind(bedingungen, "ram", "Description", beleg.zahlungstext[:500])
    if beleg.faellig_am:
        kind(kind(bedingungen, "ram", "DueDateDateTime"), "udt", "DateTimeString", _datum(beleg.faellig_am), format="102")
    if lastschrift:
        kind(bedingungen, "ram", "DirectDebitMandateID", k.sepa_mandatsreferenz)

    netto = summe_positionen - rabatt_summe + nebenkosten
    brutto = netto + steuer_summe
    summen = kind(abrechnung, "ram", "SpecifiedTradeSettlementHeaderMonetarySummation")
    kind(summen, "ram", "LineTotalAmount", _betrag(summe_positionen))
    kind(summen, "ram", "ChargeTotalAmount", _betrag(nebenkosten))
    if rabatt_summe:
        kind(summen, "ram", "AllowanceTotalAmount", _betrag(rabatt_summe))
    kind(summen, "ram", "TaxBasisTotalAmount", _betrag(netto))
    kind(summen, "ram", "TaxTotalAmount", _betrag(steuer_summe), currencyID="EUR")
    kind(summen, "ram", "GrandTotalAmount", _betrag(brutto))
    vorab = anzahlung_summe(beleg)  # Vorkasse/Anzahlung aus Proforma oder Bestellung (EN 16931 BT-113)
    if vorab:
        kind(summen, "ram", "TotalPrepaidAmount", _betrag(vorab))
    kind(summen, "ram", "DuePayableAmount", _betrag(brutto - vorab))

    ET.indent(wurzel)
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(wurzel, encoding="utf-8")


def beleg_pdf_mit_xml(beleg):
    """(PDF-Bytes, Hinweis): bei abgeschlossenen Rechnungen/Gutschriften das PDF/A-3 mit eingebetteter E-Rechnung, sonst das
    normale PDF. Die erste E-Rechnung wird archiviert und danach unverändert ausgeliefert (GoBD, `ERechnungArchiv`).
    Kann die XML nicht erzeugt werden (z. B. fehlende USt-ID), kommt das normale PDF mit Hinweis zurück, nichts wird archiviert."""
    from .pdf import beleg_pdf
    if beleg.art not in (Belegart.RECHNUNG, Belegart.GUTSCHRIFT) or not beleg.abgeschlossen or not beleg.nummer:
        return beleg_pdf(beleg), ""
    archiv = archiv_holen(beleg)
    if archiv is not None:
        return bytes(archiv.pdf), ""
    pdf = beleg_pdf(beleg)
    try:
        xml = xml_erzeugen(beleg)
    except ERechnungAusgangFehler as fehler:
        return pdf, f"Ohne E-Rechnung (ZUGFeRD): {fehler}"
    from apps.kern.pdfa import pdfa3
    pdf = pdfa3(pdf, str(beleg), beleg.mandant.firmenname, xml)  # PDF/A-3B mit factur-x.xml
    return bytes(archivieren(beleg, pdf).pdf), ""


def archiv_holen(beleg):
    from .models import ERechnungArchiv
    return ERechnungArchiv.objects.filter(beleg=beleg).first()


def archivieren(beleg, pdf: bytes):
    """Erste E-Rechnung festschreiben; gleichzeitiger zweiter Aufruf bekommt die schon gespeicherte (Unique auf beleg)."""
    import hashlib

    from django.db import IntegrityError, transaction

    from .models import ERechnungArchiv
    try:
        with transaction.atomic():
            return ERechnungArchiv.objects.create(beleg=beleg, pdf=pdf, pdf_sha256=hashlib.sha256(pdf).hexdigest())
    except IntegrityError:
        return ERechnungArchiv.objects.get(beleg=beleg)


def xrechnung_archiviert(beleg) -> bytes:
    """XRechnung (XML) zum Beleg: beim ersten Abruf erzeugt und im Archiv festgeschrieben, danach unverändert."""
    import hashlib

    from django.db import transaction
    from django.utils import timezone

    from .models import ERechnungArchiv
    archiv = archiv_holen(beleg)
    if archiv is not None and archiv.xrechnung:
        return bytes(archiv.xrechnung)
    xml = xml_erzeugen(beleg, xrechnung=True)
    if archiv is None:  # das PDF dazu gleich mit festschreiben, damit beides zusammenpasst
        beleg_pdf_mit_xml(beleg)
        archiv = archiv_holen(beleg)
        if archiv is None:
            return xml
    with transaction.atomic():
        archiv = ERechnungArchiv.objects.select_for_update().get(pk=archiv.pk)
        if not archiv.xrechnung:
            archiv.xrechnung, archiv.xrechnung_sha256, archiv.xrechnung_am = xml, hashlib.sha256(xml).hexdigest(), timezone.now()
            archiv.save()
    return bytes(archiv.xrechnung)
