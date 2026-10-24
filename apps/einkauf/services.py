from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from django.db import models, transaction
from django.db.models import Q, Sum
from django.utils import timezone

from apps.lager.models import Bewegungsart, Lagerbewegung, bestand, reserviert
from apps.lager.services import seriennummern_einlagern, zeilen
from apps.stammdaten.models import Artikel, ArtikelLieferant, ArtikelMandant, Lieferant
from apps.verkauf.models import Belegart, Nummernkreis, Position

from .models import (BestellPosition, BestellStatus, Bestellung, Lieferantenrechnung, Rma, RmaStatus,
                     RechnungStatus, Wareneingang, WareneingangPosition)


def nummer(mandant, art: str) -> str:
    return Nummernkreis.naechste(mandant, art, timezone.localdate().year)


# ---- Bestellung ---------------------------------------------------------------------------------

def ek_vorschlag(mandant, lieferant, artikel):
    """(EK, Bestellnummer des Lieferanten): Preisliste, sonst letzter EK des Mandanten."""
    al = ArtikelLieferant.objects.filter(artikel=artikel, lieferant=lieferant).first()
    if al and al.ek is not None:
        return al.ek, al.bestellnummer
    am = ArtikelMandant.objects.filter(artikel=artikel, mandant=mandant).first()
    return (am.letzter_ek if am and am.letzter_ek is not None else artikel.durchschnitts_ek or Decimal("0")), (al.bestellnummer if al else "")


@transaction.atomic
def bestellung_anlegen(mandant, lieferant, bemerkung="", aus_beleg=None) -> Bestellung:
    return Bestellung.objects.create(mandant=mandant, lieferant=lieferant, nummer=nummer(mandant, "BE"),
                                     bemerkung=bemerkung, aus_beleg=aus_beleg)


def bestellposition(bestellung, artikel, menge, ek=None) -> BestellPosition:
    if bestellung.status not in (BestellStatus.ENTWURF, BestellStatus.BESTELLT, BestellStatus.TEIL):
        raise ValueError("Zu dieser Bestellung können keine Positionen mehr erfasst werden.")
    vorschlag, bestellnr = ek_vorschlag(bestellung.mandant, bestellung.lieferant, artikel)
    pos = bestellung.positionen.filter(artikel=artikel, geliefert=0).first()
    if pos:  # gleicher Artikel: Menge addieren
        pos.menge += menge
        pos.save()
        return pos
    return BestellPosition.objects.create(bestellung=bestellung, artikel=artikel, bestellnummer=bestellnr,
                                          bezeichnung=artikel.bezeichnung, menge=menge, ek=ek if ek is not None else vorschlag)


def bestellen(bestellung):
    if not bestellung.positionen.exists():
        raise ValueError("Bestellung ohne Positionen.")
    if bestellung.status == BestellStatus.ENTWURF:
        bestellung.status = BestellStatus.BESTELLT
        bestellung.save(update_fields=["status"])


def guenstigster_lieferant(artikel):
    """Günstigster Lieferant aus den Preislisten; Lieferanten mit Bestand 0 nur, wenn sonst keiner existiert."""
    kandidaten = ArtikelLieferant.objects.filter(artikel=artikel, ek__isnull=False, lieferant__gesperrt=False)
    return (kandidaten.filter(Q(bestand__isnull=True) | Q(bestand__gt=0)).order_by("ek").first()
            or kandidaten.order_by("ek").first())


@transaction.atomic
def bestellungen_aus_beleg(beleg) -> list:
    """Angebot/Auftrag in Lieferantenbestellungen kopieren: je Lieferant eine Bestellung (günstigster Lieferant)."""
    je_lieferant = defaultdict(list)
    ohne = []
    for p in beleg.positionen.select_related("artikel"):
        if not p.artikel_id or p.artikel.typ not in ("A", "P"):
            continue
        al = guenstigster_lieferant(p.artikel)
        (je_lieferant[al.lieferant] if al else ohne).append(p)
    ergebnis = []
    for lieferant, positionen in je_lieferant.items():
        b = bestellung_anlegen(beleg.mandant, lieferant, f"Aus {beleg}", aus_beleg=beleg)
        for p in positionen:
            bestellposition(b, p.artikel, p.menge)
        ergebnis.append(b)
    if not ergebnis:
        raise ValueError("Keiner der Artikel hat einen Lieferanten in den Preislisten.")
    beleg._ohne_lieferant = ohne
    return ergebnis


def offen_bestellt(mandant, artikel) -> Decimal:
    return BestellPosition.objects.filter(
        bestellung__mandant=mandant, artikel=artikel,
        bestellung__status__in=[BestellStatus.BESTELLT, BestellStatus.TEIL]).aggregate(
        s=Sum(models.F("menge") - models.F("geliefert")))["s"] or Decimal("0")


def bestellvorschlag(mandant) -> list:
    """Artikel unter Mindestbestand (verfügbar + bereits bestellt). Ohne festen Mindestbestand: ein Monat Reichweite
    aus dem Verkaufsverlauf der letzten 90 Tage."""
    seit = timezone.localdate() - timedelta(days=90)
    verkauft = {s["artikel"]: -s["m"] for s in Lagerbewegung.objects.filter(
        mandant=mandant, art=Bewegungsart.VERKAUF, zeitpunkt__date__gte=seit).values("artikel").annotate(m=Sum("menge"))}
    fest = {d.artikel_id: d.mindestbestand for d in ArtikelMandant.objects.filter(mandant=mandant, mindestbestand__isnull=False)}
    vorschlag = []
    for artikel_id in set(fest) | set(verkauft):
        artikel = Artikel.objects.filter(pk=artikel_id, typ__in=["A", "P"], nicht_aktiv=False).first()
        if not artikel:
            continue
        if artikel_id in fest:
            ziel, grund = Decimal(fest[artikel_id]), "Mindestbestand"
        else:
            ziel, grund = (verkauft[artikel_id] / 3).to_integral_value(rounding="ROUND_CEILING"), "Verkaufsverlauf (1 Monat)"
        haben = bestand(mandant, artikel) - reserviert(mandant, artikel) + offen_bestellt(mandant, artikel)
        if ziel > 0 and haben < ziel:
            al = guenstigster_lieferant(artikel)
            vorschlag.append({"artikel": artikel, "ziel": ziel, "haben": haben, "menge": ziel - haben, "grund": grund,
                              "lieferant": al.lieferant if al else None, "ek": al.ek if al else None})
    return sorted(vorschlag, key=lambda z: z["artikel"].nummer)


@transaction.atomic
def bestellungen_aus_vorschlag(mandant, auswahl) -> list:
    """auswahl: Liste von (artikel_id, menge); je Lieferant wird eine Entwurfsbestellung angelegt."""
    je_lieferant = defaultdict(list)
    for artikel_id, menge in auswahl:
        artikel = Artikel.objects.get(pk=artikel_id)
        al = guenstigster_lieferant(artikel)
        if al is None:
            raise ValueError(f"{artikel.nummer}: kein Lieferant in den Preislisten.")
        je_lieferant[al.lieferant].append((artikel, menge))
    ergebnis = []
    for lieferant, zeilen_ in je_lieferant.items():
        b = bestellung_anlegen(mandant, lieferant, "Bestellvorschlag")
        for artikel, menge in zeilen_:
            bestellposition(b, artikel, menge)
        ergebnis.append(b)
    return ergebnis


# ---- Wareneingang -------------------------------------------------------------------------------

@transaction.atomic
def wareneingang_anlegen(mandant, lieferant, bestellung=None, benutzer=None) -> Wareneingang:
    we = Wareneingang.objects.create(mandant=mandant, lieferant=lieferant, bestellung=bestellung,
                                     nummer=nummer(mandant, "WE"), benutzer=benutzer)
    return we


def artikel_per_code(code: str, lieferant=None):
    """Artikel über Scan-Code: EAN, Artikelnummer, Herstellernummer oder Bestellnummer des Lieferanten."""
    code = code.strip()
    if not code:
        return None
    treffer = Artikel.objects.filter(nicht_aktiv=False).filter(
        Q(ean=code) | Q(nummer__iexact=code) | Q(herstellernummer__iexact=code)).first()
    if treffer:
        return treffer
    al = ArtikelLieferant.objects.filter(bestellnummer__iexact=code)
    if lieferant is not None:
        al = al.filter(lieferant=lieferant)
    al = al.select_related("artikel").first()
    return al.artikel if al else None


@transaction.atomic
def wareneingang_erfassen(we, artikel, menge=Decimal("1"), ek=None, seriennummern="") -> WareneingangPosition:
    """Zeile zum Wareneingang; derselbe Artikel (ohne Seriennummern) erhöht die vorhandene Zeile (Scan +1)."""
    if we.gebucht:
        raise ValueError("Der Wareneingang ist bereits gebucht.")
    bp = None
    if we.bestellung_id:
        bp = we.bestellung.positionen.filter(artikel=artikel).first()
    vorhanden = we.positionen.filter(artikel=artikel).first()
    if vorhanden and not seriennummern and not vorhanden.seriennummern:
        vorhanden.menge += menge
        vorhanden.save()
        return vorhanden
    if ek is None:
        ek = bp.ek if bp else ek_vorschlag(we.mandant, we.lieferant, artikel)[0]
    return WareneingangPosition.objects.create(wareneingang=we, bestellposition=bp, artikel=artikel, menge=menge,
                                               ek=ek, seriennummern=seriennummern)


def wareneingang_aus_bestellung(we):
    """Offene Mengen der Bestellung als Zeilen vorbelegen."""
    for bp in we.bestellung.positionen.all():
        if bp.offen > 0 and not we.positionen.filter(bestellposition=bp).exists():
            WareneingangPosition.objects.create(wareneingang=we, bestellposition=bp, artikel=bp.artikel, menge=bp.offen, ek=bp.ek)


@transaction.atomic
def wareneingang_buchen(we, benutzer=None) -> int:
    """Bucht Bestand, letzten EK, Durchschnitts-EK, Seriennummern und Liefermengen der Bestellung."""
    if we.gebucht:
        raise ValueError("Der Wareneingang ist bereits gebucht.")
    positionen = list(we.positionen.select_related("artikel", "bestellposition"))
    if not positionen:
        raise ValueError("Keine Positionen erfasst.")
    for p in positionen:  # vorab prüfen, damit nichts halb gebucht wird
        sn = zeilen(p.seriennummern)
        if sn and len(sn) != p.menge:
            raise ValueError(f"{p.artikel.nummer}: {len(sn)} Seriennummern, aber Menge {p.menge:f}.")
    heute = we.datum
    for p in positionen:
        a = p.artikel
        vorher = bestand(we.mandant, a)
        Lagerbewegung.objects.create(mandant=we.mandant, artikel=a, art=Bewegungsart.WARENEINGANG, menge=p.menge,
                                     bemerkung=f"{we.nummer} {we.lieferant.name1}", benutzer=benutzer)
        am, _ = ArtikelMandant.objects.get_or_create(artikel=a, mandant=we.mandant)
        am.letzter_ek, am.letzter_ek_datum = p.ek, heute
        am.save()
        if a.durchschnitts_ek is not None and vorher > 0:  # gleitender Durchschnitt über den Bestand
            a.durchschnitts_ek = ((a.durchschnitts_ek * vorher + p.ek * p.menge) / (vorher + p.menge)).quantize(Decimal("0.0001"))
        else:
            a.durchschnitts_ek = p.ek
        if a.typ == "P":  # Lieferantenprodukt wird bei Wareneingang zum Lagerartikel
            a.typ = "A"
        a.save()
        if p.seriennummern.strip():
            seriennummern_einlagern(we.mandant, a, p.seriennummern, we.nummer)
        if p.bestellposition_id:
            p.bestellposition.geliefert += p.menge
            p.bestellposition.save()
    we.gebucht = True
    we.save()
    if we.bestellung_id:
        we.bestellung.status_aktualisieren()
    return len(positionen)


# ---- Lieferantenrechnung / Zahlung ----------------------------------------------------------------

def rechnung_erfassen(mandant, lieferant, rechnungsnummer, datum, netto, brutto, steuer=None, quelle="manuell",
                      wareneingaenge=()) -> Lieferantenrechnung:
    zb = lieferant.zahlungsbedingung
    faellig = datum + timedelta(days=zb.tage_netto if zb and zb.tage_netto else 14)
    skonto_bis = datum + timedelta(days=zb.skonto_tage) if zb and zb.skonto_tage and zb.skonto_prozent else None
    r = Lieferantenrechnung.objects.create(
        mandant=mandant, lieferant=lieferant, rechnungsnummer=rechnungsnummer, datum=datum, faellig=faellig,
        netto=netto, brutto=brutto, steuer=steuer if steuer is not None else brutto - netto,
        skonto_prozent=zb.skonto_prozent if skonto_bis else Decimal("0"), skonto_bis=skonto_bis, quelle=quelle)
    if wareneingaenge:
        r.wareneingaenge.set(wareneingaenge)
    return r


def rma_anlegen(mandant, lieferant, bezeichnung, fehler, artikel=None, seriennummer="", reparatur=None) -> Rma:
    rma = Rma.objects.create(mandant=mandant, lieferant=lieferant, nummer=nummer(mandant, "RM"), bezeichnung=bezeichnung,
                             fehler=fehler, artikel=artikel, seriennummer=seriennummer, reparatur=reparatur)
    if reparatur is not None:
        from apps.werkstatt.models import Status
        reparatur.status = Status.RMA
        reparatur.save()
    return rma
