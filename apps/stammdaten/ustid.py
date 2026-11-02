"""USt-ID online prüfen. Ausländische EU-Nummern bestätigt das BZSt (eVatR), mit Name und Ort als qualifizierte Abfrage; dafür ist
die eigene deutsche USt-ID des Mandanten Pflicht. Deutsche Nummern bestätigt das BZSt nicht, sie laufen über VIES der EU-Kommission.
Beide Dienste sind kostenlos und brauchen keinen Zugang in der .env."""
import http.client
import json
import re
import urllib.error
import urllib.request
from datetime import date

BZST = "https://api.evatr.vies.bzst.de/app/v1/abfrage"
VIES = "https://ec.europa.eu/taxation_customs/vies/rest-api/check-vat-number"
EU_LAENDER = {"AT", "BE", "BG", "CY", "CZ", "DE", "DK", "EE", "EL", "ES", "FI", "FR", "HR", "HU", "IE", "IT", "LT", "LU", "LV", "MT",
              "NL", "PL", "PT", "RO", "SE", "SI", "SK", "XI"}
FORMAT = re.compile(r"[A-Z]{2}[0-9A-Z+*]{2,12}")
ZEITLIMIT = 10  # Sekunden je Netzschritt; die Abfrage läuft im Webprozess, der Nutzer wartet auf die Antwort
NICHT_ERREICHBAR = "Der Prüfdienst ist zurzeit nicht erreichbar. Bitte später noch einmal versuchen."
EIGENE_FALSCH = "Der Prüfdienst lehnt die eigene USt-ID des Mandanten ab. Bitte unter Verwaltung → Mandanten prüfen."
BZST_GUELTIG = {"evatr-0000": "Die USt-ID ist gültig.",
                "evatr-2008": "Die USt-ID ist gültig. Zu Name und Anschrift liegt eine Besonderheit vor, Rückfragen beantwortet das BZSt."}
BZST_FORMAT = {"evatr-0005", "evatr-0012", "evatr-2003"}
BZST_EIGENE = {"evatr-0004", "evatr-0006", "evatr-2005"}


class UstIdFehler(Exception):
    """Keine Aussage zur Nummer möglich; es entsteht kein Nachweis."""


def normalisieren(text, land="") -> str:
    """Großschreibung ohne Trennzeichen; fehlt der Ländercode, kommt er aus dem Land des Partners. Griechenland heißt hier EL."""
    nummer = re.sub(r"[\s.\-/]", "", (text or "").upper())
    if nummer and not re.match(r"[A-Z]{2}", nummer):
        nummer = (land or "").strip().upper() + nummer
    return "EL" + nummer[2:] if nummer.startswith("GR") else nummer


def _senden(url, daten) -> dict:
    anfrage = urllib.request.Request(url, data=json.dumps(daten).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "Accept": "application/json"})
    try:
        try:
            with urllib.request.urlopen(anfrage, timeout=ZEITLIMIT) as antwort:
                inhalt = antwort.read()
        except urllib.error.HTTPError as fehler:  # das BZSt meldet „nicht vergeben“ und Formatfehler mit 4xx und Status im Inhalt
            inhalt = fehler.read()
        ergebnis = json.loads(inhalt.decode("utf-8"))
    except (OSError, ValueError, http.client.HTTPException):  # u. a. Zeitüberschreitung, TLS, ungültige Antwort
        raise UstIdFehler(NICHT_ERREICHBAR)
    if not isinstance(ergebnis, dict):
        raise UstIdFehler(NICHT_ERREICHBAR)
    return ergebnis


def _datum(text) -> str:
    try:
        return date.fromisoformat(str(text)[:10]).strftime("%d.%m.%Y")
    except ValueError:
        return str(text or "?")


def _bzst(nummer, eigene, firmenname, strasse, plz, ort) -> dict:
    daten = {"anfragendeUstid": eigene, "angefragteUstid": nummer}
    if firmenname and ort:  # sonst einfache Abfrage: nur die Gültigkeit
        daten.update({"firmenname": firmenname, "ort": ort})
        daten.update({k: v for k, v in (("strasse", strasse), ("plz", plz)) if v})
    antwort = _senden(BZST, daten)
    status = str(antwort.get("status", ""))
    if status == "evatr-0008" and "firmenname" in daten:  # Grenze der qualifizierten Abfragen: laut BZSt einfach neu anfragen
        return _bzst(nummer, eigene, "", "", "", "")
    if status in BZST_EIGENE:
        raise UstIdFehler(EIGENE_FALSCH)
    if status in BZST_GUELTIG:
        meldung = BZST_GUELTIG[status] + ("" if "firmenname" in daten else " Name und Anschrift wurden nicht abgeglichen.")
    elif status == "evatr-2001":
        meldung = "Die USt-ID ist nicht vergeben."
    elif status == "evatr-2002":
        meldung = f"Die USt-ID ist noch nicht gültig, erst ab {_datum(antwort.get('gueltigAb'))}."
    elif status == "evatr-2006":
        meldung = f"Die USt-ID ist nicht mehr gültig. Sie galt von {_datum(antwort.get('gueltigAb'))} bis {_datum(antwort.get('gueltigBis'))}."
    elif status in BZST_FORMAT:
        meldung = "Die USt-ID hat ein falsches Format."
    else:
        raise UstIdFehler(NICHT_ERREICHBAR)
    return {"dienst": "bzst", "gueltig": status in BZST_GUELTIG, "status": status, "meldung": meldung, "referenz": str(antwort.get("id", ""))[:100],
            "anfragende_ust_id": eigene, "auskunft": "", **{f: daten.get(f, "") for f in ("firmenname", "strasse", "plz", "ort")},
            **{f: str(antwort.get(n) or "")[:1] for f, n in
               (("erg_name", "ergFirmenname"), ("erg_strasse", "ergStrasse"), ("erg_plz", "ergPlz"), ("erg_ort", "ergOrt"))}}


def _vies(nummer, eigene) -> dict:
    daten = {"countryCode": nummer[:2], "vatNumber": nummer[2:]}
    if re.fullmatch(r"DE\d{9}", eigene):  # nur mit eigener Nummer vergibt VIES eine Abfragenummer
        daten.update({"requesterMemberStateCode": "DE", "requesterNumber": eigene[2:]})
    antwort = _senden(VIES, daten)
    if antwort.get("actionSucceed") is False or not isinstance(antwort.get("valid"), bool):  # kommt auch mit HTTP 200
        codes = {str(w.get("error", "")) for w in antwort.get("errorWrappers") or [] if isinstance(w, dict)}
        if "INVALID_INPUT" in codes:
            raise UstIdFehler("VIES nimmt die Nummer nicht an. Bitte die Schreibweise prüfen.")
        raise UstIdFehler(EIGENE_FALSCH if "INVALID_REQUESTER_INFO" in codes else NICHT_ERREICHBAR)
    gueltig = antwort["valid"]
    # Deutschland gibt über VIES weder Namen noch Anschrift heraus, die Felder stehen dann auf „---“
    auskunft = " · ".join(t for t in (str(antwort.get("name") or ""), str(antwort.get("address") or "")) if t.strip(" -"))
    return {"dienst": "vies", "gueltig": gueltig, "status": "gültig" if gueltig else "nicht gültig",
            "meldung": ("Die USt-ID ist gültig. Name und Anschrift bestätigt VIES bei deutschen Nummern nicht." if gueltig
                        else "Die USt-ID ist nicht gültig oder nicht vergeben."),
            "referenz": str(antwort.get("requestIdentifier") or "")[:100], "anfragende_ust_id": eigene if "requesterNumber" in daten else "",
            "auskunft": auskunft[:500], "erg_name": "", "erg_strasse": "", "erg_plz": "", "erg_ort": ""}


def pruefen(mandant, ust_id, land="", firmenname="", strasse="", plz="", ort="") -> dict:
    """Fragt den zuständigen Dienst und liefert die Felder für UstIdPruefung. UstIdFehler heißt: keine Aussage zur Nummer."""
    nummer = normalisieren(ust_id, land)
    if not nummer:
        raise UstIdFehler("Bitte zuerst die USt-ID eintragen.")
    if not FORMAT.fullmatch(nummer):
        raise UstIdFehler("Die USt-ID muss mit dem Ländercode beginnen, z. B. ATU12345678.")
    if nummer[:2] not in EU_LAENDER:
        raise UstIdFehler(f"Nur USt-IDs aus der EU lassen sich online prüfen, „{nummer[:2]}“ gehört nicht dazu.")
    eigene = normalisieren(mandant.ust_id, "DE")
    if nummer.startswith("DE"):
        ergebnis = _vies(nummer, eigene)
    elif re.fullmatch(r"DE\d{9}", eigene):
        ergebnis = _bzst(nummer, eigene, firmenname, strasse, plz, ort)
    else:
        raise UstIdFehler("Für ausländische USt-IDs fehlt die eigene deutsche USt-ID des Mandanten (Verwaltung → Mandanten).")
    return {"ust_id": nummer, **ergebnis}
