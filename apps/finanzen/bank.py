"""Bankauszug-Import (CSV und CAMT.053-XML) und automatische Zuordnung zu offenen Rechnungen."""
import csv
import hashlib
import io
import re
import xml.etree.ElementTree as ET
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from django.db import transaction

from apps.verkauf.models import Beleg, Belegart

from . import services
from .models import Bankumsatz, Zahlart

RECHNUNGSNR = re.compile(r"RE\s?(\d{4})\s?-?\s?(\d{5})", re.IGNORECASE)

# mögliche Spaltennamen in Bank-CSV-Dateien (klein geschrieben, Umlaute wie in der Datei)
SPALTEN = {
    "datum": ("buchungstag", "buchungsdatum", "datum", "buchung", "valuta"),
    "betrag": ("betrag", "umsatz", "betrag (eur)", "betrag in eur"),
    "name": ("name zahlungsbeteiligter", "beguenstigter/zahlungspflichtiger", "begünstigter/zahlungspflichtiger",
             "auftraggeber/empfänger", "auftraggeber", "name", "zahlungspflichtiger"),
    "iban": ("iban zahlungsbeteiligter", "iban", "kontonummer/iban", "iban auftraggeber"),
    "zweck": ("verwendungszweck", "buchungstext", "zweck", "verwendungszweck/buchungstext"),
}


class ImportFehler(Exception):
    pass


def zahl(text) -> Decimal:
    t = str(text).strip().replace(" ", "")
    if "," not in t and t.count(".") == 1 and len(t.split(".")[1]) == 3 and t.lstrip("+-").split(".")[0] != "0":
        raise ImportFehler(f"Betrag mehrdeutig (Tausenderpunkt oder Dezimalpunkt?): {text}")
    if "," in t:
        t = t.replace(".", "").replace(",", ".")
    try:
        wert = Decimal(t)
    except InvalidOperation:
        raise ImportFehler(f"Betrag nicht lesbar: {text}")
    if not wert.is_finite():
        raise ImportFehler(f"Betrag nicht lesbar: {text}")
    return wert


def tag(text) -> date:
    t = str(text).strip()[:10]
    for fmt in ("%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(t, fmt).date()
        except ValueError:
            pass
    raise ImportFehler(f"Datum nicht lesbar: {text}")


def lese_csv(inhalt: bytes) -> list:
    try:
        text = inhalt.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = inhalt.decode("cp1252")
    zeilen = text.splitlines()
    # Kopfzeile suchen (manche Banken schreiben Vorspann)
    start = next((i for i, z in enumerate(zeilen) if any(k in z.lower() for k in ("buchungstag", "buchungsdatum", "datum"))
                  and any(k in z.lower() for k in ("betrag", "umsatz"))), None)
    if start is None:
        raise ImportFehler("Kopfzeile mit Datum und Betrag nicht gefunden.")
    trenner = ";" if zeilen[start].count(";") >= zeilen[start].count(",") else ","
    leser = csv.reader(zeilen[start:], delimiter=trenner)
    kopf = [k.strip().lower() for k in next(leser)]

    def spalte(name):
        return next((kopf.index(n) for n in SPALTEN[name] if n in kopf), None)

    i = {n: spalte(n) for n in SPALTEN}
    if i["datum"] is None or i["betrag"] is None:
        raise ImportFehler("Spalten für Datum und Betrag nicht erkannt.")
    umsaetze = []
    for z in leser:
        if not z or len(z) <= max(v for v in i.values() if v is not None) or not z[i["betrag"]].strip():
            continue
        wert = lambda n: z[i[n]].strip() if i[n] is not None else ""
        umsaetze.append({"datum": tag(z[i["datum"]]), "betrag": zahl(z[i["betrag"]]), "name": wert("name"),
                         "iban": wert("iban").replace(" ", ""), "zweck": wert("zweck")})
    return umsaetze


def lese_camt(inhalt: bytes) -> list:
    try:
        wurzel = ET.fromstring(inhalt)
    except ET.ParseError as fehler:
        raise ImportFehler(f"XML nicht lesbar: {fehler}")
    ns = {"c": wurzel.tag.split("}")[0].strip("{")} if "}" in wurzel.tag else {}
    p = (lambda n: f"c:{n}") if ns else (lambda n: n)

    def finde(knoten, pfad):
        return knoten.find("/".join(p(x) for x in pfad.split("/")), ns)

    def text(knoten, pfad):
        el = finde(knoten, pfad)
        return el.text.strip() if el is not None and el.text else ""

    umsaetze = []
    for ntry in wurzel.iter(("{%s}Ntry" % ns["c"]) if ns else "Ntry"):
        betrag = zahl(text(ntry, "Amt"))
        if text(ntry, "CdtDbtInd") == "DBIT":
            betrag = -betrag
        details = finde(ntry, "NtryDtls/TxDtls")
        gegen = "Dbtr" if betrag > 0 else "Cdtr"
        gegen_konto = "DbtrAcct" if betrag > 0 else "CdtrAcct"
        zweck = ""
        if details is not None:
            zweck = " ".join(e.text.strip() for e in details.iter() if e.tag.endswith("Ustrd") and e.text)
            if not zweck:
                zweck = text(details, "RmtInf/Strd/CdtrRefInf/Ref")
        umsaetze.append({"datum": tag(text(ntry, "BookgDt/Dt") or text(ntry, "ValDt/Dt")), "betrag": betrag,
                         "name": text(details, f"RltdPties/{gegen}/Nm") if details is not None else "",
                         "iban": text(details, f"RltdPties/{gegen_konto}/Id/IBAN") if details is not None else "",
                         "zweck": zweck or text(ntry, "AddtlNtryInf")})
    return umsaetze


def pruefsumme(mandant, u) -> str:
    roh = f"{mandant.pk}|{u['datum']}|{u['betrag']}|{u['iban']}|{u['zweck']}"
    return hashlib.sha256(roh.encode()).hexdigest()


def finde_rechnung(mandant, zweck: str):
    for jahr, nr in RECHNUNGSNR.findall(zweck or ""):
        beleg = Beleg.objects.filter(mandant=mandant, art=Belegart.RECHNUNG, abgeschlossen=True,
                                     nummer=f"RE{jahr}-{nr}").first()
        if beleg:
            return beleg
    return None


@transaction.atomic
def importiere(mandant, dateiname: str, inhalt: bytes, benutzer=None) -> dict:
    """Liest eine Bankdatei ein; Dubletten werden übersprungen, Eingänge automatisch zugeordnet."""
    umsaetze = lese_camt(inhalt) if dateiname.lower().endswith(".xml") else lese_csv(inhalt)
    ergebnis = {"gelesen": len(umsaetze), "neu": 0, "doppelt": 0, "automatisch": 0}
    gleiche = {}
    for u in umsaetze:
        summe = pruefsumme(mandant, u)
        nr = gleiche[summe] = gleiche.get(summe, -1) + 1  # zwei identische Umsätze in einer Datei sind echte Doppelbuchungen
        if nr:
            summe = hashlib.sha256(f"{summe}|{nr}".encode()).hexdigest()
        if Bankumsatz.objects.filter(mandant=mandant, pruefsumme=summe).exists():
            ergebnis["doppelt"] += 1
            continue
        umsatz = Bankumsatz.objects.create(mandant=mandant, buchungsdatum=u["datum"], betrag=u["betrag"],
                                           name=u["name"][:150], iban=u["iban"][:34], zweck=u["zweck"][:500],
                                           pruefsumme=summe, importiert_von=benutzer)
        ergebnis["neu"] += 1
        if zuordnen_automatisch(umsatz, benutzer):
            ergebnis["automatisch"] += 1
    return ergebnis


def zuordnen_automatisch(umsatz, benutzer=None) -> bool:
    """Eingang mit Rechnungsnummer im Verwendungszweck und passendem offenem Betrag wird sofort gebucht;
    sonst nur ein Vorschlag (Rechnung vermerkt)."""
    if umsatz.betrag <= 0:
        umsatz.status = Bankumsatz.IGNORIERT if umsatz.betrag < 0 else umsatz.status
        umsatz.save(update_fields=["status"])
        return False
    beleg = finde_rechnung(umsatz.mandant, umsatz.zweck)
    if beleg is None:
        return False
    umsatz.vorschlag = beleg
    if services.offen(beleg) == umsatz.betrag:
        zuordnen(umsatz, beleg, benutzer)
        return True
    umsatz.save(update_fields=["vorschlag"])
    return False


@transaction.atomic
def zuordnen(umsatz, beleg, benutzer=None):
    if umsatz.status == Bankumsatz.ZUGEORDNET:
        raise ValueError("Umsatz ist bereits zugeordnet.")
    zahlung = services.zahlung_buchen(beleg, umsatz.betrag, umsatz.buchungsdatum, Zahlart.UEBERWEISUNG,
                                      f"Bank: {umsatz.name}".strip()[:200], benutzer)
    umsatz.zahlung, umsatz.vorschlag, umsatz.status = zahlung, beleg, Bankumsatz.ZUGEORDNET
    umsatz.save(update_fields=["zahlung", "vorschlag", "status"])
