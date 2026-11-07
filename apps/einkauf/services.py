from collections import defaultdict
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.db import models, transaction
from django.db.models import F, Q, Sum
from django.utils import timezone

from apps.lager.models import Bewegungsart, Lagerbewegung, bestand, reserviert
from apps.esd.models import ESDKey
from apps.esd.services import zeilen as esd_zeilen
from apps.lager.services import seriennummern_einlagern, zeilen
from apps.stammdaten.ekkalk import ek_import as import_ek_von, plausibilitaet_we, wareneingang_ek
from apps.stammdaten.models import Artikel, ArtikelLieferant, ArtikelMandant, Lieferant, Preislistenartikel, keine_leistung
from apps.verkauf.models import Belegart, Nummernkreis, Position

from .models import (BestellPosition, BestellStatus, Bestellung, Lieferantenrechnung, Rma, RmaStatus,
                     RechnungStatus, Wareneingang, WareneingangPosition)


def nummer(mandant, art: str) -> str:
    return Nummernkreis.naechste(mandant, art, timezone.localdate().year)


def ek_runden(ek):
    """EK in Bestellung und Wareneingang hat 2 Stellen, damit EK × Menge sichtbar aufgeht (Preisliste und Ø-EK rechnen genauer)."""
    return None if ek is None else ek.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# ---- Bestellung ---------------------------------------------------------------------------------

def ek_vorschlag(mandant, lieferant, artikel):
    """(EK, Bestellnummer des Lieferanten): Preisliste, sonst letzter Lieferantenpreis des Mandanten, sonst der jüngste eines
    anderen Mandanten. Der Durchschnitts-EK enthält Nebenkosten und ist ein Notbehelf, danach der manuell eingetragene EK."""
    al = ArtikelLieferant.objects.filter(artikel=artikel, lieferant=lieferant).first()
    if al and al.ek is not None:
        return al.ek, al.bestellnummer
    gekauft = ArtikelMandant.objects.filter(artikel=artikel, letzter_ek__isnull=False)
    am = gekauft.filter(mandant=mandant).first() or gekauft.order_by("-letzter_ek_datum", "-id").first()
    ersatz = artikel.durchschnitts_ek or artikel.ek_manuell or Decimal("0")
    return (am.letzter_ek if am else ersatz), (al.bestellnummer if al else "")


@transaction.atomic
def bestellung_anlegen(mandant, lieferant, bemerkung="", aus_beleg=None, benutzer=None) -> Bestellung:
    return Bestellung.objects.create(mandant=mandant, lieferant=lieferant, nummer=nummer(mandant, "BE"),
                                     bemerkung=bemerkung, aus_beleg=aus_beleg,
                                     angelegt_von=benutzer if benutzer is not None and benutzer.is_authenticated else None)


def bestellposition(bestellung, artikel, menge, ek=None, preisliste_artikel=None) -> BestellPosition:
    """Position zur Bestellung: eigener Artikel oder Artikel aus der Preisliste des Lieferanten (wird beim Wareneingang
    zum eigenen Artikel)."""
    if bestellung.status not in (BestellStatus.ENTWURF, BestellStatus.BESTELLT, BestellStatus.TEIL):
        raise ValueError("Zu dieser Bestellung können keine Positionen mehr erfasst werden.")
    ek = ek_runden(ek)
    if artikel is None and preisliste_artikel is not None and preisliste_artikel.artikel_id:
        artikel = preisliste_artikel.artikel  # zu dem Eintrag gibt es schon einen eigenen Artikel
    if artikel is not None:
        keine_leistung(artikel)
    if artikel is None:
        if preisliste_artikel is None:
            raise ValueError("Artikel fehlt.")
        if preisliste_artikel.lieferant_id != bestellung.lieferant_id:
            raise ValueError(f"{preisliste_artikel.nummer} gehört zur Preisliste von {preisliste_artikel.lieferant.name1}, "
                             f"nicht zu {bestellung.lieferant.name1}.")
        pos = bestellung.positionen.filter(preisliste_artikel=preisliste_artikel, artikel__isnull=True, geliefert=0).first()
        if pos and (ek is None or pos.ek == ek):
            pos.menge += menge
            pos.save()
            return pos
        return BestellPosition.objects.create(
            bestellung=bestellung, preisliste_artikel=preisliste_artikel, bestellnummer=preisliste_artikel.bestellnummer,
            bezeichnung=preisliste_artikel.bezeichnung[:255], menge=menge, ek=ek if ek is not None else ek_runden(preisliste_artikel.ek))
    vorschlag, bestellnr = ek_vorschlag(bestellung.mandant, bestellung.lieferant, artikel)
    pos = bestellung.positionen.filter(artikel=artikel, geliefert=0).first()
    if pos and (ek is None or pos.ek == ek):  # gleicher Artikel zum gleichen Preis: Menge addieren, sonst eigene Zeile
        pos.menge += menge
        pos.save()
        return pos
    return BestellPosition.objects.create(bestellung=bestellung, artikel=artikel, bestellnummer=bestellnr,
                                          bezeichnung=artikel.bezeichnung, menge=menge, ek=ek if ek is not None else ek_runden(vorschlag))


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


# ---- Lieferantenbestellung aus Angebot/Kundenbestellung (Issue 61) ------------------------------
# Bestellt gilt eine Menge erst ab Status „bestellt“ (Entwürfe und Stornos zählen nicht). Lieferantenbestellungen aus dem
# Angebot zählen auch für die daraus kopierte Kundenbestellung. Die Rückfrage gibt es nur bei Kundenbestellungen.
ZAEHLT_ALS_BESTELLT = (BestellStatus.BESTELLT, BestellStatus.TEIL, BestellStatus.GELIEFERT)
MODUS_FEHLEND, MODUS_KOMPLETT = "fehlend", "komplett"


def quellbelege(beleg) -> list:
    """Der Beleg und seine Vorgänger (Angebot/Kundenbestellung), aus denen schon beim Lieferanten bestellt sein kann."""
    ids, b = [], beleg
    while b is not None and b.art in (Belegart.ANGEBOT, Belegart.AUFTRAG) and b.pk not in ids and len(ids) < 10:
        ids.append(b.pk)
        b = b.vorgaenger
    return ids


def lieferantenbestellungen(beleg):
    """Alle Lieferantenbestellungen aus diesem Beleg und seinen Vorgängern (Historie, auch Entwürfe und Stornos)."""
    return (Bestellung.objects.filter(mandant=beleg.mandant, aus_beleg_id__in=quellbelege(beleg))
            .select_related("lieferant", "aus_beleg", "angelegt_von").order_by("angelegt_am", "id"))


def bestellbare_positionen(beleg) -> list:
    """Positionen, die beim Lieferanten bestellt werden können: Lagerartikel (Typ A) und Preislistenartikel (Typ P)."""
    return [p for p in beleg.positionen.select_related("artikel", "preisliste_artikel__lieferant")
            if (p.preisliste_artikel_id and not p.artikel_id) or (p.artikel_id and p.artikel.typ == "A")]


def _schluessel(artikel_id, preisliste_artikel):
    if artikel_id:
        return "a", artikel_id
    if preisliste_artikel is not None and preisliste_artikel.artikel_id:  # inzwischen eigener Artikel
        return "a", preisliste_artikel.artikel_id
    return "p", preisliste_artikel.pk if preisliste_artikel is not None else None


def bestellt_je_position(beleg, positionen=None) -> dict:
    """{Positions-ID: bestellte Menge}. Zugeordnet über Artikel bzw. Preislistenartikel (auch alte Bestellungen ohne
    Positionsbezug); mehrere Positionen desselben Artikels werden der Reihe nach bedient."""
    positionen = bestellbare_positionen(beleg) if positionen is None else positionen
    vorrat = defaultdict(Decimal)
    for bp in BestellPosition.objects.filter(bestellung__mandant=beleg.mandant, bestellung__aus_beleg_id__in=quellbelege(beleg),
                                             bestellung__status__in=ZAEHLT_ALS_BESTELLT).select_related("preisliste_artikel"):
        vorrat[_schluessel(bp.artikel_id, bp.preisliste_artikel)] += bp.menge
    ergebnis = {}
    for p in positionen:
        k = _schluessel(p.artikel_id, p.preisliste_artikel)
        menge = min(max(vorrat[k], Decimal("0")), p.menge)
        vorrat[k] -= menge
        ergebnis[p.pk] = menge
    return ergebnis


def bestellstand(beleg) -> dict:
    """Für Rückfrage und Anzeige: Positionen mit bestellt/fehlt, vorhandene Lieferantenbestellungen."""
    positionen = bestellbare_positionen(beleg)
    bestellt = bestellt_je_position(beleg, positionen)
    for p in positionen:
        p.bestellt = bestellt[p.pk]
        p.fehlt = p.menge - p.bestellt
    return {"positionen": positionen, "bestellungen": list(lieferantenbestellungen(beleg)),
            "etwas_bestellt": any(p.bestellt > 0 for p in positionen), "etwas_fehlt": any(p.fehlt > 0 for p in positionen)}


def rueckfrage_noetig(beleg) -> bool:
    """Nur Kundenbestellungen: Ist schon etwas beim Lieferanten bestellt, wird vor dem erneuten Kopieren gefragt."""
    return beleg.art == Belegart.AUFTRAG and any(m > 0 for m in bestellt_je_position(beleg).values())


@transaction.atomic
def bestellungen_aus_beleg(beleg, modus=None, grund="", benutzer=None) -> list:
    """Angebot oder Kundenbestellung in Lieferantenbestellungen kopieren: je Lieferant eine Bestellung (günstigster Lieferant).
    Ist bei einer Kundenbestellung schon etwas bestellt, muss modus gewählt sein: „fehlend“ (nur Restmengen) oder
    „komplett“ (alles erneut, mit Grund)."""
    from apps.verkauf.models import Beleg
    Beleg.objects.select_for_update().filter(pk=beleg.pk).first()  # gleichzeitiges Kopieren nacheinander
    grund = (grund or "").strip()
    positionen = bestellbare_positionen(beleg)
    bestellt = bestellt_je_position(beleg, positionen)
    bemerkung = f"Aus {beleg}"
    if beleg.art == Belegart.AUFTRAG and any(m > 0 for m in bestellt.values()):
        if modus == MODUS_FEHLEND:
            bemerkung += " – nur fehlende Mengen"
        elif modus == MODUS_KOMPLETT:
            if not grund:
                raise ValueError("Für eine erneute komplette Bestellung ist ein Grund nötig.")
            bemerkung += f" – erneut komplett bestellt: {grund}"
        else:
            raise ValueError("Aus dieser Kundenbestellung ist schon beim Lieferanten bestellt – bitte wählen, was bestellt werden soll.")
    else:
        modus = MODUS_KOMPLETT
    je_lieferant = defaultdict(list)
    ohne = []
    for p in positionen:
        menge = p.menge - bestellt[p.pk] if modus == MODUS_FEHLEND else p.menge
        if menge <= 0:
            continue
        if p.preisliste_artikel_id and not p.artikel_id:  # Typ P: der Lieferant steht an der Preislistenzeile
            je_lieferant[p.preisliste_artikel.lieferant].append((p, menge))
            continue
        al = guenstigster_lieferant(p.artikel)
        (je_lieferant[al.lieferant] if al else ohne).append((p, menge))
    if modus == MODUS_FEHLEND and not je_lieferant and not ohne:
        raise ValueError("Alle Positionen sind bereits beim Lieferanten bestellt.")
    ergebnis = []
    for lieferant, zeilen_ in je_lieferant.items():
        b = bestellung_anlegen(beleg.mandant, lieferant, bemerkung, aus_beleg=beleg, benutzer=benutzer)
        for p, menge in zeilen_:
            bestellposition(b, p.artikel, menge, preisliste_artikel=p.preisliste_artikel)
        ergebnis.append(b)
    if not ergebnis:
        raise ValueError("Keiner der Artikel hat einen Lieferanten in den Preislisten.")
    beleg._ohne_lieferant = [p for p, _ in ohne]
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
    """Aus einem Preislistenartikel (Typ P) wird ein eigener Artikel (Typ A) mit Nummer aus dem Nummernkreis.
    Nur nach Rückfrage aufrufen (nie still). Gibt es schon einen Artikel mit derselben Herstellernummer, wird dieser verknüpft."""
    eintrag = Preislistenartikel.objects.select_for_update().get(pk=eintrag.pk)  # Doppelklick: zweiter Aufruf sieht den ersten
    if eintrag.artikel_id:
        return eintrag.artikel
    from apps.stammdaten.models import Hersteller, Stammnummer, Steuersatz
    herstellernr = (eintrag.herstellernummer or "").strip()
    artikel = None
    if herstellernr:  # nur verknüpfen, wenn auch Hersteller oder EAN passen (Herstellernummern sind nicht eindeutig)
        for kandidat in Artikel.objects.filter(herstellernummer__iexact=herstellernr).select_related("hersteller").order_by("pk"):
            hersteller_gleich = bool(eintrag.hersteller and kandidat.hersteller_id
                                     and kandidat.hersteller.name.strip().lower() == eintrag.hersteller.strip().lower())
            ean_gleich = bool(eintrag.ean and kandidat.ean and eintrag.ean == kandidat.ean)
            if hersteller_gleich or ean_gleich:
                artikel = kandidat
                break
    if artikel is None:
        nummer = Stammnummer.naechste("A")
        steuer = Steuersatz.objects.filter(nummer=1).first() or Steuersatz.objects.order_by("-prozent").first()
        if steuer is None:
            raise ValueError("Es ist kein Steuersatz angelegt.")
        hersteller = Hersteller.objects.filter(name__iexact=eintrag.hersteller).first() if eintrag.hersteller else None
        if eintrag.hersteller and hersteller is None:
            hersteller = Hersteller.objects.create(name=eintrag.hersteller[:150])
        artikel = Artikel.objects.create(
            nummer=nummer, typ="A", bezeichnung=eintrag.bezeichnung, herstellernummer=eintrag.herstellernummer, ean=eintrag.ean,
            hersteller=hersteller, gewicht_kg=eintrag.gewicht_kg,
            laenge_cm=eintrag.laenge_cm, breite_cm=eintrag.breite_cm, hoehe_cm=eintrag.hoehe_cm, steuersatz=steuer)
    eintrag.artikel = artikel
    eintrag.save(update_fields=["artikel"])
    ArtikelLieferant.objects.get_or_create(
        lieferant=eintrag.lieferant, bestellnummer=eintrag.bestellnummer,
        defaults={"artikel": artikel, "ek": eintrag.ek, "bestand": eintrag.bestand, "lieferzeit_tage": eintrag.lieferzeit_tage,
                  "preisliste": eintrag.preisliste, "aktualisiert_am": eintrag.aktualisiert_am})
    BestellPosition.objects.filter(preisliste_artikel=eintrag, artikel__isnull=True).update(artikel=artikel)
    umstellen = Position.objects.filter(preisliste_artikel=eintrag, artikel__isnull=True).exclude(
        beleg__art__in=("RE", "GS", "SP"), beleg__abgeschlossen=True)  # Verkaufspositionen (außer abgeschlossene Rechnungen, Gutschriften, Proforma-Stornos) werden zu A
    umstellen.update(artikel=artikel, typ="A", preisliste_artikel=None)
    return artikel


def preislisten_treffer(code: str, lieferant):
    """Preislistenartikel des Lieferanten ohne eigenen Artikel zu diesem Code (Bestell-, Hersteller- oder EAN-Nummer)."""
    code = (code or "").strip()
    if not code or lieferant is None:
        return None
    return Preislistenartikel.objects.filter(lieferant=lieferant, artikel__isnull=True).filter(
        Q(bestellnummer__iexact=code) | Q(herstellernummer__iexact=code) | Q(ean=code)).first()


def artikel_per_code(code: str, lieferant=None):
    """Artikel über Scan-Code: EAN, Artikelnummer, Herstellernummer oder Bestellnummer des Lieferanten.
    Legt nie einen Artikel an; Preislistenartikel ohne eigenen Artikel liefert preislisten_treffer()."""
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
    if lieferant is not None:  # Preislistenartikel, der schon einen eigenen Artikel hat
        eintrag = Preislistenartikel.objects.filter(lieferant=lieferant, artikel__isnull=False).filter(
            Q(bestellnummer__iexact=code) | Q(herstellernummer__iexact=code) | Q(ean=code)).select_related("artikel").first()
        if eintrag:
            return eintrag.artikel
    return None


def offene_p_positionen(we):
    """Offene Preislistenpositionen (Typ P) der Bestellung ohne eigenen Artikel, die noch nicht übernommen sind."""
    if not we.bestellung_id:
        return []
    return list(we.bestellung.positionen.filter(artikel__isnull=True, preisliste_artikel__isnull=False)
                .select_related("preisliste_artikel", "bestellung__lieferant"))


@transaction.atomic
def wareneingang_erfassen(we, artikel, menge=Decimal("1"), ek=None, seriennummern="") -> WareneingangPosition:
    """Zeile zum Wareneingang; derselbe Artikel (ohne Seriennummern) erhöht die vorhandene Zeile (Scan +1)."""
    if we.gebucht:
        raise ValueError("Der Wareneingang ist bereits gebucht.")
    keine_leistung(artikel)
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
                                               ek=ek_runden(ek), seriennummern=seriennummern)


def bleibt_offen(we):
    """Teillieferung: Bestellpositionen, die nach diesem Wareneingang noch offen sind (Rest bleibt bestellt).
    Vor der Buchung wird die Menge dieses Wareneingangs abgezogen, danach steht sie schon in „geliefert“."""
    if not we.bestellung_id:
        return []
    im_we = {}
    if not we.gebucht:
        for p in we.positionen.filter(bestellposition__isnull=False):
            im_we[p.bestellposition_id] = im_we.get(p.bestellposition_id, Decimal("0")) + p.menge
    rest = []
    for bp in we.bestellung.positionen.select_related("artikel"):
        if bp.artikel_id and bp.artikel.typ == "L":
            continue
        diese = im_we.get(bp.pk, Decimal("0"))
        if bp.offen - diese > 0:
            rest.append({"bp": bp, "diese": diese, "rest": bp.offen - diese, "fehlt_ganz": not we.gebucht and diese == 0})
    return rest


def wareneingang_aus_bestellung(we, nur=None, p_anlegen=False, nur_p=False):
    """Offene Mengen der Bestellung als Zeilen vorbelegen (nur=Bestellposition: nur diese wieder aufnehmen;
    nur_p: nur offene Preislistenpositionen ohne Artikel, entfernte A-Positionen bleiben draußen).
    Preislistenpositionen (P) ohne Artikel werden nur mit p_anlegen=True (nach Rückfrage) zu eigenen Artikeln, sonst bleiben sie offen."""
    if we.gebucht:
        raise ValueError("Der Wareneingang ist bereits gebucht.")
    positionen = we.bestellung.positionen.select_related("preisliste_artikel")
    if nur is not None:
        positionen = positionen.filter(pk=nur.pk)
    if nur_p:
        positionen = positionen.filter(artikel__isnull=True, preisliste_artikel__isnull=False)
    # Mengen, die schon in einem anderen, noch nicht gebuchten Wareneingang dieser Bestellung stehen, nicht doppelt vorbelegen
    in_anderen = {}
    for bp_id, menge in WareneingangPosition.objects.filter(
            wareneingang__bestellung=we.bestellung, wareneingang__gebucht=False, bestellposition__isnull=False
    ).exclude(wareneingang=we).values_list("bestellposition_id", "menge"):
        in_anderen[bp_id] = in_anderen.get(bp_id, Decimal("0")) + menge
    for bp in positionen:
        if bp.artikel_id is None and bp.preisliste_artikel_id:
            if not p_anlegen:  # nie still anlegen: bleibt in der Bestellung offen
                continue
            bp.artikel = artikel_aus_preisliste(bp.preisliste_artikel)
            bp.save(update_fields=["artikel"])
        if bp.artikel_id is None:
            continue
        if bp.artikel.typ == "L":  # Leistungen kommen nicht ins Lager
            continue
        menge = bp.offen - in_anderen.get(bp.pk, Decimal("0"))
        if menge > 0 and not we.positionen.filter(bestellposition=bp).exists():
            WareneingangPosition.objects.create(wareneingang=we, bestellposition=bp, artikel=bp.artikel, menge=menge, ek=bp.ek)


class PlausibilitaetFehler(ValueError):
    """Wareneingangspreis weicht stark vom Ø-EK bzw. Import-EK ab: Rückfrage, gebucht wird erst nach Bestätigung."""


def mengen_rueckfragen(we, positionen=None):
    """Mehr erfasst als in der Bestellung noch offen ist (z. B. schon mit einem anderen Wareneingang gebucht): Rückfrage."""
    positionen = list(we.positionen.all()) if positionen is None else positionen
    je_bp = {}
    for p in positionen:
        if p.bestellposition_id:
            je_bp[p.bestellposition_id] = je_bp.get(p.bestellposition_id, Decimal("0")) + p.menge
    texte = []
    for bp in BestellPosition.objects.filter(pk__in=je_bp):  # frisch: ein anderer Wareneingang kann schon gebucht sein
        if je_bp[bp.pk] > bp.offen:
            offen = max(bp.offen, Decimal("0")).normalize()  # außerhalb des f-Strings: Python 3.10 auf dem Server
            erfasst = je_bp[bp.pk].normalize()
            texte.append(f"{bp.bezeichnung[:60]}: in der Bestellung nur noch {offen:f} offen, erfasst {erfasst:f} – "
                         "schon mit einem anderen Wareneingang gebucht? Sonst Überlieferung")
    return texte


def preis_rueckfragen(we, positionen=None):
    """Rückfragetexte je Position, deren Kalkulations-EK um mehr als die Grenze (50 %) vom Ø-EK (nur mit Bestand) bzw. Import-EK abweicht."""
    positionen = list(we.positionen.select_related("artikel")) if positionen is None else positionen
    kalk = we.kalkulation()
    texte = []
    for p in positionen:
        if p.artikel.typ not in ("A", "P"):
            continue
        am = ArtikelMandant.objects.filter(artikel=p.artikel, mandant=we.mandant).first()
        ek_avg = am.ek_avg if am and bestand(we.mandant, p.artikel) > 0 else None
        text = plausibilitaet_we(kalk[p.pk], ek_avg, import_ek_von(p.artikel))
        if text:
            texte.append(f"{p.artikel.nummer}: {text}")
    return texte


@transaction.atomic
def wareneingang_buchen(we, benutzer=None, bestaetigt=False) -> int:
    """Bucht Bestand, letzten EK, Durchschnitts-EK, Seriennummern und Liefermengen der Bestellung.
    Weicht ein Preis stark vom Ø-EK/Import-EK ab, wird ohne bestaetigt=True nichts gebucht (PlausibilitaetFehler)."""
    we = Wareneingang.objects.select_for_update().get(pk=we.pk)  # Sperre: Doppelklick/zweiter Tab darf nicht doppelt buchen
    if we.gebucht:
        raise ValueError("Der Wareneingang ist bereits gebucht.")
    positionen = list(we.positionen.select_related("artikel", "bestellposition"))
    if not positionen:
        raise ValueError("Keine Positionen erfasst.")
    for p in positionen:  # vorab prüfen, damit nichts halb gebucht wird
        keine_leistung(p.artikel)
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
    if not bestaetigt:
        rueckfragen = mengen_rueckfragen(we, positionen) + preis_rueckfragen(we, positionen)
        if rueckfragen:
            raise PlausibilitaetFehler(" | ".join(rueckfragen))
    heute = we.datum
    kalkulation = we.kalkulation()
    for p in positionen:
        p.kalk_ek = kalkulation[p.pk]  # eigentlicher EK: inkl. anteiliger Versand-, Versicherungs- und Verpackungskosten
        p.save(update_fields=["kalk_ek"])
        a = Artikel.objects.select_for_update().get(pk=p.artikel_id)  # frisch laden: mehrere Zeilen desselben Artikels
        wareneingang_ek(we.mandant, a, bestand(we.mandant, a), p.menge, p.kalk_ek)  # Ø-EK vor der Buchung, mit dem Bestand davor
        Lagerbewegung.objects.create(mandant=we.mandant, artikel=a, art=Bewegungsart.WARENEINGANG, menge=p.menge,
                                     bemerkung=f"{we.nummer} {we.lieferant.name1}", benutzer=benutzer)
        am, _ = ArtikelMandant.objects.get_or_create(artikel=a, mandant=we.mandant)
        am.letzter_ek, am.letzter_ek_datum = p.ek, heute  # Lieferantenpreis: Vorschlag für die nächste Bestellung, ohne Nebenkosten
        am.save()
        # Der EK des Artikels bleibt unberührt: maßgebend ist der Import-EK (README 18), der Kalkulations-EK steht an der Position
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


def rechnung_loeschen(rechnung, notiz, benutzer=None) -> list:
    """Offene Rechnung löschen, die Zuordnung der Wareneingänge entfällt damit. Notiz und Benutzer stehen vor dem Löschen in
    der Historie und bleiben so im Änderungsprotokoll. Gibt die Nummern der frei gewordenen Wareneingänge zurück."""
    notiz = (notiz or "").strip()
    if not notiz:
        raise ValueError("Bitte eine Notiz angeben, warum die Rechnung gelöscht wird.")
    with transaction.atomic():
        r = Lieferantenrechnung.objects.select_for_update().get(pk=rechnung.pk)  # der Status kann sich in einem anderen Tab geändert haben
        if r.status == RechnungStatus.BEZAHLT:
            raise ValueError("Bezahlte Rechnungen lassen sich nicht löschen – erst die Zahlung stornieren.")
        if r.status == RechnungStatus.EXPORTIERT:
            raise ValueError("Die Rechnung wurde schon zur Zahlung exportiert (SEPA-Datei) und lässt sich nicht löschen. Bitte erst mit der Bank klären.")
        nummern = list(r.wareneingaenge.values_list("nummer", flat=True))
        r.historie_eintrag(f"Rechnung gelöscht – {notiz}" + (f" (Zuordnung aufgehoben: {', '.join(nummern)})" if nummern else ""), benutzer)
        r.save(update_fields=["historie"])
        r.delete()
    return nummern


def rma_anlegen(mandant, lieferant, bezeichnung, fehler, artikel=None, seriennummer="", reparatur=None) -> Rma:
    rma = Rma.objects.create(mandant=mandant, lieferant=lieferant, nummer=nummer(mandant, "RM"), bezeichnung=bezeichnung,
                             fehler=fehler, artikel=artikel, seriennummer=seriennummer, reparatur=reparatur)
    if reparatur is not None:
        from apps.werkstatt.models import Status
        reparatur.status = Status.RMA
        reparatur.save()
    return rma
