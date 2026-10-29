from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from django.db import models, transaction
from django.db.models import F, Q, Sum
from django.utils import timezone

from apps.lager.models import Bewegungsart, Lagerbewegung, bestand, reserviert
from apps.esd.models import ESDKey
from apps.esd.services import zeilen as esd_zeilen
from apps.lager.services import seriennummern_einlagern, zeilen
from apps.stammdaten.models import Artikel, ArtikelLieferant, ArtikelMandant, Lieferant, Preislistenartikel
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


def bestellposition(bestellung, artikel, menge, ek=None, preisliste_artikel=None) -> BestellPosition:
    """Position zur Bestellung: eigener Artikel oder Artikel aus der Preisliste des Lieferanten (wird beim Wareneingang
    zum eigenen Artikel)."""
    if bestellung.status not in (BestellStatus.ENTWURF, BestellStatus.BESTELLT, BestellStatus.TEIL):
        raise ValueError("Zu dieser Bestellung können keine Positionen mehr erfasst werden.")
    if artikel is None and preisliste_artikel is not None and preisliste_artikel.artikel_id:
        artikel = preisliste_artikel.artikel  # zu dem Eintrag gibt es schon einen eigenen Artikel
    if artikel is None:
        if preisliste_artikel is None:
            raise ValueError("Artikel fehlt.")
        if preisliste_artikel.lieferant_id != bestellung.lieferant_id:
            raise ValueError(f"{preisliste_artikel.nummer} gehört zur Preisliste von {preisliste_artikel.lieferant.name1}, "
                             f"nicht zu {bestellung.lieferant.name1}.")
        pos = bestellung.positionen.filter(preisliste_artikel=preisliste_artikel, artikel__isnull=True, geliefert=0).first()
        if pos:
            pos.menge += menge
            pos.save()
            return pos
        return BestellPosition.objects.create(
            bestellung=bestellung, preisliste_artikel=preisliste_artikel, bestellnummer=preisliste_artikel.bestellnummer,
            bezeichnung=preisliste_artikel.bezeichnung[:255], menge=menge, ek=ek if ek is not None else preisliste_artikel.ek)
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
    for p in beleg.positionen.select_related("artikel", "preisliste_artikel__lieferant"):
        if p.preisliste_artikel_id and not p.artikel_id:  # Typ P: der Lieferant steht an der Preislistenzeile
            je_lieferant[p.preisliste_artikel.lieferant].append(p)
            continue
        if not p.artikel_id or p.artikel.typ != "A":
            continue
        al = guenstigster_lieferant(p.artikel)
        (je_lieferant[al.lieferant] if al else ohne).append(p)
    ergebnis = []
    for lieferant, positionen in je_lieferant.items():
        b = bestellung_anlegen(beleg.mandant, lieferant, f"Aus {beleg}", aus_beleg=beleg)
        for p in positionen:
            bestellposition(b, p.artikel, p.menge, preisliste_artikel=p.preisliste_artikel)
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


@transaction.atomic
def artikel_aus_preisliste(eintrag):
    """Aus einem Preislistenartikel (Typ P) wird beim Wareneingang ein eigener Artikel (Typ A)."""
    if eintrag.artikel_id:
        return eintrag.artikel
    from apps.stammdaten.models import Hersteller, Steuersatz
    nummer = (eintrag.herstellernummer or "")[:40]
    artikel = Artikel.objects.filter(nummer__iexact=nummer).first() if nummer else None
    if artikel is not None and artikel.herstellernummer.lower() != eintrag.herstellernummer.lower():
        artikel = None  # gleiche Nummer, aber anderes Produkt
    if artikel is None:
        if not nummer or Artikel.objects.filter(nummer__iexact=nummer).exists():
            nummer = f"P{eintrag.lieferant.nummer}-{eintrag.bestellnummer}"[:40]
        steuer = Steuersatz.objects.filter(nummer=1).first() or Steuersatz.objects.order_by("-prozent").first()
        if steuer is None:
            raise ValueError("Es ist kein Steuersatz angelegt.")
        hersteller = Hersteller.objects.filter(name__iexact=eintrag.hersteller).first() if eintrag.hersteller else None
        if eintrag.hersteller and hersteller is None:
            hersteller = Hersteller.objects.create(name=eintrag.hersteller[:150])
        artikel = Artikel.objects.create(
            nummer=nummer, typ="A", bezeichnung=eintrag.bezeichnung, herstellernummer=eintrag.herstellernummer, ean=eintrag.ean,
            hersteller=hersteller, gewicht_kg=eintrag.gewicht_kg, steuersatz=steuer)
    eintrag.artikel = artikel
    eintrag.save(update_fields=["artikel"])
    ArtikelLieferant.objects.get_or_create(
        lieferant=eintrag.lieferant, bestellnummer=eintrag.bestellnummer,
        defaults={"artikel": artikel, "ek": eintrag.ek, "bestand": eintrag.bestand, "lieferzeit_tage": eintrag.lieferzeit_tage,
                  "preisliste": eintrag.preisliste, "aktualisiert_am": eintrag.aktualisiert_am})
    BestellPosition.objects.filter(preisliste_artikel=eintrag, artikel__isnull=True).update(artikel=artikel)
    umstellen = Position.objects.filter(preisliste_artikel=eintrag, artikel__isnull=True).exclude(
        beleg__art__in=("RE", "GS"), beleg__abgeschlossen=True)  # Verkaufspositionen (außer abgeschlossene Rechnungen) werden zu A
    umstellen.update(artikel=artikel, typ="A", preisliste_artikel=None)
    return artikel


def artikel_per_code(code: str, lieferant=None):
    """Artikel über Scan-Code: EAN, Artikelnummer, Herstellernummer oder Bestellnummer des Lieferanten.
    Findet sich nur ein Preislistenartikel des Lieferanten, wird daraus der eigene Artikel angelegt."""
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
    if al:
        return al.artikel
    if lieferant is not None:
        eintrag = Preislistenartikel.objects.filter(lieferant=lieferant).filter(
            Q(bestellnummer__iexact=code) | Q(herstellernummer__iexact=code) | Q(ean=code)).first()
        if eintrag:
            return artikel_aus_preisliste(eintrag)
    return None


@transaction.atomic
def wareneingang_erfassen(we, artikel, menge=Decimal("1"), ek=None, seriennummern="") -> WareneingangPosition:
    """Zeile zum Wareneingang; derselbe Artikel (ohne Seriennummern) erhöht die vorhandene Zeile (Scan +1)."""
    if we.gebucht:
        raise ValueError("Der Wareneingang ist bereits gebucht.")
    if menge is None or not menge.is_finite() or menge <= 0:
        raise ValueError("Die Menge muss größer als 0 sein.")
    if ek is not None and (not ek.is_finite() or ek < 0):
        raise ValueError("Der EK darf nicht negativ sein.")
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
    for bp in we.bestellung.positionen.select_related("preisliste_artikel"):
        if bp.artikel_id is None and bp.preisliste_artikel_id:  # Preislistenartikel wird zum eigenen Artikel
            bp.artikel = artikel_aus_preisliste(bp.preisliste_artikel)
            bp.save(update_fields=["artikel"])
        if bp.offen > 0 and not we.positionen.filter(bestellposition=bp).exists():
            WareneingangPosition.objects.create(wareneingang=we, bestellposition=bp, artikel=bp.artikel, menge=bp.offen, ek=bp.ek)


@transaction.atomic
def wareneingang_buchen(we, benutzer=None) -> int:
    """Bucht Bestand, letzten EK, Durchschnitts-EK, Seriennummern und Liefermengen der Bestellung."""
    we = Wareneingang.objects.select_for_update().get(pk=we.pk)  # Sperre: Doppelklick/zweiter Tab darf nicht doppelt buchen
    if we.gebucht:
        raise ValueError("Der Wareneingang ist bereits gebucht.")
    positionen = list(we.positionen.select_related("artikel", "bestellposition"))
    if not positionen:
        raise ValueError("Keine Positionen erfasst.")
    for p in positionen:  # vorab prüfen, damit nichts halb gebucht wird
        if p.menge <= 0 or p.ek < 0:
            raise ValueError(f"{p.artikel.nummer}: Menge muss größer als 0 und der EK darf nicht negativ sein.")
        sn = zeilen(p.seriennummern)
        if sn and len(sn) != p.menge:
            raise ValueError(f"{p.artikel.nummer}: {len(sn)} Seriennummern, aber Menge {p.menge:f}.")
        if p.artikel.esd_key:
            keys = [k for k, _ in esd_zeilen(p.esd_keys)]
            if len(keys) != p.menge:
                raise ValueError(f"{p.artikel.nummer} (ESD): {len(keys)} Key(s) erfasst, aber Menge {p.menge:f} – bitte je Stück einen Key eintragen.")
            if len(set(keys)) != len(keys):
                raise ValueError(f"{p.artikel.nummer} (ESD): ein Key steht mehrfach in der Liste.")
            vorhanden = ESDKey.objects.filter(mandant=we.mandant, artikel=p.artikel, key__in=keys).values_list("key", flat=True)
            if vorhanden:
                raise ValueError(f"{p.artikel.nummer} (ESD): Key bereits im Bestand: {', '.join(list(vorhanden)[:3])}.")
    heute = we.datum
    for p in positionen:
        a = Artikel.objects.select_for_update().get(pk=p.artikel_id)  # frisch laden: mehrere Zeilen desselben Artikels
        vorher = Lagerbewegung.objects.filter(artikel=a).aggregate(s=Sum("menge"))["s"] or Decimal("0")  # Ø-EK gilt für alle Mandanten
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
        if a.esd_key:
            for key, anzahl in esd_zeilen(p.esd_keys):
                ESDKey.objects.create(mandant=we.mandant, artikel=a, key=key, max_vergaben=anzahl or 1)
        if p.bestellposition_id:  # in der Datenbank hochzählen, damit mehrere Zeilen derselben Bestellposition nicht überschreiben
            BestellPosition.objects.filter(pk=p.bestellposition_id).update(geliefert=F("geliefert") + p.menge)
    we.gebucht = True
    we.save()
    if we.bestellung_id:
        we.bestellung.status_aktualisieren()
    return len(positionen)


# ---- Lieferantenrechnung / Zahlung ----------------------------------------------------------------

def rechnung_erfassen(mandant, lieferant, rechnungsnummer, datum, netto, brutto, steuer=None, quelle="manuell",
                      wareneingaenge=()) -> Lieferantenrechnung:
    if netto is None or brutto is None or netto <= 0 or brutto <= 0:
        raise ValueError("Netto und Brutto müssen größer als 0 sein (Gutschriften des Lieferanten bitte nicht als Rechnung erfassen).")
    if Lieferantenrechnung.objects.filter(mandant=mandant, lieferant=lieferant, rechnungsnummer=rechnungsnummer).exists():
        raise ValueError(f"Die Rechnung {rechnungsnummer} von {lieferant.name1} ist bereits erfasst.")
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
