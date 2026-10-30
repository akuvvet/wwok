"""Lieferantenvergleich: Wo ist ein Produkt für die gewünschte Menge wirklich am günstigsten?

Gerechnet wird mit Warenwert plus den Versandkosten, die diese Bestellung zusätzlich auslöst. Ein offener Entwurf beim
Lieferanten zählt mit: Er kann den Mindestbestellwert oder die Versandkostenfreiheit erreichen. Entschieden wird von Hand –
der Vergleich sortiert und weist hin, er bestellt nichts selbst."""
from decimal import Decimal

from django.db import transaction
from django.db.models import Q

from apps.stammdaten.models import ArtikelLieferant, Lieferant, Preislistenartikel

from . import services
from .models import BestellPosition, Bestellung, BestellStatus

CENT = Decimal("0.01")


def eur(betrag) -> str:
    """Betrag für Hinweistexte, deutsch geschrieben: 1.234,50"""
    return f"{betrag:,.2f}".replace(",", " ").replace(".", ",").replace(" ", ".")


def passende_preislisten(artikel=None, eintrag=None):
    """Einträge aller Preislisten zum selben Produkt: verknüpft, gleiche EAN oder gleiche Herstellernummer."""
    quelle = artikel or eintrag
    bedingung = Q(pk=eintrag.pk) if eintrag else Q(pk__in=[])
    if artikel:
        bedingung |= Q(artikel=artikel)
    if quelle.ean:
        bedingung |= Q(ean=quelle.ean)
    if quelle.herstellernummer:
        bedingung |= Q(herstellernummer__iexact=quelle.herstellernummer)
    eintraege = Preislistenartikel.objects.filter(bedingung, lieferant__gesperrt=False, ek__gt=0).select_related("lieferant")
    if artikel:  # mit einem anderen eigenen Artikel verknüpft: anderes Produkt, auch bei gleicher Nummer
        eintraege = eintraege.exclude(Q(artikel__isnull=False) & ~Q(artikel=artikel))
    return [e for e in eintraege if gleiches_produkt(quelle, e, artikel, eintrag)]


def gleiches_produkt(quelle, e, artikel, eintrag) -> bool:
    """Herstellernummern sind nicht eindeutig: verschiedene EAN oder verschiedener Hersteller schließen einen Treffer aus."""
    if (eintrag and e.pk == eintrag.pk) or (artikel and e.artikel_id == artikel.pk):
        return True
    if quelle.ean and e.ean:
        return quelle.ean == e.ean
    hersteller = (artikel.hersteller.name if artikel and artikel.hersteller_id else (eintrag.hersteller if eintrag else "")).strip().lower()
    anderer = e.hersteller.strip().lower()
    return not hersteller or not anderer or hersteller in anderer or anderer in hersteller


def angebote(artikel=None, eintrag=None, menge=Decimal("1")) -> list:
    """Je Lieferant das günstigste Angebot, das die Menge liefern kann; sonst das günstigste überhaupt."""
    beste = {}

    def rang(ek, bestand):
        return (bestand is not None and bestand < menge, ek)

    def merken(lieferant, ek, bestand, lieferzeit, bestellnummer, stand, pl=None):
        vorhanden = beste.get(lieferant.pk)
        if vorhanden is None or rang(ek, bestand) < rang(vorhanden["ek"], vorhanden["bestand"]):
            beste[lieferant.pk] = {"lieferant": lieferant, "ek": ek, "bestand": bestand, "bestellnummer": bestellnummer, "pl": pl,
                                   "lieferzeit": lieferzeit if lieferzeit is not None else lieferant.lieferzeit_tage, "stand": stand}

    if artikel:
        for al in ArtikelLieferant.objects.filter(artikel=artikel, ek__gt=0, lieferant__gesperrt=False).select_related("lieferant"):
            merken(al.lieferant, al.ek, al.bestand, al.lieferzeit_tage, al.bestellnummer, al.aktualisiert_am)
    for e in passende_preislisten(artikel, eintrag):
        merken(e.lieferant, e.ek, e.bestand, e.lieferzeit_tage, e.bestellnummer, e.aktualisiert_am, pl=e)
    return list(beste.values())


def versand(lieferant, warenwert):
    """Versandkosten bei diesem Warenwert; None = nicht hinterlegt."""
    if warenwert <= 0:
        return Decimal("0")
    if lieferant.versandkostenfrei_ab is not None and warenwert >= lieferant.versandkostenfrei_ab:
        return Decimal("0")
    return lieferant.versandkosten


def entwurf(mandant, lieferant, bevorzugt=None):
    """Offener Entwurf des Lieferanten: die gerade bearbeitete Bestellung, sonst der neueste."""
    if bevorzugt is not None and bevorzugt.lieferant_id == lieferant.pk and bevorzugt.status == BestellStatus.ENTWURF:
        return bevorzugt
    return Bestellung.objects.filter(mandant=mandant, lieferant=lieferant, status=BestellStatus.ENTWURF).order_by("-id").first()


def entwurfswert(bestellung, ohne_position=None) -> Decimal:
    if bestellung is None:
        return Decimal("0")
    return sum((p.netto for p in bestellung.positionen.all() if p.pk != getattr(ohne_position, "pk", None)), Decimal("0"))


def vergleich(mandant, menge, artikel=None, eintrag=None, ohne_position=None, bevorzugt=None) -> list:
    """Angebote sortiert: zuerst lieferbar und Mindestbestellwert erreicht, darin nach Gesamtkosten dieser Menge."""
    zeilen = []
    bevorzugt = ohne_position.bestellung if ohne_position is not None else bevorzugt
    for a in angebote(artikel, eintrag, menge):
        l = a["lieferant"]
        b = entwurf(mandant, l, bevorzugt)
        vorher = entwurfswert(b, ohne_position)
        ware = (a["ek"] * menge).quantize(CENT)
        nachher = vorher + ware
        v_neu, v_alt = versand(l, nachher), versand(l, vorher)
        offen = v_neu is None or v_alt is None
        zusatz = Decimal("0") if offen else v_neu - v_alt  # kann negativ sein: die Menge macht den Entwurf versandkostenfrei
        fehlt = max((l.mindestbestellwert or Decimal("0")) - nachher, Decimal("0"))
        lieferbar = a["bestand"] is None or a["bestand"] >= menge
        hinweise = []
        if fehlt:
            hinweise.append(f"Mindestbestellwert {eur(l.mindestbestellwert)} € – es fehlen {eur(fehlt)} €")
        if a["bestand"] is not None and not lieferbar:
            hinweise.append(f"nur {a['bestand']} auf Lager" if a["bestand"] > 0 else "nicht auf Lager")
        if offen:
            hinweise.append("Versandkosten nicht hinterlegt")
        elif l.versandkostenfrei_ab is not None and nachher < l.versandkostenfrei_ab:
            hinweise.append(f"versandkostenfrei ab {eur(l.versandkostenfrei_ab)} € – es fehlen {eur(l.versandkostenfrei_ab - nachher)} €")
        if vorher:
            hinweise.append(f"Entwurf {b.nummer} über {eur(vorher)} € wird ergänzt")
        zeilen.append({**a, "ware": ware, "versand": zusatz, "versand_offen": offen, "gesamt": ware + zusatz,
                       "je_stueck": ((ware + zusatz) / menge).quantize(CENT), "fehlt": fehlt, "lieferbar": lieferbar,
                       "hinweise": hinweise, "entwurf": b})
    zeilen.sort(key=lambda z: (not z["lieferbar"], bool(z["fehlt"]), z["gesamt"], z["lieferzeit"] if z["lieferzeit"] is not None else 999))
    if zeilen:
        erste = zeilen[0]  # „am günstigsten“ nur, wenn die Gesamtkosten feststehen
        erste["empfehlung"] = erste["lieferbar"] and not erste["fehlt"] and not any(z["versand_offen"] for z in zeilen)
    return zeilen


@transaction.atomic
def bestellen(mandant, lieferant_id, menge, artikel=None, eintrag=None, position=None, bevorzugt=None) -> Bestellung:
    """Legt das gewählte Angebot in den offenen Entwurf des Lieferanten (oder einen neuen) – mit dem Preis aus dem Vergleich.
    Mit position wird eine Entwurfsposition zum neuen Lieferanten verschoben."""
    if menge <= 0:
        raise ValueError("Die Menge muss größer als 0 sein.")
    angebot = next((a for a in angebote(artikel, eintrag, menge) if a["lieferant"].pk == lieferant_id), None)
    if angebot is None:
        raise ValueError("Dieser Lieferant führt das Produkt nicht (mehr).")
    lieferant = Lieferant.objects.get(pk=lieferant_id)
    if position is not None:
        position = BestellPosition.objects.select_for_update().select_related("bestellung").get(pk=position.pk)
        if position.bestellung.status != BestellStatus.ENTWURF or position.geliefert:
            raise ValueError("Nur Positionen aus Entwürfen ohne Lieferung lassen sich verschieben.")
        if position.bestellung.lieferant_id == lieferant_id:
            return position.bestellung  # steht schon beim gewählten Lieferanten
    ziel = entwurf(mandant, lieferant, bevorzugt) or services.bestellung_anlegen(mandant, lieferant, "Aus Lieferantenvergleich")
    pl = angebot["pl"]
    if artikel is None and pl is not None and pl.artikel_id is None:
        neu = services.bestellposition(ziel, None, menge, angebot["ek"], preisliste_artikel=pl)
    else:
        neu = services.bestellposition(ziel, artikel or pl.artikel, menge, angebot["ek"])
    if angebot["bestellnummer"] and neu.bestellnummer != angebot["bestellnummer"]:
        neu.bestellnummer = angebot["bestellnummer"]
        neu.save(update_fields=["bestellnummer"])
    if position is not None:
        position.delete()
    return ziel


def alternative(position):
    """Günstigeres Angebot eines anderen Lieferanten für eine Entwurfsposition – gemessen am EK der Position samt Versand;
    nur lieferbare Angebote mit erreichtem Mindestbestellwert."""
    b = position.bestellung
    artikel = position.artikel or (position.preisliste_artikel.artikel if position.preisliste_artikel_id else None)
    eintrag = None if artikel else position.preisliste_artikel
    if artikel is None and eintrag is None:
        return None
    zeilen = vergleich(b.mandant, position.menge, artikel, eintrag, ohne_position=position)
    beste = next((z for z in zeilen if z["lieferbar"] and not z["fehlt"] and z["lieferant"].pk != b.lieferant_id), None)
    if beste is None:
        return None
    gesamtwert = entwurfswert(b)
    mit, ohne = versand(b.lieferant, gesamtwert), versand(b.lieferant, gesamtwert - position.netto)
    eigene = position.netto + (mit - ohne if mit is not None and ohne is not None else Decimal("0"))
    if beste["gesamt"] >= eigene:
        return None
    return {**beste, "ersparnis": (eigene - beste["gesamt"]).quantize(CENT)}
