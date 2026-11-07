import hashlib
import json
import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone as tz
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.utils import timezone
from django.utils.html import linebreaks

from apps.finanzen.models import Zahlart
from apps.finanzen.services import zahlung_buchen
from apps.lager.models import bestand, gruppen_verfuegbar, reserviert
from apps.stammdaten.models import Anschriftart, Ansprechpartner, Artikel, Kategorie, Kunde, Kundenanschrift, Stammnummer, Zahlungsbedingung
from apps.stammdaten.preise import stufenpreis
from apps.verkauf.models import Beleg, Belegart, Position

from .models import ShopArtikel, ShopBestellung, ShopKategorie, SyncProtokoll
from .shopware import BILDTYPEN, STANDARD_WAEHRUNG_EUR, ShopFehler, ShopwareClient

CENT = Decimal("0.01")
MAX_DEAKTIVIEREN = 50  # mehr auf einmal nur nach ausdrücklichem Auslösen
ZEITLIMIT_TAKT = 480  # Sekunden; der Zeitplan startet alle 10 Minuten


def g(daten, *pfad, standard=""):
    """Verschachtelte Werte defensiv lesen (Shopware liefert fehlende Teile als None)."""
    for schluessel in pfad:
        if isinstance(daten, list):
            daten = daten[schluessel] if isinstance(schluessel, int) and len(daten) > schluessel else None
        elif isinstance(daten, dict):
            daten = daten.get(schluessel)
        else:
            return standard
        if daten is None:
            return standard
    return daten


def dez(wert) -> Decimal:
    return Decimal(str(wert or 0))


def netto(brutto, steuersatz) -> Decimal:
    return (dez(brutto) / (1 + dez(steuersatz) / 100)).quantize(CENT, rounding=ROUND_HALF_UP)


def shop_netto(wert, steuersatz, steuerstatus: str) -> Decimal:
    """Netto eines Shop-Betrags: Bestellungen von Netto-Kunden tragen ihre Preise bereits ohne Steuer."""
    if steuerstatus in ("net", "tax-free"):
        return dez(wert).quantize(CENT, rounding=ROUND_HALF_UP)
    return netto(wert, steuersatz)


# ---- Bestellimport -----------------------------------------------------------------------------------

def adresse_text(a: dict) -> str:
    name = " ".join(t for t in (g(a, "firstName"), g(a, "lastName")) if t)
    firma = g(a, "company")
    zeilen = [firma or name, name if firma else "", g(a, "street"), f"{g(a, 'zipcode')} {g(a, 'city')}".strip()]
    land = g(a, "country", "iso")
    if land and land != "DE":
        zeilen.append(land)
    return "\n".join(z for z in zeilen if z)


def kunde_finden_oder_anlegen(shop, bestellung: dict, rechnung: dict, versand: dict):
    email = (g(bestellung, "orderCustomer", "email") or "").strip()
    kunde = None
    if email:
        kunde = Kunde.objects.filter(email__iexact=email).first()
        if kunde is None:
            ap = Ansprechpartner.objects.filter(email__iexact=email, kunde__isnull=False).select_related("kunde").first()
            kunde = ap.kunde if ap else None
    if kunde:
        return kunde, False
    firma = g(rechnung, "company") or g(bestellung, "orderCustomer", "company")
    vorname = g(bestellung, "orderCustomer", "firstName") or g(rechnung, "firstName")
    nachname = g(bestellung, "orderCustomer", "lastName") or g(rechnung, "lastName")
    person = f"{vorname} {nachname}".strip()
    kunde = Kunde.objects.create(
        nummer=Stammnummer.naechste(Stammnummer.Art.KUNDE), name1=(firma or person)[:150], name2=(person if firma else "")[:150],
        strasse=g(rechnung, "street")[:200], plz=g(rechnung, "zipcode")[:10], ort=g(rechnung, "city")[:100],
        land=(g(rechnung, "country", "iso") or "DE")[:2], email=email[:254], telefon=(g(rechnung, "phoneNumber") or "")[:50],
        kundenart="gewerblich" if firma else "endkunde", gesellschaftsform="" if firma else "privat",
        zahlungsbedingung=shop.standard_zahlungsbedingung)
    Ansprechpartner.objects.create(kunde=kunde, nachname=(nachname or person or "Kunde")[:100], vorname=vorname[:100],
                                   email=email[:254], hauptkontakt=True)
    if versand and adresse_text(versand) != adresse_text(rechnung):
        Kundenanschrift.objects.create(
            kunde=kunde, art=Anschriftart.VERSAND, standard=True, name1=(g(versand, "company") or f"{g(versand, 'firstName')} {g(versand, 'lastName')}").strip()[:150],
            strasse=g(versand, "street")[:200], plz=g(versand, "zipcode")[:10], ort=g(versand, "city")[:100],
            land=(g(versand, "country", "iso") or "DE")[:2])
    return kunde, True


def zahlart_von(name: str) -> str:
    n = name.lower()
    if "paypal" in n:
        return Zahlart.PAYPAL
    if "kredit" in n or "card" in n or "visa" in n or "master" in n:
        return Zahlart.KARTE
    if "lastschrift" in n or "sepa" in n:
        return Zahlart.LASTSCHRIFT
    return Zahlart.UEBERWEISUNG


@transaction.atomic
def bestellung_importieren(shop, b: dict):
    """Eine Shopware-Bestellung als Beleg Bestellung anlegen; Rückgabe: ShopBestellung."""
    extern_id, nummer = b["id"], str(b.get("orderNumber", ""))
    if ShopBestellung.objects.filter(shop=shop, shop_id_extern=extern_id).exists():
        return None
    adressen = {a["id"]: a for a in (b.get("addresses") or [])}
    rechnung = adressen.get(b.get("billingAddressId")) or next(iter(adressen.values()), {})
    lieferung = (b.get("deliveries") or [{}])[0] or {}
    versand = g(lieferung, "shippingOrderAddress", standard={}) or adressen.get(g(lieferung, "shippingOrderAddressId", standard=None)) or rechnung
    kunde, neu = kunde_finden_oder_anlegen(shop, b, rechnung, versand)

    zahlart_name = g((b.get("transactions") or [{}])[0], "paymentMethod", "name")
    zb_nummer = shop.zahlungszuordnung.get(zahlart_name) if zahlart_name else None
    zb = Zahlungsbedingung.objects.filter(nummer=zb_nummer).first() if zb_nummer else None
    beleg = Beleg.objects.create(
        mandant=shop.mandant, art=Belegart.AUFTRAG, kunde=kunde,
        ansprechpartner=kunde.ansprechpartner.filter(hauptkontakt=True).first(),
        belegdatum=datetime.fromisoformat((b.get("orderDateTime") or b.get("createdAt")).replace("Z", "+00:00")).date(),
        zahlungsbedingung=zb or shop.standard_zahlungsbedingung or kunde.zahlungsbedingung,
        rechnungsanschrift=adresse_text(rechnung), versandanschrift=adresse_text(versand),
        versandart=g(lieferung, "shippingMethod", "name")[:100], referenz=nummer[:100],
        bemerkung=f"Shop: {shop.name}, Bestellung {nummer}" + (f"\nKundenkommentar: {b['customerComment']}" if b.get("customerComment") else ""))
    hinweise = []
    if neu:
        hinweise.append(f"Neukunde {kunde.nummer} angelegt")
    steuerstatus = b.get("taxStatus") or g(b, "price", "taxStatus") or "gross"
    ohne_steuer = steuerstatus == "tax-free"
    for z in b.get("lineItems") or []:
        if z.get("type") not in (None, "product"):
            continue  # Gutscheine, Rabatte usw. werden nicht übernommen
        steuer = Decimal("0") if ohne_steuer else dez(g(z, "price", "calculatedTaxes", 0, "taxRate", standard=19))
        produktnummer = g(z, "payload", "productNumber")
        artikel = Artikel.objects.filter(nummer=produktnummer).first() if produktnummer else None
        if artikel is None:
            hinweise.append(f"Artikel {produktnummer or z.get('label')} nicht im ERP")
        Position.objects.create(
            beleg=beleg, typ=artikel.typ if artikel else "L", artikel=artikel, bezeichnung=str(z.get("label") or produktnummer)[:255],
            menge=dez(z.get("quantity") or 1), einzelpreis=shop_netto(z.get("unitPrice"), steuer, steuerstatus), steuer_prozent=steuer,
            zusatztext="" if artikel else f"Artikelnummer im Shop: {produktnummer}")
    versandkosten = g(lieferung, "shippingCosts", "totalPrice", standard=g(b, "shippingCosts", "totalPrice", standard=0))
    if dez(versandkosten):
        beleg.versandkosten = shop_netto(versandkosten, g(lieferung, "shippingCosts", "calculatedTaxes", 0, "taxRate", standard=19), steuerstatus)
        beleg.save()
    if not beleg.positionen.exists():
        raise ValueError(f"Bestellung {nummer} enthält keine Produktpositionen.")
    beleg.berechne()
    if abs(beleg.summe_brutto - dez(b.get("amountTotal"))) > Decimal("0.05"):
        hinweise.append(f"Summe ERP {beleg.summe_brutto} € weicht vom Shop ({dez(b.get('amountTotal'))} €) ab")
    status = g((b.get("transactions") or [{}])[0], "stateMachineState", "technicalName")
    if status == "paid":  # bereits bezahlt (z. B. PayPal) → Anzahlung auf die Bestellung
        zahlung_buchen(beleg, dez(b.get("amountTotal")), beleg.belegdatum, zahlart_von(zahlart_name or ""),
                       f"Shop {nummer}: {zahlart_name}"[:200])
    return ShopBestellung.objects.create(shop=shop, shop_id_extern=extern_id, bestellnummer=nummer, beleg=beleg,
                                         hinweis="; ".join(hinweise)[:300])


def bestellungen_abrufen(shop, client=None) -> dict:
    """Holt neue Bestellungen seit dem letzten Import (mit einem Tag Überlappung, Dubletten werden erkannt)."""
    client = client or ShopwareClient(shop)
    seit = (shop.letzter_bestellimport - timedelta(days=1)) if shop.letzter_bestellimport else None
    start = timezone.now()
    ergebnis = {"neu": 0, "bekannt": 0, "fehler": []}
    seite = 1
    while True:
        antwort = client.bestellungen(seit, seite)
        daten = antwort.get("data") or []
        for b in daten:
            try:
                if bestellung_importieren(shop, b) is None:
                    ergebnis["bekannt"] += 1
                else:
                    ergebnis["neu"] += 1
            except Exception as fehler:  # eine fehlerhafte Bestellung darf den ganzen Abruf nicht abbrechen
                ergebnis["fehler"].append(f"{b.get('orderNumber', '?')}: {fehler}")
        if len(daten) < 50:
            break
        seite += 1
    if not ergebnis["fehler"]:  # bei Fehlern bleibt der Zeitpunkt stehen, damit die Bestellung erneut versucht wird
        shop.letzter_bestellimport = start
        shop.save(update_fields=["letzter_bestellimport"])
    SyncProtokoll.objects.create(
        shop=shop, art="bestellungen", erfolgreich=not ergebnis["fehler"],
        meldung=f"{ergebnis['neu']} neu, {ergebnis['bekannt']} bereits bekannt" + (f", Fehler: {'; '.join(ergebnis['fehler'])}"[:350] if ergebnis["fehler"] else ""))
    return ergebnis


# ---- Bestand und Preise ------------------------------------------------------------------------------

def meldebestand(shop, artikel):
    """Nur der verfügbare Bestand (Bestand − Reservierungen − Sicherheitsbestand) wird gemeldet."""
    if artikel.typ == "G":
        frei = gruppen_verfuegbar(shop.mandant, artikel)
        return None if frei is None else max(int(frei) - shop.bestand_puffer, 0)
    if artikel.typ == "A":
        frei = bestand(shop.mandant, artikel) - reserviert(shop.mandant, artikel)
        return max(int(frei) - shop.bestand_puffer, 0)
    return None


def verkaufspreis(shop, artikel, stufe=None):
    """Netto und brutto der Preisstufe (ohne Angabe die des Shops); ohne Preis (None, None)."""
    netto_preis = stufenpreis(artikel, stufe or shop.preisstufe)
    if netto_preis is None:
        return None, None
    return netto_preis, (netto_preis * (1 + artikel.steuersatz.prozent / 100)).quantize(CENT, rounding=ROUND_HALF_UP)


def preisliste(netto_preis, brutto) -> list:
    return [{"currencyId": STANDARD_WAEHRUNG_EUR, "gross": float(brutto), "net": float(netto_preis), "linked": True}]


def kundenpreis(shop, artikel) -> tuple:
    """Eigener Preis für angemeldete Kunden als Shopware-Staffelpreis mit Regel; (Eintrag, Brutto) oder (None, None)."""
    if not shop.preisstufe_kunden or not shop.regel_kunden_id:
        return None, None
    preis_netto, preis_brutto = verkaufspreis(shop, artikel, shop.preisstufe_kunden)
    if not preis_brutto:
        return None, None
    return {"id": feste_id(shop, "kundenpreis", artikel.pk), "ruleId": shop.regel_kunden_id, "quantityStart": 1, "quantityEnd": None,
            "price": preisliste(preis_netto, preis_brutto)}, preis_brutto


def regel_pruefen(shop, client) -> bool:
    """Eine falsche Regel-ID würde jeden Schreibvorgang samt Bestand scheitern lassen, deshalb vorher prüfen."""
    if not shop.preisstufe_kunden or not shop.regel_kunden_id:
        return True
    if client.regel_vorhanden(shop.regel_kunden_id):
        return True
    SyncProtokoll.objects.create(shop=shop, art="abgleich", erfolgreich=False, meldung=(
        f"Die Regel {shop.regel_kunden_id} gibt es im Shop nicht. Kundenpreise wurden nicht gesendet, Bestand und Standardpreis schon."))
    return False


def nicht_mehr_online(shop, client, alle=False) -> list:
    """Zuvor gemeldete Artikel, die offline sind, im Shop deaktivieren. Gelöscht wird nichts; Artikel, die wwok nie
    gemeldet hat, bleiben unberührt, auch wenn sie im Shop stehen."""
    sichtbar = Artikel.objects.shop_sichtbar().values("pk")
    zuordnungen = list(ShopArtikel.objects.filter(shop=shop, im_shop_aktiv=True).exclude(artikel__in=sichtbar).select_related("artikel"))
    if not zuordnungen:
        return []
    offline = Kategorie.offline_ids()
    # Bewusst über eine Offline-Kategorie abgeschaltet: kein Importfehler, die Schutzgrenze gilt dafür nicht
    wegen_kategorie = [z for z in zuordnungen if z.artikel.online and not z.artikel.nicht_aktiv and z.artikel.kategorie_id in offline]
    sonstige = [z for z in zuordnungen if z not in wegen_kategorie]
    if len(sonstige) > MAX_DEAKTIVIEREN and not alle:  # z. B. Importfehler: nicht den ganzen Shop abschalten
        SyncProtokoll.objects.create(shop=shop, art="abgleich", erfolgreich=False, meldung=(
            f"{len(sonstige)} Artikel wären im Shop zu deaktivieren, das ist ungewöhnlich viel. Nichts deaktiviert. "
            "Stimmt es, „Bestand & Preise senden“ mit „alle Artikel“ auslösen."))
        zuordnungen = wegen_kategorie
        if not zuordnungen:
            return []
    vorhanden = [z.produkt_id for z in zuordnungen if client.produkt_vorhanden(z.produkt_id)]  # nie über die Nummer: sie kann inzwischen einem fremden Produkt gehören
    if vorhanden:
        client.produkte_aktualisieren([{"id": produkt_id, "active": False} for produkt_id in vorhanden])
    for z in zuordnungen:  # im Shop gelöschte Produkte sind ebenso erledigt
        z.im_shop_aktiv = False
        z.save(update_fields=["im_shop_aktiv"])
    return vorhanden


def kategorien_abgleichen(shop, client) -> int:
    """Shop-Kategorien an den Online-Status der Kategorien in wwok anpassen. Nur Kategorien, die wwok zugeordnet hat;
    fremde Shop-Kategorien bleiben unberührt. Gibt die Zahl der umgeschalteten Kategorien zurück."""
    offline = Kategorie.offline_ids()
    bekannt = set(ShopKategorie.objects.filter(shop=shop).values_list("kategorie_id", flat=True))
    # nur Hauptkategorien über den Namen; Unterkategorien ordnet die KategorieKarte beim Senden über den Pfad zu
    neu = Kategorie.objects.filter(artikel__shop_zuordnungen__shop=shop, eltern__isnull=True).exclude(pk__in=bekannt).distinct()
    if neu:  # erst über den Namen (nur eindeutig), danach immer über die ID
        karte = _nach_name(client.kategorien())
        for k in neu:
            treffer = karte.get(k.name.strip().casefold(), [])
            if len(treffer) == 1:
                ShopKategorie.objects.get_or_create(shop=shop, kategorie=k, defaults={"kategorie_id_extern": treffer[0]})
    zu_aendern = [z for z in ShopKategorie.objects.filter(shop=shop) if z.im_shop_aktiv == (z.kategorie_id in offline)]
    if not zu_aendern:
        return 0
    client.kategorien_aktiv_setzen([(z.kategorie_id_extern, z.kategorie_id not in offline) for z in zu_aendern])
    for z in zu_aendern:
        z.im_shop_aktiv = z.kategorie_id not in offline
        z.save(update_fields=["im_shop_aktiv"])
    return len(zu_aendern)


def abgleichen(shop, client=None, alle=False) -> dict:
    """Bestände und Preise der Online-Artikel an den Shop melden; ohne 'alle' nur Änderungen seit dem letzten Abgleich."""
    client = client or ShopwareClient(shop)
    try:
        deaktiviert = nicht_mehr_online(shop, client, alle)
    except ShopFehler as fehler:  # Bestand und Preise müssen trotzdem raus; beim nächsten Abgleich wird erneut deaktiviert
        deaktiviert = []
        SyncProtokoll.objects.create(shop=shop, art="abgleich", erfolgreich=False, meldung=f"Deaktivieren fehlgeschlagen: {fehler}"[:500])
    try:
        kategorien_abgleichen(shop, client)
    except ShopFehler as fehler:  # beim nächsten Abgleich erneut
        SyncProtokoll.objects.create(shop=shop, art="abgleich", erfolgreich=False, meldung=f"Kategorien nicht umgeschaltet: {fehler}"[:500])
    regel_ok = regel_pruefen(shop, client)
    artikel = list(Artikel.objects.shop_sichtbar().filter(typ__in=["A", "G"]).select_related("steuersatz"))
    ids = client.produkt_ids([a.nummer for a in artikel])
    payload, vorgemerkt, nicht_im_shop = [], [], 0
    for a in artikel:
        produkt_id = ids.get(a.nummer)
        if not produkt_id:
            nicht_im_shop += 1
            continue
        zuordnung, _ = ShopArtikel.objects.get_or_create(shop=shop, artikel=a, defaults={"produkt_id": produkt_id})
        bestand_neu = meldebestand(shop, a)
        preis_netto, preis_brutto = verkaufspreis(shop, a)
        wieder_online = not zuordnung.im_shop_aktiv
        preis_kunde, brutto_kunde = kundenpreis(shop, a) if regel_ok else (None, zuordnung.gemeldeter_kundenpreis)
        geaendert = alle or wieder_online or zuordnung.gemeldeter_bestand != bestand_neu or zuordnung.gemeldeter_preis != preis_brutto \
            or zuordnung.gemeldeter_kundenpreis != brutto_kunde or zuordnung.produkt_id != produkt_id \
            or (preis_kunde is not None and zuordnung.gemeldete_regel != shop.regel_kunden_id)
        if not geaendert:
            continue
        eintrag = {"id": produkt_id}
        if wieder_online:
            eintrag["active"] = True
        if bestand_neu is not None:
            eintrag["stock"] = bestand_neu
        if preis_brutto is not None:
            eintrag["price"] = preisliste(preis_netto, preis_brutto)
        if preis_kunde:
            eintrag["prices"] = [preis_kunde]
        payload.append(eintrag)
        vorgemerkt.append((zuordnung, produkt_id, bestand_neu, preis_brutto, brutto_kunde))
    # Fremde Preise im Shop bleiben unberührt: Neben ihnen entschiede die Menge zufällig, welcher Preis gilt
    eigene = {feste_id(shop, "kundenpreis", a.pk) for a in artikel}
    mit_preis = [e["id"] for e in payload if "prices" in e]
    belegt = {z["productId"] for z in client.produktpreise(mit_preis) if z["id"] not in eigene} if mit_preis else set()
    for e in payload:
        if e["id"] in belegt:
            del e["prices"]
    if payload:
        client.produkte_aktualisieren(payload)
    jetzt = timezone.now()
    entfallen = []
    for zuordnung, produkt_id, bestand_neu, preis_brutto, brutto_kunde in vorgemerkt:
        if brutto_kunde is None and zuordnung.gemeldeter_kundenpreis is not None:  # VK der Stufe entfallen: alten Kundenpreis nicht stehen lassen
            entfallen.append(zuordnung)
        elif produkt_id not in belegt:
            zuordnung.gemeldeter_kundenpreis = brutto_kunde
            zuordnung.gemeldete_regel = shop.regel_kunden_id if brutto_kunde is not None else ""
        zuordnung.produkt_id, zuordnung.gemeldeter_bestand, zuordnung.gemeldeter_preis, zuordnung.gesendet_am = produkt_id, bestand_neu, preis_brutto, jetzt
        zuordnung.im_shop_aktiv = True
        zuordnung.save(update_fields=["produkt_id", "gemeldeter_bestand", "gemeldeter_preis", "gemeldeter_kundenpreis", "gemeldete_regel", "gesendet_am", "im_shop_aktiv"])
    if entfallen:
        try:
            client.kundenpreise_loeschen([feste_id(shop, "kundenpreis", z.artikel_id) for z in entfallen])
            ShopArtikel.objects.filter(pk__in=[z.pk for z in entfallen]).update(gemeldeter_kundenpreis=None, gemeldete_regel="")
        except ShopFehler as fehler:  # gemeldeter Preis bleibt stehen, der nächste Abgleich versucht es erneut
            SyncProtokoll.objects.create(shop=shop, art="abgleich", erfolgreich=False, meldung=f"Kundenpreise nicht entfernt: {fehler}"[:500])
    shop.letzter_abgleich = jetzt
    shop.save(update_fields=["letzter_abgleich"])
    ergebnis = {"gesendet": len(payload), "nicht_im_shop": nicht_im_shop, "geprueft": len(artikel), "deaktiviert": len(deaktiviert), "kundenpreis_belegt": len(belegt)}
    SyncProtokoll.objects.create(shop=shop, art="abgleich", meldung=f"{ergebnis['gesendet']} Artikel gesendet, {nicht_im_shop} nicht im Shop gefunden, {len(artikel)} geprüft, {len(deaktiviert)} deaktiviert"
                                 + (f", {len(belegt)} ohne Kundenpreis, weil im Shop schon ein Preis mit Regel steht" if belegt else ""))
    return ergebnis


# ---- Artikeldaten (Texte, Bilder, neue Artikel) ------------------------------------------------------

def feste_id(shop, art: str, schluessel) -> str:
    """Gleichbleibende Shopware-ID, damit ein wiederholter oder abgebrochener Lauf nichts doppelt anlegt."""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"wwok/{shop.pk}/{art}/{schluessel}").hex


def beschreibung(artikel) -> str:
    """Shopware erwartet HTML; reiner Text bekommt Absätze."""
    text = artikel.bezeichnung_erweitert or artikel.kurzbeschreibung
    return text if re.search(r"<[a-zA-Z]", text) else linebreaks(text, autoescape=True) if text else ""


def produktdaten(artikel) -> dict:
    """Felder fürs Produkt. Was in wwok leer ist, fehlt hier und bleibt im Shop, wie es ist."""
    werte = {
        "name": artikel.bezeichnung, "description": beschreibung(artikel), "ean": artikel.ean,
        "manufacturerNumber": artikel.herstellernummer, "metaTitle": artikel.meta_titel[:255],
        "metaDescription": artikel.meta_description[:255], "keywords": artikel.meta_keywords[:255],
        "customSearchKeywords": [w.strip() for w in re.split(r"[,;\n]", artikel.keywords) if w.strip()],
    }
    if artikel.gewicht_kg is not None:
        werte["weight"] = float(artikel.gewicht_kg)
    for feld, wert in (("length", artikel.laenge_cm), ("width", artikel.breite_cm), ("height", artikel.hoehe_cm)):
        if wert is not None:
            werte[feld] = float(wert * 10)  # Shopware rechnet in Millimetern
    return {feld: wert for feld, wert in werte.items() if wert not in ("", [])}


def _nach_name(eintraege) -> dict:
    """Name klein → IDs; ein doppelter Name bleibt erkennbar, damit nichts falsch zugeordnet wird."""
    karte = {}
    for e in eintraege:
        if (e.get("name") or "").strip():
            karte.setdefault(e["name"].strip().casefold(), []).append(e["id"])
    return karte


class KategorieKarte:
    """Shop-Kategorien eines Laufs (Issue 59): Zuordnung über die gespeicherte Zuordnung, sonst über den Pfad ab der
    Hauptnavigation des Verkaufskanals; fehlende Kategorien werden samt fehlender Eltern angelegt. Nur online
    Kategorien, nur beim Senden eines Online-Artikels. Umbenennen/Verschieben in wwok wird nur für Kategorien
    nachgezogen, die wwok selbst angelegt hat; fremde Shop-Kategorien bleiben unberührt."""

    def __init__(self, shop, client, ergebnis):
        self.shop, self.client, self.ergebnis = shop, client, ergebnis
        self.eintraege = {e["id"]: {"name": (e.get("name") or "").strip(), "eltern": e.get("parentId") or ""}
                          for e in client.kategorien()}
        self.zuordnung = {z.kategorie_id: z for z in ShopKategorie.objects.filter(shop=shop)}
        self.offline = Kategorie.offline_ids()
        self._wurzel, self.fertig = None, {}

    def wurzel(self) -> str:
        if self._wurzel is None:
            self._wurzel = self.client.navigationswurzel()
        return self._wurzel

    def _kinder(self, eltern, name) -> list:
        n = name.casefold()
        return [i for i, e in self.eintraege.items() if e["eltern"] == eltern and e["name"].casefold() == n]

    def _ueberall(self, name) -> list:  # ohne Shop-Kategorien, die schon einer anderen wwok-Kategorie gehören
        n, vergeben = name.casefold(), {z.kategorie_id_extern for z in self.zuordnung.values()}
        return [i for i, e in self.eintraege.items() if e["name"].casefold() == n and i not in vergeben]

    def _merken(self, k, ext, angelegt=False):
        angelegt = angelegt or ext == feste_id(self.shop, "kategorie", k.pk)  # auch nach zurückgerolltem Artikel erkennbar
        z, _ = ShopKategorie.objects.update_or_create(shop=self.shop, kategorie=k, defaults={"kategorie_id_extern": ext, "angelegt": angelegt})
        self.zuordnung[k.pk] = z

    def id_fuer(self, k, tiefe=0) -> str:
        """Shop-ID der Kategorie; "" = nicht zuzuordnen (mehrdeutig, offline oder Fehler)."""
        if k.pk in self.fertig:
            return self.fertig[k.pk]
        self.fertig[k.pk] = ext = self._ermitteln(k, tiefe)
        return ext

    def _ermitteln(self, k, tiefe) -> str:
        if k.pk in self.offline or tiefe > 10:
            return ""
        name = k.name.strip()
        eltern = k.eltern
        z = self.zuordnung.get(k.pk)
        if z and z.kategorie_id_extern in self.eintraege:  # bekannt: über die ID, nie mehr über den Namen
            if z.angelegt:
                self._nachziehen(k, z.kategorie_id_extern, eltern, tiefe)
            return z.kategorie_id_extern
        eltern_ext = self.id_fuer(eltern, tiefe + 1) if eltern else self.wurzel()
        if eltern and not eltern_ext:
            return ""
        treffer = self._kinder(eltern_ext, name)
        if not treffer and not eltern:  # Hauptkategorie: wie bisher auch anderswo im Shop, wenn der Name dort eindeutig ist;
            anderswo = self._ueberall(name)  # mehrere gleichnamige anderswo (z. B. „Kabel“ unter Audio und Netzwerk):
            treffer = anderswo if len(anderswo) == 1 else []  # eigene Hauptkategorie anlegen statt raten
        if len(treffer) == 1:
            self._merken(k, treffer[0])
            return treffer[0]
        if treffer:  # gleichnamig doppelt auf derselben Ebene im Shop: nichts anlegen, nichts raten
            self.ergebnis["kategorie_fehlt"].add(str(k))
            return ""
        ext = feste_id(self.shop, "kategorie", k.pk)  # gleichbleibend: ein wiederholter Lauf legt nichts doppelt an
        try:
            self.client.kategorien_schreiben([{"id": ext, "name": name, "parentId": eltern_ext, "active": True, "visible": True,
                                               "type": "page", "displayNestedProducts": True}])
        except ShopFehler as fehler:
            self.ergebnis["fehler"].append(f"Kategorie {k}: {fehler}")
            return ""
        self.eintraege[ext] = {"name": name, "eltern": eltern_ext}
        self._merken(k, ext, angelegt=True)
        self.ergebnis["kategorie_neu"].append(str(k))
        return ext

    def _nachziehen(self, k, ext, eltern, tiefe):
        """Von wwok angelegte Kategorie: Name und Elternkategorie wie in wwok."""
        eltern_ext = self.id_fuer(eltern, tiefe + 1) if eltern else self.wurzel()
        if not eltern_ext:
            return
        jetzt, soll = self.eintraege[ext], {"name": k.name.strip(), "eltern": eltern_ext}
        if jetzt == soll:
            return
        try:
            self.client.kategorien_schreiben([{"id": ext, "name": soll["name"], "parentId": soll["eltern"]}])
        except ShopFehler as fehler:
            self.ergebnis["fehler"].append(f"Kategorie {k}: {fehler}")
            return
        self.eintraege[ext] = soll
        self.ergebnis["kategorie_geaendert"].append(str(k))


def _hersteller_id(shop, client, karte, name) -> str:
    schluessel = name.strip().casefold()
    if schluessel not in karte:
        hersteller_id = feste_id(shop, "hersteller", schluessel)
        client.hersteller_anlegen(hersteller_id, name)
        karte[schluessel] = [hersteller_id]
    return karte[schluessel][0]


def _bilder_lesen(artikel):
    """Dateien der Bilder. Fehlt eine, gibt es None: Dann bleibt im Shop alles, wie es ist, statt Bilder zu verlieren."""
    dateien, hinweise = [], []
    for b in artikel.bilder.all():
        endung = os.path.splitext(b.bild.name)[1].lstrip(".").lower()
        if endung not in BILDTYPEN:
            hinweise.append(f"Bild {b.bild.name}: Dateityp nicht unterstützt")
            continue
        try:
            with b.bild.open("rb") as datei:
                dateien.append((b, endung, datei.read()))
        except OSError:
            return None, [f"Bild {b.bild.name}: Datei fehlt, Bilder bleiben im Shop unverändert"]
    return dateien, hinweise


def _artikel_senden(shop, client, a, z, produkt_id, lauf, alle) -> str:
    """Sendet einen Artikel und gibt zurück, was geschehen ist (Schlüssel im Ergebnis des Laufs)."""
    nummer_geaendert = False
    if not produkt_id and z and client.produkt_vorhanden(z.produkt_id):  # Artikelnummer wurde in wwok geändert
        produkt_id, nummer_geaendert = z.produkt_id, True
    anderes_produkt = bool(z) and z.produkt_id != produkt_id  # im Shop gelöscht oder neu angelegt: seine Bilder sind weg
    daten = produktdaten(a)
    if nummer_geaendert:
        daten["productNumber"] = a.nummer
    kategorie_id = lauf["kategorien"].id_fuer(a.kategorie) if a.kategorie_id else ""  # fehlt sie, wird sie angelegt
    herstellername = a.hersteller.name if a.hersteller_id else ""
    stand = hashlib.sha256(json.dumps([daten, kategorie_id, herstellername], sort_keys=True).encode()).hexdigest()
    bilder_stand = hashlib.sha256(json.dumps([(b.pk, b.bild.name) for b in a.bilder.all()]).encode()).hexdigest()
    teilstand = "~" + bilder_stand[:63]  # Zeitlimit mitten in den Bildern: Der nächste Lauf lädt nur den Rest
    bilder_neu = alle or not z or anderes_produkt or z.bilder_stand != bilder_stand
    if produkt_id and z and z.daten_stand == stand and not bilder_neu:
        return "unveraendert"
    preis_netto, preis_brutto = verkaufspreis(shop, a)
    if not produkt_id and not preis_brutto:  # ohne Preis oder mit 0 € nicht sichtbar anlegen
        return "ohne_preis"
    eintrag = {**daten, "id": produkt_id or feste_id(shop, "produkt", a.pk)}
    if herstellername:
        eintrag["manufacturerId"] = _hersteller_id(shop, client, lauf["hersteller"], herstellername)
    if kategorie_id:
        eintrag["categories"] = [{"id": kategorie_id}]
    bestand_neu = None
    if not produkt_id:  # Neuanlage: Shopware verlangt Steuer, Preis, Bestand und Nummer
        steuer_id = lauf["steuern"].get(a.steuersatz.prozent.normalize())
        if not steuer_id:
            raise ShopFehler(f"Steuersatz {a.steuersatz.prozent} % fehlt im Shop.")
        bestand_neu = meldebestand(shop, a) or 0
        eintrag.update({"productNumber": a.nummer, "taxId": steuer_id, "stock": bestand_neu, "active": True,
                        "isCloseout": True,  # nur verfügbarer Bestand ist bestellbar, Schutz vor Doppelverkauf
                        "price": preisliste(preis_netto, preis_brutto),
                        "visibilities": [{"id": feste_id(shop, "sichtbar", a.pk), "salesChannelId": client.verkaufskanal(), "visibility": 30}]})
        preis_kunde, brutto_kunde = kundenpreis(shop, a) if lauf["regel_ok"] else (None, None)
        if preis_kunde:
            eintrag["prices"] = [preis_kunde]
    bisher = list(z.bild_ids) if z and not anderes_produkt else []
    neue_bilder, hinweise = bisher, []
    bilder_erledigt, teilweise = True, False
    if bilder_neu and a.bilder.all() and produkt_id and not bisher:
        eigene = {feste_id(shop, "produktbild", b.pk) for b in a.bilder.all()}
        if set(client.produktbild_ids(produkt_id)) - eigene:
            bilder_neu = False  # im Shop gepflegte Galerie nicht verdoppeln
    if bilder_neu:
        dateien, hinweise = _bilder_lesen(a)
        bilder_erledigt = dateien is not None
        if bilder_erledigt:
            eintraege, hochgeladen = [], 0
            fortsetzen = bool(z) and z.bilder_stand == teilstand
            for b, endung, inhalt in dateien:
                if hochgeladen and time.monotonic() > lauf["ende"]:  # mindestens ein Bild je Lauf, sonst käme der Artikel nie durch
                    teilweise = True
                    break
                bild_id, medien_id = feste_id(shop, "produktbild", b.pk), feste_id(shop, "medien", b.pk)
                if not (fortsetzen and bild_id in bisher):
                    client.bild_hochladen(medien_id, f"wwok-{shop.pk}-{b.pk}", endung, inhalt)
                    hochgeladen += 1
                eintraege.append({"id": bild_id, "mediaId": medien_id, "position": b.position})
            if eintraege:
                eintrag["media"], eintrag["coverId"] = eintraege, eintraege[0]["id"]
            neue_bilder = [e["id"] for e in eintraege]
    client.produkte_aktualisieren([eintrag])
    if not teilweise:
        for bild_id in bisher:
            if bild_id not in neue_bilder:
                client.produktbild_loeschen(eintrag["id"], bild_id)
    else:
        neue_bilder = neue_bilder + [bild_id for bild_id in bisher if bild_id not in neue_bilder]
        lauf["ergebnis"]["offen"] += 1
    bisherige_kategorie = z.kategorie_id if z else ""
    if kategorie_id and bisherige_kategorie and bisherige_kategorie != kategorie_id:
        client.kategorie_entfernen(eintrag["id"], bisherige_kategorie)  # nur die, die wwok selbst zugeordnet hat
    werte = {"produkt_id": eintrag["id"], "daten_stand": stand, "bild_ids": neue_bilder, "gesendet_am": timezone.now(),
             "bilder_stand": teilstand if teilweise else bilder_stand if bilder_erledigt else (z.bilder_stand if z else ""),
             "kategorie_id": kategorie_id or bisherige_kategorie}
    if not produkt_id:
        werte.update(gemeldeter_bestand=bestand_neu, gemeldeter_preis=preis_brutto, gemeldeter_kundenpreis=brutto_kunde,
                     gemeldete_regel=shop.regel_kunden_id if brutto_kunde is not None else "")
    ShopArtikel.objects.update_or_create(shop=shop, artikel=a, defaults=werte)
    lauf["ergebnis"]["fehler"] += [f"{a.nummer}: {h}" for h in hinweise]
    return "aktualisiert" if produkt_id else "neu"


def artikeldaten_senden(shop, client=None, alle=False, nummer="", zeitlimit=ZEITLIMIT_TAKT) -> dict:
    """Texte, Maße, Hersteller, Kategorie und Bilder der Online-Artikel senden und fehlende Artikel sichtbar anlegen.
    Ohne 'alle' nur Geänderte. Nach dem Zeitlimit endet der Lauf; der nächste macht beim Rest weiter."""
    client = client or ShopwareClient(shop)
    client.wartezeit = min(zeitlimit, client.wartezeit)
    ende = time.monotonic() + zeitlimit
    shop.letzter_artikelabgleich = timezone.now()  # gleich zu Beginn, damit der nächste Zeitplan-Aufruf nicht parallel startet
    shop.save(update_fields=["letzter_artikelabgleich"])
    artikel = Artikel.objects.shop_sichtbar().filter(typ__in=["A", "G"]) \
        .select_related("steuersatz", "kategorie", "hersteller").prefetch_related("bilder")
    if nummer:
        artikel = artikel.filter(nummer=nummer)
        if not artikel:
            raise ShopFehler(f"Artikel {nummer} gibt es nicht oder er ist nicht online.")
    artikel = list(artikel)
    zuordnungen = {z.artikel_id: z for z in ShopArtikel.objects.filter(shop=shop)}
    if alle:  # zuletzt Gesendete ans Ende, damit wiederholte Läufe mit Zeitlimit alle Artikel erreichen
        def zuletzt(a):
            z = zuordnungen.get(a.pk)
            return (1, z.gesendet_am) if z and z.gesendet_am else (0, timezone.now())
        artikel.sort(key=zuletzt)
    try:
        ids = client.produkt_ids([a.nummer for a in artikel])
        ergebnis = {"neu": 0, "aktualisiert": 0, "unveraendert": 0, "ohne_preis": 0, "offen": 0, "fehler": [], "kategorie_fehlt": set(),
                    "kategorie_neu": [], "kategorie_geaendert": []}
        lauf = {"kategorien": KategorieKarte(shop, client, ergebnis), "hersteller": _nach_name(client.hersteller()), "ergebnis": ergebnis,
                "steuern": {Decimal(str(s["taxRate"])).normalize(): s["id"] for s in client.steuern()}, "ende": ende,
                "regel_ok": regel_pruefen(shop, client)}
    except ShopFehler as fehler:
        SyncProtokoll.objects.create(shop=shop, art="artikeldaten", erfolgreich=False, meldung=str(fehler)[:500])
        raise
    for stelle, a in enumerate(artikel):
        if time.monotonic() > ende:
            ergebnis["offen"] += len(artikel) - stelle
            break
        try:
            with transaction.atomic():
                ergebnis[_artikel_senden(shop, client, a, zuordnungen.get(a.pk), ids.get(a.nummer), lauf, alle)] += 1
        except ShopFehler as fehler:
            ergebnis["fehler"].append(f"{a.nummer}: {fehler}")
            lauf["kategorien"].fertig.clear()  # die Transaktion des Artikels ist zurückgerollt: Zuordnungen neu ermitteln
            lauf["kategorien"].zuordnung = {z.kategorie_id: z for z in ShopKategorie.objects.filter(shop=shop)}
    if ergebnis["offen"] and shop.artikeldaten_takt:  # beim nächsten Zeitplan-Aufruf weitermachen, nicht erst nach dem Takt
        shop.letzter_artikelabgleich = timezone.now() - timedelta(minutes=shop.artikeldaten_takt)
        shop.save(update_fields=["letzter_artikelabgleich"])
    SyncProtokoll.objects.create(shop=shop, art="artikeldaten", erfolgreich=not ergebnis["fehler"], meldung=artikeldaten_meldung(ergebnis)[:500])
    return ergebnis


def artikeldaten_meldung(e: dict) -> str:
    teile = [f"{e['neu']} neu angelegt, {e['aktualisiert']} aktualisiert, {e['unveraendert']} unverändert"]
    if e["ohne_preis"]:
        teile.append(f"{e['ohne_preis']} ohne Preis nicht angelegt")
    if e["offen"]:
        teile.append(f"{e['offen']} noch offen, nächster Lauf macht weiter")
    if e.get("kategorie_neu"):
        teile.append(f"{len(e['kategorie_neu'])} Kategorie(n) im Shop angelegt: " + ", ".join(e["kategorie_neu"]))
    if e.get("kategorie_geaendert"):
        teile.append("Kategorie im Shop angepasst: " + ", ".join(e["kategorie_geaendert"]))
    if e["kategorie_fehlt"]:
        teile.append("Kategorie nicht angelegt, weil sie im Shop auf derselben Ebene doppelt vorkommt (im Shop eine umbenennen "
                     "oder löschen): " + ", ".join(sorted(e["kategorie_fehlt"])))
    if e["fehler"]:
        teile.append(f"{len(e['fehler'])} Probleme: " + "; ".join(e["fehler"][:3]))
    return ", ".join(teile)


def artikeldaten_faellig(shop, jetzt=None) -> bool:
    if not shop.artikeldaten_takt:
        return False
    jetzt = jetzt or timezone.now()
    return not shop.letzter_artikelabgleich or jetzt - shop.letzter_artikelabgleich >= timedelta(minutes=shop.artikeldaten_takt)
