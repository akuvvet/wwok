"""Bankauszug-Import (CSV und CAMT.053-XML) und automatische Zuordnung zu offenen Rechnungen."""
import csv
import hashlib
import io
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from django.db import transaction

from apps.verkauf.models import Beleg, Belegart

from . import services
from .models import Bankumsatz, Lastschrift, Zahlart

LASTSCHRIFT_TAGE = 5  # so weit darf die Gutschrift der Bank vom Einzugsdatum abweichen, wenn die Referenz im Auszug fehlt
RUECKGEBUEHR_MAX = Decimal("20")  # Rücklastschrift: die Bank belastet oft Betrag + Gebühr in einer Summe

RECHNUNGSNR = re.compile(r"(RE|PF)\s?(\d{4})\s?-?\s?(\d{5,})", re.IGNORECASE)  # Rechnung oder Proforma (Vorkasse)

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
                         "zweck": zweck or text(ntry, "AddtlNtryInf"),
                         "referenz": " ".join(dict.fromkeys(e.text.strip() for e in ntry.iter() if e.text and e.text.strip()
                                                            and e.tag.rsplit("}", 1)[-1] in ("PmtInfId", "EndToEndId")))})
    return umsaetze


def pruefsumme(mandant, u) -> str:
    roh = f"{mandant.pk}|{u['datum']}|{u['betrag']}|{u['iban']}|{u['zweck']}"
    return hashlib.sha256(roh.encode()).hexdigest()


def finde_rechnungen(mandant, zweck: str) -> list:
    """Alle Rechnungen/Proformas, deren Nummer im Text steht, in der Reihenfolge des Textes (jede einmal)."""
    gefunden = []
    for art, jahr, nr in RECHNUNGSNR.findall(zweck or ""):
        art = art.upper()
        beleg = Beleg.objects.filter(mandant=mandant, art=art, abgeschlossen=True, nummer=f"{art}{jahr}-{nr}") \
            .exclude(folgebelege__art=Belegart.PROFORMA_STORNO).first()
        if beleg and beleg not in gefunden:
            gefunden.append(beleg)
    return gefunden


def finde_rechnung(mandant, zweck: str):
    treffer = finde_rechnungen(mandant, zweck)
    return treffer[0] if treffer else None


def gutschrift_treffer(umsatz, gutschriften=None) -> list:
    """Offene Lieferanten-Gutschriften, deren Restguthaben genau dem Eingang entspricht und die im Text vorkommen:
    [(Gutschrift, Nummer_im_Text)]. Nummer im Verwendungszweck (ganzes Wort) ist sicher und wird automatisch gebucht; passt nur
    der Name oder Kurzname des Lieferanten (Auftraggeber oder Verwendungszweck), bleibt es ein Vorschlag zur Prüfung."""
    if umsatz.betrag <= 0:
        return []
    from apps.einkauf import services as einkauf
    if gutschriften is None:
        gutschriften = einkauf.offene_gutschriften(umsatz.mandant)
    text = f"{umsatz.zweck} {umsatz.referenz} {umsatz.name}"
    treffer = []
    for g in gutschriften:
        if g.restguthaben != umsatz.betrag:
            continue
        nr = bool(re.search(r"(?<![A-Za-z0-9])" + re.escape(g.nummer) + r"(?![A-Za-z0-9])", text, re.IGNORECASE))
        namen = [n for n in (g.lieferant.name1, g.lieferant.kurzname) if n and len(n.strip()) >= 3]
        if nr or any(re.search(r"(?<![A-Za-z0-9])" + re.escape(n.strip()) + r"(?![A-Za-z0-9])", text, re.IGNORECASE) for n in namen):
            treffer.append((g, nr))
    return sorted(treffer, key=lambda t: not t[1])


def gutschrift_zuordnen(umsatz, gutschrift, benutzer=None):
    """Zahlungseingang einer Lieferanten-Gutschrift zuordnen: dieselbe Zahlung wie der Knopf „Zahlung erhalten buchen“
    (Betrag und Datum vom Umsatz, höchstens das Restguthaben)."""
    from apps.einkauf import services as einkauf
    zahlung = einkauf.gutschrift_zahlung_buchen(gutschrift, None, None, benutzer, bankumsatz=umsatz)
    umsatz.refresh_from_db()
    return zahlung


def aufteilen(betrag, belege) -> tuple:
    """Verteilt einen Eingang der Reihe nach auf die offenen Beträge: ([(Beleg, Teilbetrag)], Rest). Rest > 0 = Überzahlung."""
    teile, rest = [], betrag
    for b in belege:
        if rest <= 0:
            break
        offen = services.offen(b)
        if offen <= 0:
            continue
        teil = min(rest, offen)
        teile.append((b, teil))
        rest -= teil
    return teile, rest


@transaction.atomic
def importiere(mandant, dateiname: str, inhalt: bytes, benutzer=None) -> dict:
    """Liest eine Bankdatei ein; Dubletten werden übersprungen, Eingänge automatisch zugeordnet."""
    umsaetze = lese_camt(inhalt) if dateiname.lower().endswith(".xml") else lese_csv(inhalt)
    ergebnis = {"gelesen": len(umsaetze), "neu": 0, "doppelt": 0, "automatisch": 0}
    neu = []
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
                                           referenz=u.get("referenz", "")[:200],
                                           pruefsumme=summe, importiert_von=benutzer)
        ergebnis["neu"] += 1
        neu.append(umsatz)
    for umsatz in sorted(neu, key=lambda u: u.betrag < 0):  # erst Eingänge: eine Rücklastschrift braucht die gebuchte Gutschrift
        if zuordnen_automatisch(umsatz, benutzer):
            ergebnis["automatisch"] += 1
    return ergebnis


def finde_lastschriften(umsatz) -> list:
    """Eingereichte, noch nicht gebuchte Lastschriften, deren Sammelgutschrift dieser Eingang ist. Die Bank schreibt je Datei und
    Sequenz (Erst/Folge) eine Summe gut. Treffer: Referenz der Datei im Auszug und Summe passt; ohne Referenz nur, wenn Summe und
    Einzugsdatum zu genau einer Sammelbuchung passen."""
    gruppen, je_datei = defaultdict(list), defaultdict(list)
    for ls in Lastschrift.objects.filter(mandant=umsatz.mandant, aufgehoben=False, zahlung__isnull=True).select_related("beleg").order_by("id"):
        gruppen[(ls.datei, ls.sequenz)].append(ls)
        je_datei[ls.datei].append(ls)
    passend = [liste for liste in list(gruppen.values()) + list(je_datei.values())
               if sum((ls.betrag for ls in liste), Decimal("0")) == umsatz.betrag]
    text = f"{umsatz.zweck} {umsatz.referenz}".upper()
    mit_referenz = [liste for liste in passend if liste[0].datei and liste[0].datei[:30].upper() in text]
    if mit_referenz:
        return mit_referenz[0]
    nah = {tuple(ls.pk for ls in liste): liste for liste in passend
           if all(abs((umsatz.buchungsdatum - ls.einzugsdatum).days) <= LASTSCHRIFT_TAGE for ls in liste)}
    return next(iter(nah.values())) if len(nah) == 1 else []


def lastschriften_buchen(umsatz, lastschriften, benutzer=None) -> list:
    """Sammelgutschrift der Bank: je eingezogener Rechnung ein Zahlungseingang (Zahlart Lastschrift) zum Buchungsdatum."""
    zahlungen = []
    for ls in lastschriften:
        z = services.zahlung_buchen(ls.beleg, ls.betrag, umsatz.buchungsdatum, Zahlart.LASTSCHRIFT,
                                    f"Lastschrift-Einzug vom {ls.einzugsdatum:%d.%m.%Y}", benutzer, aus_bankumsatz=umsatz)
        ls.zahlung = z
        ls.save(update_fields=["zahlung"])
        zahlungen.append(z)
    umsatz.zahlung, umsatz.vorschlag, umsatz.status = zahlungen[0], lastschriften[0].beleg, Bankumsatz.ZUGEORDNET
    umsatz.save(update_fields=["zahlung", "vorschlag", "status"])
    return zahlungen


def ruecklastschrift(umsatz, benutzer=None) -> bool:
    """Belastung mit der Rechnungsnummer einer eingezogenen Lastschrift: Zahlung stornieren, Lastschrift aufheben – die Rechnung ist
    wieder offen. Ist die Gutschrift noch nicht eingelesen, bleibt der Umsatz offen (mit Vorschlag) und wird beim Nachbuchen erledigt."""
    for beleg in finde_rechnungen(umsatz.mandant, f"{umsatz.zweck} {umsatz.referenz}"):
        ls = Lastschrift.objects.filter(mandant=umsatz.mandant, beleg=beleg, aufgehoben=False).order_by("-id").first()
        if ls is None or not (ls.betrag <= -umsatz.betrag <= ls.betrag + RUECKGEBUEHR_MAX):
            continue
        umsatz.vorschlag = beleg
        if ls.zahlung_id is None:
            umsatz.save(update_fields=["vorschlag"])
            return False
        services.lastschrift_aufheben(ls, f"Rücklastschrift laut Kontoauszug vom {umsatz.buchungsdatum:%d.%m.%Y}", benutzer,
                                      datum=umsatz.buchungsdatum)
        umsatz.status = Bankumsatz.ZUGEORDNET
        umsatz.save(update_fields=["vorschlag", "status"])
        return True
    return False


def zuordnen_automatisch(umsatz, benutzer=None) -> bool:
    """Eingang mit Rechnungsnummer(n) im Verwendungszweck wird sofort gebucht, wenn er die offenen Beträge nicht übersteigt:
    genau passend, als Teilzahlung oder der Reihe nach auf mehrere Rechnungen verteilt. Eine Überzahlung bleibt Vorschlag."""
    if umsatz.betrag < 0:
        if ruecklastschrift(umsatz, benutzer):
            return True
        if umsatz.vorschlag_id is None:  # sonstige Abbuchung; eine Rücklastschrift ohne gebuchte Gutschrift bleibt offen
            umsatz.status = Bankumsatz.IGNORIERT
            umsatz.save(update_fields=["status"])
        return False
    if umsatz.betrag == 0:
        return False
    lastschriften = finde_lastschriften(umsatz)
    if lastschriften:
        lastschriften_buchen(umsatz, lastschriften, benutzer)
        return True
    belege = finde_rechnungen(umsatz.mandant, umsatz.zweck)
    if not belege:  # keine Kundenrechnung: vielleicht zahlt ein Lieferant eine Gutschrift aus
        sicher = [g for g, nr_im_text in gutschrift_treffer(umsatz) if nr_im_text]
        if len(sicher) == 1:
            try:
                gutschrift_zuordnen(umsatz, sicher[0], benutzer)
            except ValueError:  # z. B. Restguthaben inzwischen verrechnet: bleibt offen zur Prüfung
                return False
            return True
        return False
    umsatz.vorschlag = belege[0]
    teile, rest = aufteilen(umsatz.betrag, belege)
    if teile and rest <= 0:
        zuordnen(umsatz, belege, benutzer)
        return True
    umsatz.save(update_fields=["vorschlag"])
    return False


@transaction.atomic
def zuordnen(umsatz, belege, benutzer=None) -> list:
    """Bucht den Umsatz auf eine oder mehrere Rechnungen (der Reihe nach bis zum offenen Betrag; was über alle offenen
    Beträge hinausgeht, bleibt als Überzahlung auf der letzten). Rückgabe: [(Beleg, Betrag)]."""
    belege = list(belege) if isinstance(belege, (list, tuple)) else [belege]
    umsatz = Bankumsatz.objects.select_for_update().get(pk=umsatz.pk)
    if umsatz.status == Bankumsatz.ZUGEORDNET or umsatz.zahlungen.filter(storno_zu__isnull=True, storniert_durch__isnull=True).exists():
        raise ValueError("Umsatz ist bereits zugeordnet.")
    if not belege:
        raise ValueError("Bitte eine Rechnung angeben.")
    teile, rest = aufteilen(umsatz.betrag, belege)
    if rest > 0:  # Überzahlung (oder alles schon bezahlt): bleibt als Guthaben auf der letzten Rechnung
        if teile and teile[-1][0] == belege[-1]:
            teile[-1] = (belege[-1], teile[-1][1] + rest)
        else:
            teile.append((belege[-1], rest))
    zahlungen = []
    for beleg, teil in teile:
        zahlungen.append(services.zahlung_buchen(beleg, teil, umsatz.buchungsdatum, Zahlart.UEBERWEISUNG,
                                                 f"Bank: {umsatz.name}".strip()[:200], benutzer, aus_bankumsatz=umsatz))
    umsatz.zahlung, umsatz.vorschlag, umsatz.status = zahlungen[0], teile[0][0], Bankumsatz.ZUGEORDNET
    umsatz.save(update_fields=["zahlung", "vorschlag", "status"])
    return teile


def vorschlaege_nachbuchen(mandant, benutzer=None) -> int:
    """Offene Eingänge mit Rechnungsnummer erneut prüfen und buchen (z. B. Teilzahlungen aus älteren Importen)."""
    anzahl = 0
    offene = Bankumsatz.objects.filter(mandant=mandant, status=Bankumsatz.OFFEN).order_by("buchungsdatum", "id")
    # erst Eingänge, dann Rücklastschriften, die auf ihre Gutschrift gewartet haben
    for umsatz in list(offene.filter(betrag__gt=0)) + list(offene.filter(betrag__lt=0, vorschlag__isnull=False)):
        if zuordnen_automatisch(umsatz, benutzer):
            anzahl += 1
    return anzahl
