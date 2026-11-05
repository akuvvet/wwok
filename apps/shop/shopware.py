"""Schlanker Client für die Shopware-6-Admin-API (OAuth client_credentials, JSON)."""
import http.client
import json
import os
import time
import urllib.error
import urllib.request

STANDARD_WAEHRUNG_EUR = "b7d2554b0ce847cd82f3ac9bd1c0dfca"  # feste ID der Standardwährung in Shopware 6
STOREFRONT_TYP = "8a243080f92e4c719546314b577cf82b"  # feste ID des Verkaufskanal-Typs Storefront
BILDTYPEN = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "gif": "image/gif", "webp": "image/webp"}


class ShopFehler(Exception):
    def __init__(self, meldung="", status=None):
        super().__init__(meldung)
        self.status = status


class ShopwareClient:
    def __init__(self, shop):
        self.shop = shop
        self.basis = shop.url.rstrip("/")
        self._token = None
        self._ablauf = 0
        self.wartezeit = 60
        self._medienordner = False

    # -- HTTP (in Tests ersetzt) ---------------------------------------------------------------------
    def _anfrage(self, methode: str, pfad: str, daten=None, kopf=None):
        header = {"Accept": "application/json", "Content-Type": "application/json", **(kopf or {})}
        roh = isinstance(daten, bytes)  # Bilder gehen unverändert als Dateiinhalt hinaus
        anfrage = urllib.request.Request(self.basis + pfad, method=methode, headers=header,
                                         data=daten if roh else json.dumps(daten).encode() if daten is not None else None)
        try:
            with urllib.request.urlopen(anfrage, timeout=self.wartezeit) as antwort:
                inhalt = antwort.read()
                return json.loads(inhalt) if inhalt else {}
        except urllib.error.HTTPError as fehler:
            raise ShopFehler(f"Shop antwortet mit {fehler.code}: {fehler.read()[:300].decode('utf-8', 'replace')}", fehler.code)
        except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as fehler:
            raise ShopFehler(f"Shop nicht erreichbar: {fehler}")

    def _zugang(self) -> str:
        if self._token and time.time() < self._ablauf - 30:
            return self._token
        secret = os.environ.get(self.shop.secret_variable, "")
        if not secret:
            raise ShopFehler(f"Das Zugangs-Geheimnis fehlt: {self.shop.secret_variable} in der .env eintragen.")
        antwort = self._anfrage("POST", "/api/oauth/token", {
            "grant_type": "client_credentials", "client_id": self.shop.client_id, "client_secret": secret})
        if "access_token" not in antwort:
            raise ShopFehler("Anmeldung am Shop fehlgeschlagen (Zugangsdaten prüfen).")
        self._token, self._ablauf = antwort["access_token"], time.time() + int(antwort.get("expires_in", 600))
        return self._token

    def api(self, methode: str, pfad: str, daten=None, kopf=None):
        return self._anfrage(methode, pfad, daten, {"Authorization": f"Bearer {self._zugang()}", **(kopf or {})})

    def _alle(self, entitaet: str, felder: list) -> list:
        """Alle Datensätze einer Entität, seitenweise."""
        ergebnis, seite = [], 1
        while True:
            daten = self.api("POST", f"/api/search/{entitaet.replace('_', '-')}", {"page": seite, "limit": 500, "includes": {entitaet: felder}}).get("data", [])
            ergebnis += daten
            if len(daten) < 500:
                return ergebnis
            seite += 1

    # -- Fachfunktionen -----------------------------------------------------------------------------
    def bestellungen(self, seit=None, seite: int = 1, limit: int = 50) -> dict:
        filter_ = [{"type": "range", "field": "createdAt", "parameters": {"gte": seit.strftime("%Y-%m-%dT%H:%M:%S+00:00")}}] if seit else []
        adresse = {"associations": {"country": {}}}
        return self.api("POST", "/api/search/order", {
            "page": seite, "limit": limit, "filter": filter_, "sort": [{"field": "createdAt", "order": "ASC"}],
            "total-count-mode": 1,
            "associations": {
                "lineItems": {}, "orderCustomer": {}, "currency": {}, "stateMachineState": {},
                "addresses": adresse,
                "deliveries": {"associations": {"shippingOrderAddress": adresse, "shippingMethod": {}}},
                "transactions": {"associations": {"paymentMethod": {}, "stateMachineState": {}}}}})

    def produkt_ids(self, nummern: list) -> dict:
        """productNumber → Produkt-ID im Shop."""
        ergebnis = {}
        for i in range(0, len(nummern), 100):
            teil = nummern[i:i + 100]
            antwort = self.api("POST", "/api/search/product", {
                "limit": len(teil), "filter": [{"type": "equalsAny", "field": "productNumber", "value": "|".join(teil)}],
                "includes": {"product": ["id", "productNumber"]}})
            ergebnis.update({p["productNumber"]: p["id"] for p in antwort.get("data", []) if p.get("productNumber")})
        return ergebnis

    def produkte_aktualisieren(self, payload: list):
        for i in range(0, len(payload), 100):
            self.api("POST", "/api/_action/sync", {"write-products": {"entity": "product", "action": "upsert", "payload": payload[i:i + 100]}})

    def kategorien(self) -> list:
        return self._alle("category", ["id", "name", "parentId"])

    def navigationswurzel(self) -> str:
        """Hauptnavigation des Verkaufskanals: Unter ihr legt wwok fehlende Kategorien an."""
        kanal = self.verkaufskanal()
        daten = self.api("POST", "/api/search/sales-channel", {
            "ids": [kanal], "includes": {"sales_channel": ["id", "navigationCategoryId"]}}).get("data", [])
        wurzel = daten[0].get("navigationCategoryId") if daten else ""
        if not wurzel:
            raise ShopFehler("Der Verkaufskanal hat keine Hauptnavigation (Kategorie-Einstiegspunkt) – bitte in Shopware einstellen.")
        return wurzel

    def kategorien_schreiben(self, payload: list):
        """Kategorien anlegen oder ändern (upsert); nur die übergebenen Felder werden geschrieben."""
        self.api("POST", "/api/_action/sync", {"write-kategorien": {"entity": "category", "action": "upsert", "payload": payload}})

    def kategorien_aktiv_setzen(self, aenderungen: list):
        """aenderungen: Liste (Kategorie-ID im Shop, aktiv). Ändert nur das Feld active, sonst nichts."""
        for i in range(0, len(aenderungen), 100):
            self.api("POST", "/api/_action/sync", {"write-kategorien": {"entity": "category", "action": "upsert", "payload": [
                {"id": kid, "active": bool(aktiv)} for kid, aktiv in aenderungen[i:i + 100]]}})

    def hersteller(self) -> list:
        return self._alle("product_manufacturer", ["id", "name"])

    def steuern(self) -> list:
        return self._alle("tax", ["id", "taxRate"])

    def hersteller_anlegen(self, hersteller_id: str, name: str):
        self.api("POST", "/api/_action/sync", {"write-hersteller": {"entity": "product_manufacturer", "action": "upsert", "payload": [{"id": hersteller_id, "name": name}]}})

    def verkaufskanal(self) -> str:
        """Verkaufskanal für neue Artikel: der eingetragene oder der einzige Storefront-Kanal des Shops."""
        if self.shop.verkaufskanal_id:
            return self.shop.verkaufskanal_id
        kanaele = self.api("POST", "/api/search/sales-channel", {
            "filter": [{"type": "equals", "field": "typeId", "value": STOREFRONT_TYP}, {"type": "equals", "field": "active", "value": True}],
            "includes": {"sales_channel": ["id"]}}).get("data", [])
        if len(kanaele) != 1:
            raise ShopFehler(f"Der Shop hat {len(kanaele)} aktive Verkaufskanäle: bitte in der Verwaltung die Verkaufskanal-ID eintragen.")
        return kanaele[0]["id"]

    def bild_hochladen(self, medien_id: str, dateiname: str, endung: str, inhalt: bytes):
        ordner = self.medienordner()
        self.api("POST", "/api/_action/sync", {"write-medien": {"entity": "media", "action": "upsert", "payload": [
            {"id": medien_id, **({"mediaFolderId": ordner} if ordner else {})}]}})
        self.api("POST", f"/api/_action/media/{medien_id}/upload?extension={endung}&fileName={dateiname}", inhalt,
                 {"Content-Type": BILDTYPEN[endung]})

    def medienordner(self):
        """Ordner für Produktbilder; ohne Ordner erzeugt Shopware keine Vorschaubilder."""
        if self._medienordner is False:
            daten = self.api("POST", "/api/search/media-default-folder", {
                "filter": [{"type": "equals", "field": "entity", "value": "product"}], "associations": {"folder": {}}}).get("data", [])
            self._medienordner = (daten[0].get("folder") or {}).get("id") if daten else None
        return self._medienordner

    def produktbild_ids(self, produkt_id: str) -> list:
        return [m["id"] for m in self.api("POST", "/api/search/product-media", {
            "limit": 500, "filter": [{"type": "equals", "field": "productId", "value": produkt_id}], "includes": {"product_media": ["id"]}}).get("data", [])]

    def produkt_vorhanden(self, produkt_id: str) -> bool:
        return bool(self.api("POST", "/api/search/product", {
            "limit": 1, "filter": [{"type": "equals", "field": "id", "value": produkt_id}], "includes": {"product": ["id"]}}).get("data"))

    def _zuordnung_loesen(self, pfad: str):
        try:
            self.api("DELETE", pfad)
        except ShopFehler as fehler:
            if fehler.status != 404:  # schon im Shop entfernt: erledigt
                raise

    def regel_vorhanden(self, regel_id: str) -> bool:
        return bool(self.api("POST", "/api/search/rule", {
            "limit": 1, "filter": [{"type": "equals", "field": "id", "value": regel_id}], "includes": {"rule": ["id"]}}).get("data"))

    def produktpreise(self, produkt_ids: list) -> list:
        """Alle Preise mit Regel dieser Produkte."""
        ergebnis = []
        for i in range(0, len(produkt_ids), 100):
            seite = 1
            while True:
                daten = self.api("POST", "/api/search/product-price", {
                    "page": seite, "limit": 500, "filter": [{"type": "equalsAny", "field": "productId", "value": "|".join(produkt_ids[i:i + 100])}],
                    "includes": {"product_price": ["id", "productId"]}}).get("data", [])
                ergebnis += daten
                if len(daten) < 500:
                    break
                seite += 1
        return ergebnis

    def kundenpreise_loeschen(self, preis_ids: list):
        for i in range(0, len(preis_ids), 100):
            self.api("POST", "/api/_action/sync", {"delete-preise": {
                "entity": "product_price", "action": "delete", "payload": [{"id": p} for p in preis_ids[i:i + 100]]}})

    def produktbild_loeschen(self, produkt_id: str, bild_id: str):
        self._zuordnung_loesen(f"/api/product/{produkt_id}/media/{bild_id}")

    def kategorie_entfernen(self, produkt_id: str, kategorie_id: str):
        self._zuordnung_loesen(f"/api/product/{produkt_id}/categories/{kategorie_id}")
