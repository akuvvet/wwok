"""Schlanker Client für die Shopware-6-Admin-API (OAuth client_credentials, JSON)."""
import json
import os
import time
import urllib.error
import urllib.request

STANDARD_WAEHRUNG_EUR = "b7d2554b0ce847cd82f3ac9bd1c0dfca"  # feste ID der Standardwährung in Shopware 6


class ShopFehler(Exception):
    pass


class ShopwareClient:
    def __init__(self, shop):
        self.shop = shop
        self.basis = shop.url.rstrip("/")
        self._token = None
        self._ablauf = 0

    # -- HTTP (in Tests ersetzt) ---------------------------------------------------------------------
    def _anfrage(self, methode: str, pfad: str, daten=None, kopf=None):
        header = {"Accept": "application/json", "Content-Type": "application/json", **(kopf or {})}
        anfrage = urllib.request.Request(self.basis + pfad, method=methode, headers=header,
                                         data=json.dumps(daten).encode() if daten is not None else None)
        try:
            with urllib.request.urlopen(anfrage, timeout=60) as antwort:
                inhalt = antwort.read()
                return json.loads(inhalt) if inhalt else {}
        except urllib.error.HTTPError as fehler:
            raise ShopFehler(f"Shop antwortet mit {fehler.code}: {fehler.read()[:300].decode('utf-8', 'replace')}")
        except (urllib.error.URLError, OSError, ValueError) as fehler:
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

    def api(self, methode: str, pfad: str, daten=None):
        return self._anfrage(methode, pfad, daten, {"Authorization": f"Bearer {self._zugang()}"})

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
