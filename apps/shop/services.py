from datetime import datetime, timedelta, timezone as tz
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.utils import timezone

from apps.finanzen.models import Zahlart
from apps.finanzen.services import zahlung_buchen
from apps.lager.models import bestand, gruppen_verfuegbar, reserviert
from apps.stammdaten.models import Anschriftart, Ansprechpartner, Artikel, Kunde, Kundenanschrift, Zahlungsbedingung
from apps.stammdaten.preise import stufenpreis
from apps.verkauf.models import Beleg, Belegart, Position

from .models import ShopArtikel, ShopBestellung, SyncProtokoll
from .shopware import STANDARD_WAEHRUNG_EUR, ShopFehler, ShopwareClient

CENT = Decimal("0.01")


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


# ---- Bestellimport -----------------------------------------------------------------------------------

def adresse_text(a: dict) -> str:
    name = " ".join(t for t in (g(a, "firstName"), g(a, "lastName")) if t)
    firma = g(a, "company")
    zeilen = [firma or name, name if firma else "", g(a, "street"), f"{g(a, 'zipcode')} {g(a, 'city')}".strip()]
    land = g(a, "country", "iso")
    if land and land != "DE":
        zeilen.append(land)
    return "\n".join(z for z in zeilen if z)


def naechste_kundennummer() -> str:
    zahlen = [int(n) for n in Kunde.objects.values_list("nummer", flat=True) if n.isdigit()]
    return str(max(zahlen, default=40000) + 1)


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
        nummer=naechste_kundennummer(), name1=(firma or person)[:150], name2=(person if firma else "")[:150],
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
    """Eine Shopware-Bestellung als Auftrag anlegen; Rückgabe: ShopBestellung."""
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
    for z in b.get("lineItems") or []:
        if z.get("type") not in (None, "product"):
            continue  # Gutscheine, Rabatte usw. werden nicht übernommen
        steuer = dez(g(z, "price", "calculatedTaxes", 0, "taxRate", standard=19))
        produktnummer = g(z, "payload", "productNumber")
        artikel = Artikel.objects.filter(nummer=produktnummer).first() if produktnummer else None
        if artikel is None:
            hinweise.append(f"Artikel {produktnummer or z.get('label')} nicht im ERP")
        Position.objects.create(
            beleg=beleg, typ=artikel.typ if artikel else "L", artikel=artikel, bezeichnung=str(z.get("label") or produktnummer)[:255],
            menge=dez(z.get("quantity") or 1), einzelpreis=netto(z.get("unitPrice"), steuer), steuer_prozent=steuer,
            zusatztext="" if artikel else f"Artikelnummer im Shop: {produktnummer}")
    versandkosten = g(lieferung, "shippingCosts", "totalPrice", standard=g(b, "shippingCosts", "totalPrice", standard=0))
    if dez(versandkosten):
        beleg.versandkosten = netto(versandkosten, g(lieferung, "shippingCosts", "calculatedTaxes", 0, "taxRate", standard=19))
        beleg.save()
    if not beleg.positionen.exists():
        raise ValueError(f"Bestellung {nummer} enthält keine Produktpositionen.")
    beleg.berechne()
    if abs(beleg.summe_brutto - dez(b.get("amountTotal"))) > Decimal("0.05"):
        hinweise.append(f"Summe ERP {beleg.summe_brutto} € weicht vom Shop ({dez(b.get('amountTotal'))} €) ab")
    status = g((b.get("transactions") or [{}])[0], "stateMachineState", "technicalName")
    if status == "paid":  # bereits bezahlt (z. B. PayPal) → Anzahlung auf den Auftrag
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


def abgleichen(shop, client=None, alle=False) -> dict:
    """Bestände und Preise der Online-Artikel an den Shop melden; ohne 'alle' nur Änderungen seit dem letzten Abgleich."""
    client = client or ShopwareClient(shop)
    artikel = list(Artikel.objects.filter(online=True, nicht_aktiv=False, typ__in=["A", "G"]).select_related("steuersatz"))
    ids = client.produkt_ids([a.nummer for a in artikel])
    payload, vorgemerkt, nicht_im_shop = [], [], 0
    for a in artikel:
        produkt_id = ids.get(a.nummer)
        if not produkt_id:
            nicht_im_shop += 1
            continue
        zuordnung, _ = ShopArtikel.objects.get_or_create(shop=shop, artikel=a, defaults={"produkt_id": produkt_id})
        bestand_neu = meldebestand(shop, a)
        preis_netto = stufenpreis(a, shop.preisstufe)
        preis_brutto = (preis_netto * (1 + a.steuersatz.prozent / 100)).quantize(CENT, rounding=ROUND_HALF_UP) if preis_netto is not None else None
        geaendert = alle or zuordnung.gemeldeter_bestand != bestand_neu or zuordnung.gemeldeter_preis != preis_brutto
        if not geaendert:
            continue
        eintrag = {"id": produkt_id}
        if bestand_neu is not None:
            eintrag["stock"] = bestand_neu
        if preis_brutto is not None:
            eintrag["price"] = [{"currencyId": STANDARD_WAEHRUNG_EUR, "gross": float(preis_brutto), "net": float(preis_netto), "linked": True}]
        payload.append(eintrag)
        vorgemerkt.append((zuordnung, produkt_id, bestand_neu, preis_brutto))
    if payload:
        client.produkte_aktualisieren(payload)
    jetzt = timezone.now()
    for zuordnung, produkt_id, bestand_neu, preis_brutto in vorgemerkt:
        zuordnung.produkt_id, zuordnung.gemeldeter_bestand, zuordnung.gemeldeter_preis, zuordnung.gesendet_am = produkt_id, bestand_neu, preis_brutto, jetzt
        zuordnung.save()
    shop.letzter_abgleich = jetzt
    shop.save(update_fields=["letzter_abgleich"])
    ergebnis = {"gesendet": len(payload), "nicht_im_shop": nicht_im_shop, "geprueft": len(artikel)}
    SyncProtokoll.objects.create(shop=shop, art="abgleich", meldung=f"{ergebnis['gesendet']} Artikel gesendet, {nicht_im_shop} nicht im Shop gefunden, {len(artikel)} geprüft")
    return ergebnis
