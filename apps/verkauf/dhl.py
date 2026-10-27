"""DHL Paket (Parcel DE Shipping API v2): Versandlabel und Sendungsnummer automatisch erzeugen, Sendung stornieren.

Zugangsdaten stehen nur in der .env (je Mandant, Kurzname in Großbuchstaben):
  DHL_API_KEY_<MANDANT>      API-Key der App im DHL-Entwicklerportal
  DHL_BENUTZER_<MANDANT>     Benutzername des Geschäftskundenportals (GKP)
  DHL_PASSWORT_<MANDANT>     Passwort des GKP-Benutzers
  DHL_ABRECHNUNG_<MANDANT>   Abrechnungsnummer (14 Stellen, z. B. 33333333330101)
  DHL_TEST=1                 optional: Testumgebung (Sandbox) verwenden"""
import base64
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

PRODUKTION = "https://api-eu.dhl.com/parcel/de/shipping/v2"
SANDBOX = "https://api-sandbox.dhl.com/parcel/de/shipping/v2"
ISO3 = {"DE": "DEU", "AT": "AUT", "CH": "CHE", "NL": "NLD", "BE": "BEL", "LU": "LUX", "FR": "FRA", "IT": "ITA", "ES": "ESP",
        "PL": "POL", "CZ": "CZE", "DK": "DNK", "SE": "SWE", "GB": "GBR", "IE": "IRL", "PT": "PRT", "HU": "HUN"}
PROFIL = "STANDARD_GRUPPENPROFIL"


class DhlFehler(Exception):
    pass


def zugang(mandant) -> dict:
    k = mandant.kurzname.upper()
    werte = {n: os.environ.get(f"DHL_{n.upper()}_{k}", "") for n in ("api_key", "benutzer", "passwort", "abrechnung")}
    fehlt = [f"DHL_{n.upper()}_{k}" for n, w in werte.items() if not w]
    if fehlt:
        raise DhlFehler("DHL-Zugang unvollständig: in der .env fehlt " + ", ".join(fehlt) + ".")
    werte["url"] = SANDBOX if os.environ.get("DHL_TEST") == "1" else PRODUKTION
    return werte


def strasse_hausnummer(text: str) -> tuple:
    treffer = re.match(r"^(.*?)[\s,]+(\d+\s*[a-zA-Z]?(?:\s*[-/]\s*\d+\s*[a-zA-Z]?)?)$", (text or "").strip())
    return (treffer.group(1).strip(), treffer.group(2).replace(" ", "")) if treffer else ((text or "").strip(), "")


def anschrift_zerlegen(text: str) -> dict:
    """Mehrzeilige Anschrift (Namen, Straße, „PLZ Ort“, optional Land) in DHL-Felder zerlegen."""
    zeilen = [z.strip() for z in (text or "").splitlines() if z.strip()]
    land = "DE"
    if len(zeilen) > 3 and re.fullmatch(r"[A-Z]{2}", zeilen[-1]):
        land = zeilen.pop()
    if len(zeilen) < 3:
        raise DhlFehler("Die Lieferanschrift braucht mindestens Name, Straße und „PLZ Ort“.")
    treffer = re.match(r"^(\S+)\s+(.+)$", zeilen[-1])
    if not treffer:
        raise DhlFehler(f"Zeile „{zeilen[-1]}“ ist nicht von der Form „PLZ Ort“.")
    strasse, haus = strasse_hausnummer(zeilen[-2])
    namen = zeilen[:-2]
    return {"name1": namen[0][:50], "name2": (namen[1] if len(namen) > 1 else "")[:50], "name3": (namen[2] if len(namen) > 2 else "")[:50],
            "addressStreet": strasse[:50], "addressHouse": haus[:10], "postalCode": treffer.group(1), "city": treffer.group(2)[:40],
            "country": ISO3.get(land, land)}


def auftrag(versand) -> dict:
    beleg, m = versand.beleg, versand.beleg.mandant
    zugangsdaten = zugang(m)
    empfaenger = anschrift_zerlegen(beleg.versandanschrift or beleg.rechnungsanschrift)
    ap = beleg.ansprechpartner
    mail = (ap.email if ap and ap.email else beleg.kunde.email) or ""
    if mail:
        empfaenger["email"] = mail
    strasse, haus = strasse_hausnummer(m.strasse)
    absender = {"name1": m.firmenname[:50], "addressStreet": strasse[:50], "addressHouse": haus[:10], "postalCode": m.plz,
                "city": m.ort[:40], "country": ISO3.get(m.land or "DE", "DEU")}
    inland = empfaenger["country"] == "DEU"
    sendung = {"product": "V01PAK" if inland else "V53WPAK", "billingNumber": zugangsdaten["abrechnung"],
               "refNo": (beleg.nummer or str(beleg.pk))[:35], "shipper": absender, "consignee": empfaenger,
               "details": {"weight": {"uom": "kg", "value": float(versand.gewicht_kg or 1)}}}
    return {"profile": PROFIL, "shipments": [sendung]}


def _anfrage(zugangsdaten, methode, pfad, daten=None):
    basis = base64.b64encode(f"{zugangsdaten['benutzer']}:{zugangsdaten['passwort']}".encode()).decode()
    kopf = {"dhl-api-key": zugangsdaten["api_key"], "Authorization": f"Basic {basis}", "Accept": "application/json",
            "Content-Type": "application/json"}
    anfrage = urllib.request.Request(zugangsdaten["url"] + pfad, method=methode, headers=kopf,
                                     data=json.dumps(daten).encode() if daten is not None else None)
    try:
        with urllib.request.urlopen(anfrage, timeout=30) as antwort:
            return json.loads(antwort.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as fehler:
        try:
            inhalt = json.loads(fehler.read().decode("utf-8"))
        except Exception:
            inhalt = {}
        raise DhlFehler(f"DHL meldet {fehler.code}: {meldungen(inhalt) or fehler.reason}")
    except (urllib.error.URLError, TimeoutError) as fehler:
        raise DhlFehler(f"DHL nicht erreichbar: {fehler}")


def meldungen(antwort: dict) -> str:
    texte = []
    for item in antwort.get("items", []) or []:
        for k in item.get("validationMessages", []) or []:
            texte.append(f"{k.get('property', '')}: {k.get('validationMessage', '')}".strip(": "))
        s = item.get("sstatus") or {}
        if s.get("title") and s.get("statusCode", 200) >= 300:
            texte.append(f"{s['title']} {s.get('detail', '')}".strip())
    st = antwort.get("status") or {}
    if st.get("detail") and not texte:
        texte.append(st["detail"])
    return "; ".join(t for t in texte if t)


def label_erzeugen(versand) -> None:
    """Fordert bei DHL das Label an und speichert Sendungsnummer und PDF-Label in der Versendung."""
    zugangsdaten = zugang(versand.beleg.mandant)
    antwort = _anfrage(zugangsdaten, "POST", "/orders?includeDocs=include&printFormat=910-300-700&validate=false", auftrag(versand))
    items = antwort.get("items") or []
    if not items or not items[0].get("shipmentNo"):
        raise DhlFehler("DHL hat kein Label geliefert: " + (meldungen(antwort) or "unbekannter Fehler"))
    item = items[0]
    versand.sendungsnummer = item["shipmentNo"]
    b64 = (item.get("label") or {}).get("b64")
    if b64:
        versand.label = base64.b64decode(b64)
    versand.dhl_erzeugt = True
    versand.save()


def stornieren(versand) -> None:
    zugangsdaten = zugang(versand.beleg.mandant)
    antwort = _anfrage(zugangsdaten, "DELETE", "/orders?" + urllib.parse.urlencode({"profile": PROFIL, "shipment": versand.sendungsnummer}))
    items = antwort.get("items") or []
    if items and (items[0].get("sstatus") or {}).get("statusCode", 200) >= 300:
        raise DhlFehler("Stornierung bei DHL nicht möglich: " + (meldungen(antwort) or "unbekannt"))
