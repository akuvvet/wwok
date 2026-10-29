"""Deutsche Post INTERNETMARKE (REST v1): Briefmarke kaufen und als Etikett (z. B. DYMO 99014, 101 × 54 mm) drucken,
nicht genutzte Marken erstatten lassen.

Zugangsdaten stehen nur in der .env (je Mandant, Kurzname in Großbuchstaben):
  DHL_API_KEY_<MANDANT>              API Key der App im DHL-Entwicklerportal (dieselbe App wie für DHL Paket)
  DHL_API_SECRET_<MANDANT>           API Secret derselben App
  DP_PORTOKASSE_BENUTZER_<MANDANT>   Anmeldename (E-Mail) der Portokasse
  DP_PORTOKASSE_PASSWORT_<MANDANT>   Passwort der Portokasse (höchstens 22 Zeichen)
  DP_SEITENFORMAT_<MANDANT>          optional: ID des Druckformats; sonst wird DYMO 99014 / 101 × 54 mm gesucht

In der Portokasse muss die Geschäftsanwendung freigegeben sein („Meine Daten → Geschäftsanwendungen“)."""
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal

from django.utils import timezone

from .dhl import ISO3, DhlFehler, anschrift_zerlegen, strasse_hausnummer
from .models import Versand

URL = "https://api-eu.dhl.com/post/de/shipping/im/v1"
ETIKETT_MM = (101, 54)  # DYMO 99014


class PostFehler(Exception):
    """Fehler der INTERNETMARKE. unklar=True: die Anfrage kam evtl. an, die Antwort aber nicht (Zeitüberschreitung,
    Verbindungsabbruch, unlesbare Antwort) – bei einem Kauf ist dann offen, ob die Marke bezahlt wurde."""

    def __init__(self, text, unklar=False):
        super().__init__(text)
        self.unklar = unklar


def zugang(mandant) -> dict:
    k = mandant.kurzname.upper()
    namen = {"api_key": f"DHL_API_KEY_{k}", "api_secret": f"DHL_API_SECRET_{k}",
             "benutzer": f"DP_PORTOKASSE_BENUTZER_{k}", "passwort": f"DP_PORTOKASSE_PASSWORT_{k}"}
    werte = {n: os.environ.get(v, "") for n, v in namen.items()}
    fehlt = [namen[n] for n, w in werte.items() if not w]
    if fehlt:
        raise PostFehler("INTERNETMARKE-Zugang unvollständig: in der .env fehlt " + ", ".join(fehlt) + ".")
    werte["seitenformat"] = os.environ.get(f"DP_SEITENFORMAT_{k}", "")
    return werte


def meldung(inhalt) -> str:
    if isinstance(inhalt, dict):
        teile = [str(inhalt[f]) for f in ("title", "detail", "description", "message") if inhalt.get(f)]
        return "; ".join(teile)
    return ""


def _senden(anfrage):
    try:
        with urllib.request.urlopen(anfrage, timeout=30) as antwort:
            return antwort.read()
    except urllib.error.HTTPError as fehler:
        try:
            inhalt = json.loads(fehler.read().decode("utf-8"))
        except Exception:
            inhalt = {}
        raise PostFehler(f"Deutsche Post meldet {fehler.code}: {meldung(inhalt) or fehler.reason}")
    except (urllib.error.URLError, OSError, ValueError) as fehler:
        raise PostFehler(f"INTERNETMARKE nicht erreichbar: {fehler}", unklar=True)


def token(z) -> str:
    daten = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": z["api_key"],
                                    "client_secret": z["api_secret"], "username": z["benutzer"],
                                    "password": z["passwort"]}).encode()
    anfrage = urllib.request.Request(URL + "/user", data=daten, method="POST",
                                     headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
    antwort = json.loads(_senden(anfrage).decode("utf-8") or "{}")
    if not antwort.get("access_token"):
        raise PostFehler("Anmeldung an der Portokasse fehlgeschlagen: " + (meldung(antwort) or "kein Token"))
    return antwort["access_token"]


def _json(tok, methode, pfad, daten=None) -> dict:
    anfrage = urllib.request.Request(URL + pfad, method=methode, data=json.dumps(daten).encode() if daten is not None else None,
                                     headers={"Authorization": f"Bearer {tok}", "Accept": "application/json",
                                              "Content-Type": "application/json"})
    inhalt = _senden(anfrage)
    try:
        return json.loads(inhalt.decode("utf-8") or "{}")
    except ValueError:
        raise PostFehler("Unlesbare Antwort der INTERNETMARKE.", unklar=True)


def seitenformat(tok, z) -> int:
    """ID des Druckformats: aus der .env oder das Etikettenformat DYMO 99014 (101 × 54 mm) aus dem Katalog."""
    if z["seitenformat"]:
        if not z["seitenformat"].isdigit():
            raise PostFehler("DP_SEITENFORMAT in der .env muss eine Zahl sein.")
        return int(z["seitenformat"])
    formate = _json(tok, "GET", "/app/catalog?types=PAGE_FORMATS").get("pageFormats") or []
    for f in formate:
        if "99014" in (f.get("name") or ""):
            return f["id"]
    for f in formate:
        groesse = ((f.get("pageLayout") or {}).get("size") or {})
        masse = sorted([round(groesse.get("x") or 0), round(groesse.get("y") or 0)])
        if f.get("pageType") == "LABELPRINTER" and masse == sorted(ETIKETT_MM):
            return f["id"]
    etiketten = ", ".join(f"{f.get('id')}: {f.get('name')}" for f in formate if f.get("pageType") == "LABELPRINTER")
    raise PostFehler("Druckformat DYMO 99014 (101 × 54 mm) nicht gefunden. Bitte DP_SEITENFORMAT in der .env setzen"
                     + (f" – verfügbare Etikettenformate: {etiketten}" if etiketten else "."))


def zeile(strasse: str, haus: str, laenge=50) -> str:
    """Straße + Hausnummer auf die Feldlänge kürzen, ohne die Hausnummer abzuschneiden."""
    if not haus:
        return strasse[:laenge]
    return f"{strasse[:laenge - len(haus) - 1]} {haus}"


def adressen(beleg) -> dict:
    m = beleg.mandant
    try:
        empf = anschrift_zerlegen(beleg.versandanschrift or beleg.rechnungsanschrift)
    except DhlFehler as fehler:
        raise PostFehler(str(fehler))
    if empf["country"] != "DEU":
        raise PostFehler("Briefe mit INTERNETMARKE sind hier nur innerhalb Deutschlands eingerichtet.")
    if not (len(empf["postalCode"]) == 5 and empf["postalCode"].isdigit()):
        raise PostFehler(f"Postleitzahl „{empf['postalCode']}“ ist keine deutsche PLZ.")
    strasse, haus = strasse_hausnummer(m.strasse)
    if not (m.strasse and m.plz and m.ort):
        raise PostFehler("Absender unvollständig: bitte Straße, PLZ und Ort beim Mandanten pflegen.")
    if not (len(m.plz.strip()) == 5 and m.plz.strip().isdigit()):
        raise PostFehler(f"Absender-PLZ „{m.plz}“ des Mandanten ist keine deutsche PLZ.")
    absender = {"name": m.firmenname[:50], "addressLine1": zeile(strasse, haus), "postalCode": m.plz.strip(),
                "city": m.ort[:40], "country": ISO3.get(m.land or "DE", "DEU")}
    empfaenger = {"name": empf["name1"][:50], "addressLine1": zeile(empf["addressStreet"], empf.get("addressHouse", "")),
                  "postalCode": empf["postalCode"], "city": empf["city"][:40], "country": "DEU"}
    zusatz = ", ".join(n for n in (empf.get("name2"), empf.get("name3")) if n)  # z. H. …, Abteilung …
    if zusatz:
        empfaenger["additionalName"] = zusatz[:40]
    return {"sender": absender, "receiver": empfaenger}


def cent(betrag: Decimal) -> int:
    return int((betrag * 100).to_integral_value())


def marke_kaufen(versand) -> None:
    """Kauft die Briefmarke aus der Portokasse und speichert Etikett (PDF), Marken-ID und Bestellnummer im Versand.

    Der Versand ist vorher als „Kauf läuft“ gespeichert (siehe View) – so sperrt er parallele Käufe. Ergebnis:
    - Vorprüfung oder Kauf von der Post abgelehnt: PostFehler (unklar=False), es wurde nichts bezahlt.
    - Antwort auf den Kauf verloren/unlesbar: Versand bleibt als „Kauf unklar“ stehen, PostFehler(unklar=True) –
      die Portokasse muss geprüft werden, bevor erneut gekauft wird.
    - Erfolg: Marken-ID und Bestellnummer sind gespeichert, bevor das Etikett geladen wird."""
    beleg, produkt = versand.beleg, versand.briefprodukt
    if produkt is None or not produkt.aktiv:
        raise PostFehler("Bitte ein Briefprodukt wählen.")
    try:  # alles vor dem Kauf: ein Fehler hier kostet nichts
        z = zugang(beleg.mandant)
        daten_adressen = adressen(beleg)
        tok = token(z)
        format_id = seitenformat(tok, z)
    except PostFehler as fehler:
        raise PostFehler(str(fehler))
    auftrag = {"type": "AppShoppingCartPDFRequest", "total": cent(produkt.preis), "pageFormatId": format_id, "dpi": "DPI300",
               "positions": [{"productCode": produkt.produktcode, "voucherLayout": "ADDRESS_ZONE", "positionType": "AppShoppingCartPDFPosition",
                              "address": daten_adressen, "position": {"labelX": 1, "labelY": 1, "page": 1}}]}
    try:
        antwort = _json(tok, "POST", "/app/shoppingcart/pdf?directCheckout=true", auftrag)
    except PostFehler as fehler:
        if fehler.unklar:
            unklar_vermerken(versand)
            raise PostFehler(f"{fehler} – ob die Marke bezahlt wurde, ist unklar. Bitte in der Portokasse prüfen, "
                             "bevor erneut gekauft wird.", unklar=True)
        if "total" in str(fehler).lower() or "price" in str(fehler).lower():
            raise PostFehler(f"{fehler} – stimmt das Porto für {produkt.name} noch? (Verwaltung → Briefprodukte)")
        raise
    korb = antwort.get("shoppingCart") or {}
    marken = korb.get("voucherList") or []
    versand.shop_order_id = str(korb.get("shopOrderId") or "")[:20]
    versand.sendungsnummer = (marken[0].get("voucherId") or "")[:40] if marken else ""  # Marken-ID (für Erstattung)
    versand.label_link = (antwort.get("link") or "")[:500]
    versand.dienst, versand.porto = "dp", produkt.preis
    if not versand.label_link or not marken:  # Antwort ohne Marke: Kauf nicht sicher gescheitert
        unklar_vermerken(versand)
        raise PostFehler("Die Post hat keine vollständige Marke geliefert: " + (meldung(antwort) or "unbekannte Antwort")
                         + " – bitte in der Portokasse prüfen.", unklar=True)
    versand.kauf_status = ""
    versand.save()  # Kauf festhalten, bevor das Etikett geladen wird
    etikett_laden(versand)


def pruefen(mandant) -> list:
    """Prüft Zugang, Guthaben, Druckformat und Produktcodes über die kostenlose Vorschau – es wird nichts gekauft."""
    from .models import Briefprodukt
    z = zugang(mandant)
    daten = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": z["api_key"], "client_secret": z["api_secret"],
                                    "username": z["benutzer"], "password": z["passwort"]}).encode()
    antwort = json.loads(_senden(urllib.request.Request(URL + "/user", data=daten, method="POST", headers={
        "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})).decode("utf-8") or "{}")
    tok = antwort.get("access_token")
    if not tok:
        raise PostFehler("Anmeldung an der Portokasse fehlgeschlagen: " + (meldung(antwort) or "kein Token"))
    zeilen = [f"Anmeldung ok, Guthaben Portokasse: {Decimal(antwort.get('walletBalance') or 0) / 100:.2f} €"]
    zeilen.append(f"Druckformat für DYMO 99014: ID {seitenformat(tok, z)}")
    for p in Briefprodukt.objects.filter(aktiv=True):
        try:
            _json(tok, "POST", "/app/shoppingcart/png?validate=true", {"type": "AppShoppingCartPreviewPNGRequest",
                                                                     "productCode": p.produktcode, "voucherLayout": "ADDRESS_ZONE"})
            zeilen.append(f"{p.name} (Code {p.produktcode}): Vorschau ok")
        except PostFehler as fehler:
            zeilen.append(f"{p.name} (Code {p.produktcode}): FEHLER {fehler}")
    return zeilen


def unklar_vermerken(versand) -> None:
    versand.kauf_status = Versand.UNKLAR
    versand.save()


def etikett_laden(versand) -> bytes:
    """Lädt das Etikett-PDF über den Link der Post (auch nachträglich, falls der erste Abruf scheiterte)."""
    if not versand.label_link:
        raise PostFehler("Zu diesem Brief gibt es keinen Etikett-Link.")
    pdf = _senden(urllib.request.Request(versand.label_link, headers={"Accept": "application/pdf"}))
    if not pdf.startswith(b"%PDF"):
        raise PostFehler("Die Post hat kein PDF geliefert.")
    versand.label = pdf
    versand.save(update_fields=["label"])
    return pdf


def erstatten(versand) -> None:
    """Beantragt die Erstattung einer nicht genutzten Marke (die Post bucht den Betrag später in die Portokasse zurück)."""
    if not versand.shop_order_id:
        raise PostFehler("Zu diesem Brief fehlt die Bestellnummer – Erstattung nur im Portokassen-Portal möglich.")
    z = zugang(versand.beleg.mandant)
    tok = token(z)
    korb = {"shopOrderId": versand.shop_order_id}
    if versand.sendungsnummer:
        korb["voucherList"] = [{"voucherId": versand.sendungsnummer}]
    _json(tok, "POST", "/app/retoure", {"shoppingCart": korb})
    versand.erstattung_beantragt = timezone.now()
    versand.save(update_fields=["erstattung_beantragt"])
