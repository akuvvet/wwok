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

from .models import (RUECKSENDUNG_OFFEN, BestellPosition, BestellStatus, Bestellung, Lieferantenrechnung, Rma, RmaStatus,
                     RechnungStatus, Ruecksendung, RuecksendungArt, RuecksendungPosition, RuecksendungStatus, Wareneingang,
                     WareneingangPosition)


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
    if lieferant.gesperrt:  # wie in der Oberfläche: bei gesperrten Lieferanten entstehen keine neuen Belege
        raise ValueError(f"Lieferant {lieferant.name1} ist gesperrt – es lässt sich keine Bestellung anlegen.")
    return Bestellung.objects.create(mandant=mandant, lieferant=lieferant, nummer=nummer(mandant, "BE"),
                                     bemerkung=bemerkung, aus_beleg=aus_beleg,
                                     angelegt_von=benutzer if benutzer is not None and benutzer.is_authenticated else None)


def bestellposition(bestellung, artikel, menge, ek=None, preisliste_artikel=None) -> BestellPosition:
    """Position zur Bestellung: eigener Artikel oder Artikel aus der Preisliste des Lieferanten (wird beim Wareneingang
    zum eigenen Artikel)."""
    if bestellung.status not in (BestellStatus.ENTWURF, BestellStatus.BESTELLT, BestellStatus.TEIL):
        raise ValueError("Zu dieser Bestellung können keine Positionen mehr erfasst werden.")
    if menge is None or not menge.is_finite() or not Decimal("0") < menge < Decimal("10000000"):
        raise ValueError("Die Menge muss größer als 0 sein.")
    if ek is not None and (not ek.is_finite() or not Decimal("0") <= ek < Decimal("100000000")):
        raise ValueError("Der Preis darf nicht negativ sein.")
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


def bezeichnung_original(position) -> str:
    """Bezeichnung laut Artikel bzw. Preisliste (Kunden- oder Bestellposition); leer bei freier Position."""
    if position.artikel_id:
        return position.artikel.bezeichnung[:255]
    return position.preisliste_artikel.bezeichnung[:255] if position.preisliste_artikel_id else ""


def bezeichnung_setzen(position, text):
    """Vom Benutzer eingegebene Bezeichnung einer Bestellposition; leer = Bezeichnung des Artikels."""
    text = (" ".join((text or "").split()) or bezeichnung_original(position))[:255]
    if text and text != position.bezeichnung:
        position.bezeichnung = text
        position.save(update_fields=["bezeichnung"])


def bezeichnung_uebernehmen(neu, quelle):
    """Hat der Benutzer die Bezeichnung der Kundenposition geändert, gilt sie auch in der Lieferantenbestellung."""
    if quelle.bezeichnung and quelle.bezeichnung != bezeichnung_original(quelle):
        bezeichnung_setzen(neu, quelle.bezeichnung)


def bestellen_pruefen(bestellung):
    """Vor dem Bestellen (und vor dem Versand der Bestellmail): Positionen vorhanden, Bestellung nicht storniert."""
    if bestellung.status == BestellStatus.STORNIERT:
        raise ValueError("Die Bestellung ist storniert.")
    if not bestellung.positionen.exists():
        raise ValueError("Bestellung ohne Positionen.")


def bestellen(bestellung):
    bestellen_pruefen(bestellung)
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
    if preisliste_artikel is not None and preisliste_artikel.herstellernummer:
        # derselbe Artikel bei einem anderen Lieferanten (Lieferantenwahl) zählt für die Position ebenfalls als bestellt
        return "p", "h:" + preisliste_artikel.herstellernummer.lower()
    return "p", preisliste_artikel.pk if preisliste_artikel is not None else None


def bestellt_je_position(beleg, positionen=None) -> dict:
    """{Positions-ID: bestellte Menge}. Zugeordnet über Artikel bzw. Preislistenartikel (auch alte Bestellungen ohne
    Positionsbezug); mehrere Positionen desselben Artikels werden der Reihe nach bedient."""
    positionen = bestellbare_positionen(beleg) if positionen is None else positionen
    vorrat = defaultdict(Decimal)
    # Teilübernahme: Die Kundenbestellung zeigt nur noch den Rest. Was nach dem Bestellen beim Lieferanten in Lieferschein/Rechnung
    # wanderte, hat die Lieferantenmenge zuerst verbraucht und darf den Rest nicht als „bestellt“ erscheinen lassen.
    from apps.verkauf.models import BelegUebernahme
    uebernahmen = list(BelegUebernahme.objects.filter(quelle_id__in=quellbelege(beleg), artikel__isnull=False))
    bps = list(BestellPosition.objects.filter(bestellung__mandant=beleg.mandant, bestellung__aus_beleg_id__in=quellbelege(beleg),
                                              bestellung__status__in=ZAEHLT_ALS_BESTELLT)
               .select_related("preisliste_artikel", "bestellung"))
    # Laufender Topf je Artikel in zeitlicher Reihenfolge: Bestellungen füllen ihn, jede Übernahme verbraucht ihn genau einmal
    # (nicht je Lieferantenposition); eine Übernahme vor der Bestellung findet noch nichts vor und verbraucht nichts.
    ereignisse = defaultdict(list)  # Artikel-ID -> [(Zeitpunkt, Rang, Menge)]; Rang 0 = Bestellung, 1 = Übernahme
    for bp in bps:
        if bp.artikel_id:
            ereignisse[bp.artikel_id].append((bp.bestellung.angelegt_am, 0, bp.menge))
        else:
            vorrat[_schluessel(bp.artikel_id, bp.preisliste_artikel)] += bp.menge
    for e in uebernahmen:
        if e.artikel_id in ereignisse:
            ereignisse[e.artikel_id].append((e.am, 1, -(e.menge * e.vorzeichen)))
    for artikel_id, liste in ereignisse.items():
        topf = Decimal("0")
        for _zeit, _rang, menge in sorted(liste, key=lambda x: (x[0], x[1])):
            topf = max(topf + menge, Decimal("0"))
        vorrat[("a", artikel_id)] += topf
    ergebnis = {}
    for p in positionen:
        ergebnis[p.pk] = Decimal("0")
        for k in _schluessel_liste(p):
            menge = min(max(vorrat[k], Decimal("0")), p.menge - ergebnis[p.pk])
            vorrat[k] -= menge
            ergebnis[p.pk] += menge
    return ergebnis


def _gleicher_hersteller(a, b) -> bool:
    """Gleiche Herstellernummer zählt nur als derselbe Artikel, wenn die Hersteller nicht ausdrücklich verschieden sind."""
    return not a.hersteller or not b.hersteller or a.hersteller.strip().lower() == b.hersteller.strip().lower()


def _schluessel_liste(position) -> list:
    """Schlüssel, unter denen eine Bestellung für die Position zählt. Typ P: auch dieselbe Herstellernummer bei einem anderen
    Lieferanten (Lieferantenwahl), egal ob dessen Preislistenzeile schon einen eigenen Artikel hat oder nicht."""
    eintrag = position.preisliste_artikel
    liste = [_schluessel(position.artikel_id, eintrag)]
    if eintrag is not None and not position.artikel_id and eintrag.herstellernummer:
        liste.append(("p", "h:" + eintrag.herstellernummer.lower()))
        for e in Preislistenartikel.objects.filter(herstellernummer__iexact=eintrag.herstellernummer, artikel__isnull=False):
            if _gleicher_hersteller(eintrag, e):
                liste.append(("a", e.artikel_id))
    return list(dict.fromkeys(liste))


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


def _zu_bestellen(beleg, modus=None, grund=""):
    """([(Position, Menge)], Bemerkung, Modus): was aus dem Beleg jetzt beim Lieferanten zu bestellen ist. Ist bei einer
    Kundenbestellung schon etwas bestellt, muss modus gewählt sein: „fehlend“ (nur Restmengen) oder „komplett“ (mit Grund)."""
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
    zeilen_ = [(p, p.menge - bestellt[p.pk] if modus == MODUS_FEHLEND else p.menge) for p in positionen]
    zeilen_ = [(p, menge) for p, menge in zeilen_ if menge > 0]
    if modus == MODUS_FEHLEND and not zeilen_:
        raise ValueError("Alle Positionen sind bereits beim Lieferanten bestellt.")
    return zeilen_, bemerkung, modus


# ---- Lieferantenwahl mit Preisvergleich ---------------------------------------------------------
# Hat der Kundenbeleg mehrere Positionen und führt mehr als ein Lieferant mindestens eine davon, wird vor dem Anlegen gefragt,
# bei wem bestellt wird. Verglichen wird der Warenwert (EK × Menge) aus den Preislisten; Versandkosten sind nicht bekannt.

def _cent(betrag):
    return betrag.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _lieferbar(angebot) -> bool:
    return angebot["bestand"] is None or angebot["bestand"] > 0


def _rang(angebot):
    """Reihenfolge der Angebote einer Position: lieferbar vor Bestand 0, dann der Preis; bei Gleichstand der Lieferant der
    Angebotszeile (Typ P), dann der Name."""
    return (not _lieferbar(angebot), angebot["ek"], not angebot["eigen"], angebot["lieferant"].kurz.lower(), angebot["lieferant"].pk)


def angebote_je_lieferant(position, menge) -> dict:
    """{Lieferanten-ID: Angebot} für eine Position des Kundenbelegs. Lagerartikel: die Preislistenzeilen am Artikel.
    Preislistenartikel (Typ P): die Zeile selbst und dieselbe Herstellernummer bei anderen Lieferanten. Je Lieferant zählt
    die günstigste Zeile; gesperrte Lieferanten fehlen."""
    ergebnis = {}

    def merken(lieferant, ek, bestand, eintrag=None, eigen=False):
        ek = ek_runden(ek)
        alt = ergebnis.get(lieferant.pk)
        if alt is None or (not alt["eigen"] and (eigen or ek < alt["ek"])):
            ergebnis[lieferant.pk] = {"lieferant": lieferant, "ek": ek, "bestand": bestand, "eintrag": eintrag, "eigen": eigen,
                                      "summe": _cent(ek * menge), "knapp": bestand is not None and bestand < menge}

    if position.preisliste_artikel_id and not position.artikel_id:
        eigener = position.preisliste_artikel
        if not eigener.lieferant.gesperrt:
            merken(eigener.lieferant, eigener.ek, eigener.bestand, eigener, eigen=True)
        if eigener.herstellernummer:
            for e in (Preislistenartikel.objects.filter(herstellernummer__iexact=eigener.herstellernummer, lieferant__gesperrt=False)
                      .exclude(pk=eigener.pk).select_related("lieferant").order_by("ek", "pk")):
                if _gleicher_hersteller(eigener, e):
                    merken(e.lieferant, e.ek, e.bestand, e)
    else:
        for al in (ArtikelLieferant.objects.filter(artikel=position.artikel, ek__isnull=False, lieferant__gesperrt=False)
                   .select_related("lieferant").order_by("ek", "pk")):
            merken(al.lieferant, al.ek, al.bestand)
    return ergebnis


def lieferantenvergleich(beleg, modus=None, grund="") -> dict:
    """Preisvergleich für „In Lieferantenbestellung kopieren“: je Position die Angebote aller Lieferanten und je Lieferant,
    was es kostet, alles dort zu bestellen (was er nicht führt, kommt vom jeweils günstigsten anderen)."""
    positionen, _bemerkung, modus = _zu_bestellen(beleg, modus, grund)
    zeilen_ = []
    for p, menge in positionen:
        angebote = angebote_je_lieferant(p, menge)
        zeilen_.append({"position": p, "menge": menge, "angebote": angebote,
                        "bester": min(angebote.values(), key=_rang) if angebote else None})
    mit_angebot = [z for z in zeilen_ if z["bester"]]
    guenstigste = sum((z["bester"]["summe"] for z in mit_angebot), Decimal("0"))
    lieferanten = {}
    for z in mit_angebot:
        for a in z["angebote"].values():
            lieferanten[a["lieferant"].pk] = a["lieferant"]
    spalten = []
    for lf in lieferanten.values():
        eigene = [z["angebote"][lf.pk] for z in mit_angebot if lf.pk in z["angebote"]]
        gesamt = sum((z["angebote"].get(lf.pk, z["bester"])["summe"] for z in mit_angebot), Decimal("0"))
        spalten.append({"lieferant": lf, "anzahl": len(eigene), "fehlt": len(mit_angebot) - len(eigene),
                        "summe": sum((a["summe"] for a in eigene), Decimal("0")), "gesamt": gesamt, "mehr": gesamt - guenstigste,
                        "knapp": any(a["knapp"] for a in eigene)})
    spalten.sort(key=lambda s: (s["fehlt"], s["gesamt"], s["lieferant"].kurz.lower(), s["lieferant"].pk))
    for z in zeilen_:
        z["zellen"] = [z["angebote"].get(s["lieferant"].pk) for s in spalten]
    aufteilung = {}  # günstigste Aufteilung: Lieferant -> Anzahl Positionen
    for z in mit_angebot:
        name = z["bester"]["lieferant"].kurz
        aufteilung[name] = aufteilung.get(name, 0) + 1
    return {"zeilen": zeilen_, "spalten": spalten, "guenstigste": guenstigste, "aufteilung": sorted(aufteilung.items()),
            "modus": modus, "wahl_noetig": len(zeilen_) >= 2 and any(len(z["angebote"]) >= 2 for z in zeilen_)}


@transaction.atomic
def bestellungen_aus_beleg(beleg, modus=None, grund="", benutzer=None, wahl=None) -> list:
    """Angebot oder Kundenbestellung in Lieferantenbestellungen kopieren: je Lieferant eine Bestellung. Ohne wahl bekommt jede
    Position den günstigsten Lieferanten (Typ P: den der Preislistenzeile); mit wahl ({Positions-ID: Lieferanten-ID}, aus der
    Lieferantenwahl) den gewählten. Ist bei einer Kundenbestellung schon etwas bestellt, muss modus gewählt sein: „fehlend“
    (nur Restmengen) oder „komplett“ (alles erneut, mit Grund)."""
    from apps.verkauf.models import Beleg
    Beleg.objects.select_for_update().filter(pk=beleg.pk).first()  # gleichzeitiges Kopieren nacheinander
    positionen, bemerkung, modus = _zu_bestellen(beleg, modus, grund)
    wahl = wahl or {}
    je_lieferant = defaultdict(list)
    ohne = []
    for p, menge in positionen:
        if p.pk in wahl:
            angebot = angebote_je_lieferant(p, menge).get(wahl[p.pk])
            if angebot is None:
                raise ValueError(f"„{p.bezeichnung}“ führt der gewählte Lieferant nicht (mehr) – bitte die Lieferanten neu wählen.")
            je_lieferant[angebot["lieferant"]].append((p, menge, angebot))
            continue
        if p.preisliste_artikel_id and not p.artikel_id:  # Typ P: der Lieferant steht an der Preislistenzeile
            je_lieferant[p.preisliste_artikel.lieferant].append((p, menge, None))
            continue
        al = guenstigster_lieferant(p.artikel)
        if al:
            je_lieferant[al.lieferant].append((p, menge, None))
        else:
            ohne.append((p, menge))
    ergebnis = []
    for lieferant, zeilen_ in je_lieferant.items():
        b = bestellung_anlegen(beleg.mandant, lieferant, bemerkung, aus_beleg=beleg, benutzer=benutzer)
        for p, menge, angebot in zeilen_:
            if angebot is None:
                neu = bestellposition(b, p.artikel, menge, preisliste_artikel=p.preisliste_artikel)
            elif angebot["eintrag"] is not None:  # Typ P: die Preislistenzeile des gewählten Lieferanten
                neu = bestellposition(b, None, menge, preisliste_artikel=angebot["eintrag"])
            else:
                neu = bestellposition(b, p.artikel, menge, ek=angebot["ek"])
            bezeichnung_uebernehmen(neu, p)
        ergebnis.append(b)
    if not ergebnis:
        raise ValueError("Keiner der Artikel hat einen Lieferanten in den Preislisten.")
    beleg._ohne_lieferant = [p for p, _ in ohne]
    return ergebnis


@transaction.atomic
def rest_schliessen(bestellung) -> list:
    """Teilgelieferte Bestellung abschließen, weil der Rest nicht mehr kommt: Die bestellten Mengen werden auf das Gelieferte
    gekürzt, die Bestellung gilt als vollständig geliefert. Was wegfällt, steht danach in der Bemerkung."""
    b = Bestellung.objects.select_for_update().get(pk=bestellung.pk)
    if b.status != BestellStatus.TEIL:
        raise ValueError("Nur eine teilweise gelieferte Bestellung lässt sich so abschließen.")
    if b.wareneingaenge.filter(gebucht=False).exists():
        raise ValueError("Zur Bestellung gibt es einen offenen Wareneingang – bitte erst buchen oder löschen.")
    weg = []
    for p in b.positionen.select_for_update():
        if p.offen > 0:
            weg.append(f"{p.offen.normalize():f} × {p.nummer or p.bezeichnung}")
            p.menge = p.geliefert
            p.save(update_fields=["menge"])
    zeile = f"Rest nicht mehr lieferbar ({timezone.localdate():%d.%m.%Y}): " + ", ".join(weg)
    b.bemerkung = f"{b.bemerkung}\n{zeile}" if b.bemerkung else zeile
    b.save(update_fields=["bemerkung"])
    b.status_aktualisieren()
    bestellung.status, bestellung.bemerkung = b.status, b.bemerkung
    return weg


def offen_bestellt(mandant, artikel, mit_entwurf=False) -> Decimal:
    """Noch erwartete Menge (bestellt/teilweise geliefert). Mit mit_entwurf zählen auch nicht abgeschlossene Bestellungen mit,
    so legt ein zweiter Klick auf den Bestellvorschlag nichts doppelt an. Stornierte und gelieferte zählen nie."""
    status = [BestellStatus.BESTELLT, BestellStatus.TEIL] + ([BestellStatus.ENTWURF] if mit_entwurf else [])
    return BestellPosition.objects.filter(
        bestellung__mandant=mandant, artikel=artikel, bestellung__status__in=status).aggregate(
        s=Sum(models.F("menge") - models.F("geliefert")))["s"] or Decimal("0")


def kundenbedarf(mandant) -> dict:
    """{artikel_id: [Beleg, …]} – Artikel mit offener Menge in Kundenbestellungen (und Proforma), auch ohne Haken „R“."""
    from apps.lager.models import reservierende_positionen
    from apps.verkauf.models import mit_offener_menge
    bedarf = defaultdict(list)
    for p in mit_offener_menge(reservierende_positionen(mandant, mit_wartenden=True).filter(artikel__isnull=False)) \
            .filter(offen__gt=0).select_related("beleg__kunde").order_by("beleg__belegdatum", "beleg_id"):
        if p.beleg not in bedarf[p.artikel_id]:
            bedarf[p.artikel_id].append(p.beleg)
    return bedarf


def bestellvorschlag(mandant) -> list:
    """Artikel unter Mindestbestand (verfügbar + bereits bestellt). Ohne festen Mindestbestand: ein Monat Reichweite
    aus dem Verkaufsverlauf der letzten 90 Tage. Dazu kommt immer die Fehlmenge aus offenen Kundenbestellungen
    (Wunsch des Nutzers 18.11.2026): 4 bestellt, 2 am Lager, nichts unterwegs → 2 vorschlagen, auch ohne Mindestbestand."""
    seit = timezone.localdate() - timedelta(days=90)
    verkauft = {s["artikel"]: -s["m"] for s in Lagerbewegung.objects.filter(
        mandant=mandant, art=Bewegungsart.VERKAUF, zeitpunkt__date__gte=seit).values("artikel").annotate(m=Sum("menge"))}
    fest = {d.artikel_id: d.mindestbestand for d in ArtikelMandant.objects.filter(mandant=mandant, mindestbestand__isnull=False)}
    bedarf = kundenbedarf(mandant)
    vorschlag = []
    for artikel_id in set(fest) | set(verkauft) | set(bedarf):
        artikel = Artikel.objects.filter(pk=artikel_id, typ__in=["A", "P"], nicht_aktiv=False).first()
        if not artikel:
            continue
        if artikel_id in fest:
            ziel, grund = Decimal(fest[artikel_id]), "Mindestbestand"
        elif artikel_id in verkauft:
            ziel, grund = (verkauft[artikel_id] / 3).to_integral_value(rounding="ROUND_CEILING"), "Verkaufsverlauf (1 Monat)"
        else:
            ziel, grund = Decimal("0"), ""
        # Bedarf sind alle offenen Kundenbestellungen, auch Positionen ohne Haken „R“ (sie warten auf genau diese Ware):
        # sie stehen in „haben“ als Abzug, die Fehlmenge kommt so zum Ziel dazu
        offen_kunde = reserviert(mandant, artikel, mit_wartenden=True)
        lager = bestand(mandant, artikel)
        haben = lager - offen_kunde + offen_bestellt(mandant, artikel, mit_entwurf=True)
        if haben < ziel:
            gruende = [g for g in (grund, "Kundenbestellung" if offen_kunde > lager else "") if g]
            al = guenstigster_lieferant(artikel)
            vorschlag.append({"artikel": artikel, "ziel": ziel, "haben": haben, "menge": ziel - haben, "grund": " + ".join(gruende),
                              "lager": lager, "kundenbedarf": offen_kunde, "kundenbelege": bedarf.get(artikel_id, []),
                              "lieferant": al.lieferant if al else None, "ek": al.ek if al else None})
    return sorted(vorschlag, key=lambda z: z["artikel"].nummer)


@transaction.atomic
def bestellungen_aus_vorschlag(mandant, auswahl) -> list:
    """auswahl: Liste von (artikel_id, menge); je Lieferant wird eine Entwurfsbestellung angelegt."""
    je_lieferant = defaultdict(list)
    for artikel_id, menge in auswahl:
        artikel = Artikel.objects.filter(pk=artikel_id).first()
        if artikel is None:
            raise ValueError("Ein gewählter Artikel existiert nicht mehr.")
        if menge is None or not menge.is_finite() or not Decimal("0") < menge < Decimal("10000000"):
            raise ValueError(f"{artikel.nummer}: Die Menge muss größer als 0 sein.")
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
    if lieferant.gesperrt:  # wie in der Oberfläche: bei gesperrten Lieferanten entstehen keine neuen Belege
        raise ValueError(f"Lieferant {lieferant.name1} ist gesperrt – es lässt sich kein Wareneingang anlegen.")
    if bestellung is not None and bestellung.status == BestellStatus.STORNIERT:
        raise ValueError(f"Die Bestellung {bestellung.nummer} ist storniert – dazu lässt sich kein Wareneingang erfassen.")
    we =Wareneingang.objects.create(mandant=mandant, lieferant=lieferant, bestellung=bestellung,
                                     nummer=nummer(mandant, "WE"), benutzer=benutzer)
    return we


def gleicher_artikel(hersteller: str, ean: str, artikel_hersteller: str, artikel_ean: str) -> bool:
    """Herstellernummern sind nicht eindeutig: Ein Treffer über die Herstellernummer gilt nur, wenn auch der Hersteller
    oder die EAN übereinstimmt. Fehlen beide Angaben auf einer Seite, wird nicht verknüpft."""
    hersteller_gleich = bool(hersteller and artikel_hersteller and hersteller.strip().lower() == artikel_hersteller.strip().lower())
    ean_gleich = bool(ean and artikel_ean and ean.strip() == artikel_ean.strip())
    return hersteller_gleich or ean_gleich


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
            if gleicher_artikel(eintrag.hersteller, eintrag.ean, kandidat.hersteller.name if kandidat.hersteller_id else "", kandidat.ean):
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
    p_positionen_umstellen(eintrag, artikel)
    return artikel


def p_positionen_umstellen(eintrag, artikel):
    """Offene Bestell- und Verkaufspositionen des Preislistenartikels hängen danach am eigenen Artikel (A)."""
    BestellPosition.objects.filter(preisliste_artikel=eintrag, artikel__isnull=True).update(artikel=artikel)
    umstellen = Position.objects.filter(preisliste_artikel=eintrag, artikel__isnull=True).exclude(
        beleg__art__in=("RE", "GS", "SP"), beleg__abgeschlossen=True)  # Verkaufspositionen (außer abgeschlossene Rechnungen, Gutschriften, Proforma-Stornos) werden zu A
    umstellen.update(artikel=artikel, typ="A", preisliste_artikel=None)


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


def we_bezug_holen(mandant, eintrag, we_id, bp_id="", menge=""):
    """Artikelformular aus dem Wareneingang (P → A mit Formular): prüft den Bezug zum offenen Wareneingang.
    Mit Bestellposition: offene P-Position genau dieses Preislistenartikels; ohne (Scan): Lieferant des Eintrags = Lieferant
    des Wareneingangs, Menge > 0. Gibt {"we", "bp", "menge"} zurück oder None, wenn kein gültiger Bezug besteht."""
    if not str(we_id).isdigit():
        return None
    we = Wareneingang.objects.select_related("bestellung").filter(pk=we_id, mandant=mandant, gebucht=False).first()
    if we is None:
        return None
    if str(bp_id).isdigit():
        if not we.bestellung_id:
            return None
        bp = we.bestellung.positionen.filter(pk=bp_id, artikel__isnull=True, preisliste_artikel=eintrag).first()
        return {"we": we, "bp": bp, "menge": "", "eintrag": eintrag} if bp else None
    if eintrag.lieferant_id != we.lieferant_id or eintrag.artikel_id is not None:  # Scan: noch ohne eigenen Artikel
        return None
    try:
        menge_wert = Decimal(str(menge).replace(",", ".") or "1")
    except ArithmeticError:
        return None
    if not menge_wert.is_finite() or menge_wert <= 0:
        return None
    return {"we": we, "bp": None, "menge": str(menge_wert), "eintrag": eintrag}


@transaction.atomic
def we_artikel_uebernehmen(bezug, artikel):
    """Nach dem Speichern des Artikelformulars: neuen A-Artikel an die Bestellposition hängen und in den Wareneingang
    übernehmen (offene Menge), beim Scan mit der gescannten Menge erfassen."""
    if artikel.typ != "A":  # aus dem Wareneingang entsteht immer ein Lagerartikel (README 18: aus P wird A)
        raise ValueError("Aus dem Wareneingang wird ein Lagerartikel (Typ A) angelegt – bitte Typ A wählen. Es wurde nichts angelegt.")
    we = Wareneingang.objects.select_for_update().get(pk=bezug["we"].pk)
    if we.gebucht:
        raise ValueError("Der Wareneingang ist inzwischen gebucht – es wurde nichts angelegt.")
    eintrag = Preislistenartikel.objects.select_for_update().get(pk=bezug["eintrag"].pk)
    if eintrag.artikel_id != artikel.pk:  # inzwischen anderweitig angelegt (anderer Tab, schneller Weg): keinen zweiten Artikel
        raise ValueError("Zu diesem Preislistenartikel gibt es inzwischen einen eigenen Artikel – es wurde nichts angelegt. "
                         "Bitte den Wareneingang neu laden.")
    bp = bezug["bp"]
    if bp is not None:
        bp = BestellPosition.objects.select_for_update().get(pk=bp.pk)
        if bp.artikel_id is not None and bp.artikel_id != artikel.pk:
            raise ValueError("Die Position hat inzwischen einen anderen Artikel – bitte den Wareneingang neu laden.")
        p_positionen_umstellen(bp.preisliste_artikel, artikel)  # wie „anlegen und übernehmen“: auch Verkaufspositionen
        bp.refresh_from_db()
        wareneingang_aus_bestellung(we, nur=bp)
    else:
        p_positionen_umstellen(bezug["eintrag"], artikel)
        wareneingang_erfassen(we, artikel, Decimal(bezug["menge"]))


def _bestellposition_fuer_scan(we, artikel):
    """Bestellposition für einen Scan: bei mehreren Positionen desselben Artikels die erste, in der nach diesem Wareneingang
    noch etwas offen ist; sind alle voll, die erste (dann folgt wie bisher die Rückfrage wegen Überlieferung)."""
    kandidaten = list(we.bestellung.positionen.filter(artikel=artikel).order_by("id"))
    if len(kandidaten) <= 1:
        return kandidaten[0] if kandidaten else None
    erfasst = defaultdict(Decimal)
    for bp_id, menge in we.positionen.filter(bestellposition__in=kandidaten).values_list("bestellposition_id", "menge"):
        erfasst[bp_id] += menge
    return next((bp for bp in kandidaten if bp.offen - erfasst[bp.pk] > 0), kandidaten[0])


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
        bp = _bestellposition_fuer_scan(we, artikel)
    vorhanden = we.positionen.filter(artikel=artikel)
    if bp is not None:  # mehrere Bestellpositionen desselben Artikels: Zeile der gewählten Position erhöhen
        vorhanden = vorhanden.filter(bestellposition=bp)
    vorhanden = vorhanden.first()
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
    if we.bestellung_id and we.bestellung.status == BestellStatus.STORNIERT:
        raise ValueError(f"Die Bestellung {we.bestellung.nummer} ist storniert – der Wareneingang lässt sich nicht buchen.")
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
            seriennummern_einlagern(we.mandant, a, p.seriennummern, we.nummer, wareneingang=we)
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


def wareneingang_loeschen(we) -> str:
    """Ungebuchten Wareneingang löschen (die Mengen bleiben in der Bestellung offen). Gebuchte nie: sie haben das Lager verändert.
    Gibt die Nummer des gelöschten Wareneingangs zurück."""
    with transaction.atomic():
        w = Wareneingang.objects.select_for_update().get(pk=we.pk)  # Sperre: nicht gleichzeitig buchen und löschen
        if w.gebucht:
            raise ValueError("Der Wareneingang ist bereits gebucht und lässt sich nicht löschen.")
        if w.rechnungen.exists():
            raise ValueError("Der Wareneingang ist einer Lieferantenrechnung zugeordnet – bitte erst die Zuordnung aufheben.")
        nummer_ = w.nummer
        r = Ruecksendung.objects.select_for_update().filter(ersatz_wareneingang=w).first()
        if r is not None:  # Ersatz doch nicht da: die Rücksendung ist wieder offen (versendet)
            r.status, r.erledigt_am, r.ersatz_wareneingang = RuecksendungStatus.VERSENDET, None, None
            r.historie_eintrag(f"Ersatz-Wareneingang {nummer_} gelöscht – Rücksendung wieder offen")
            r.save()
        w.delete()
    return nummer_


def position_loeschen(bestellung, position_id):
    """Bestellposition entfernen – nur wenn nichts geliefert ist und sie in keinem offenen (ungebuchten) Wareneingang steht.
    Eine bestellte Bestellung behält mindestens eine Position (nur der Entwurf darf leer werden)."""
    with transaction.atomic():
        b = Bestellung.objects.select_for_update().get(pk=bestellung.pk)
        pos = BestellPosition.objects.select_for_update().filter(pk=position_id, bestellung=b).first()
        if pos is None:
            raise ValueError("Die Position gibt es nicht mehr.")
        if b.status in (BestellStatus.STORNIERT, BestellStatus.GELIEFERT):
            raise ValueError("Diese Bestellung lässt sich nicht mehr ändern.")
        if pos.geliefert:
            raise ValueError("Zur Position gibt es schon Wareneingänge.")
        if pos.eingaenge.filter(wareneingang__gebucht=False).exists():
            raise ValueError("Die Position steht in einem offenen Wareneingang – bitte dort ändern oder ihn erst löschen.")
        if b.status != BestellStatus.ENTWURF and b.positionen.count() <= 1:
            raise ValueError("Die letzte Position einer bestellten Bestellung lässt sich nicht löschen – bitte die Bestellung stornieren.")
        pos.delete()


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


RMA_OFFEN = (RmaStatus.ANGELEGT, RmaStatus.EINGESENDET)


@transaction.atomic
def rma_anlegen(mandant, lieferant, bezeichnung, fehler, artikel=None, seriennummer="", reparatur=None) -> Rma:
    """Doppelklick-Schutz: Zu einem Werkstattauftrag gibt es höchstens eine offene RMA; ohne Werkstattauftrag wird dieselbe
    Meldung innerhalb von 30 Sekunden nicht zweimal angelegt."""
    if reparatur is not None:
        from apps.werkstatt.models import Reparatur
        reparatur = Reparatur.objects.select_for_update().get(pk=reparatur.pk)
        offen = Rma.objects.filter(reparatur=reparatur, status__in=RMA_OFFEN).first()
        if offen:
            raise ValueError(f"Zu diesem Werkstattauftrag gibt es schon die offene RMA {offen.nummer}.")
    else:
        doppelt = Rma.objects.filter(
            mandant=mandant, lieferant=lieferant, bezeichnung=bezeichnung, fehler=fehler, artikel=artikel, seriennummer=seriennummer,
            reparatur__isnull=True, status=RmaStatus.ANGELEGT, angelegt_am__gte=timezone.now() - timedelta(seconds=30)).first()
        if doppelt:
            raise ValueError(f"Die RMA {doppelt.nummer} wurde soeben schon mit denselben Angaben angelegt.")
    rma = Rma.objects.create(mandant=mandant, lieferant=lieferant, nummer=nummer(mandant, "RM"), bezeichnung=bezeichnung,
                             fehler=fehler, artikel=artikel, seriennummer=seriennummer, reparatur=reparatur)
    if reparatur is not None:
        from apps.werkstatt.models import Status
        reparatur.status = Status.RMA
        reparatur.save()
    return rma


# ---- WE-Rücksendung -----------------------------------------------------------------------------

def ruecksendbar(we_position) -> Decimal:
    """Was aus einer Wareneingangsposition noch zurückgehen kann: Menge minus schon zurückgesendet (abgelehnte zählen nicht,
    die Ware ist wieder im Lager)."""
    weg = RuecksendungPosition.objects.filter(we_position=we_position).exclude(
        ruecksendung__status=RuecksendungStatus.ABGELEHNT).aggregate(s=Sum("menge"))["s"] or Decimal("0")
    return max(we_position.menge - weg, Decimal("0"))


def seriennummern_im_lager(we_position) -> list:
    """Seriennummern dieser Wareneingangsposition, die noch im Lager sind (nur diese können zurückgehen)."""
    from apps.lager.models import Seriennummer
    nummern = zeilen(we_position.seriennummern)
    if not nummern:
        return []
    we = we_position.wareneingang
    return list(Seriennummer.objects.filter(mandant=we.mandant, artikel=we_position.artikel, nummer__in=nummern,
                                            status=Seriennummer.IM_LAGER).order_by("nummer").values_list("nummer", flat=True))


@transaction.atomic
def ruecksendung_anlegen(we, auswahl, art, grund, benutzer=None) -> Ruecksendung:
    """auswahl: [(WareneingangPosition, Menge, [Seriennummern])]. Bucht den Bestand sofort ab (nie unter 0), Seriennummern
    gehen auf „an Lieferant zurück“. Die Lieferantenbestellung und die Lieferantenrechnung bleiben unverändert."""
    from apps.lager.models import Seriennummer
    # frisch und gesperrt lesen: zwei Rücksendungen gleichzeitig werden nacheinander geprüft
    we = Wareneingang.objects.select_for_update().select_related("lieferant").get(pk=we.pk)
    if not we.gebucht:
        raise ValueError("Zurücksenden geht nur aus einem gebuchten Wareneingang.")
    if art not in RuecksendungArt.values:
        raise ValueError("Bitte wählen: zur Gutschrift oder zum Austausch.")
    grund = " ".join((grund or "").split())
    if not grund:
        raise ValueError("Bitte den Grund der Rücksendung angeben (z. B. Überlieferung, falsche Ware).")
    zeilen_ = [(p, m, sn) for p, m, sn in auswahl if m]
    if not zeilen_:
        raise ValueError("Bitte bei mindestens einer Position eine Menge eintragen.")
    for p, menge, sn in zeilen_:
        if p.wareneingang_id != we.pk:
            raise ValueError("Die Position gehört nicht zu diesem Wareneingang.")
        if not menge.is_finite() or menge <= 0:
            raise ValueError(f"{p.artikel.nummer}: Die Menge muss größer als 0 sein.")
        rest = ruecksendbar(p)
        if menge > rest:
            raise ValueError(f"{p.artikel.nummer}: Höchstens {rest.normalize():f} können noch zurückgehen "
                             f"(geliefert {p.menge.normalize():f}).")
        if p.artikel.esd_key:
            raise ValueError(f"{p.artikel.nummer}: ESD-Keys lassen sich nicht als Ware zurücksenden.")
        if len(set(sn)) != len(sn):
            raise ValueError(f"{p.artikel.nummer}: Eine Seriennummer ist doppelt gewählt.")
        if zeilen(p.seriennummern):  # mit Seriennummern eingegangen: genau die zurückgehenden Nummern wählen
            if menge != menge.to_integral_value() or len(sn) != menge:
                raise ValueError(f"{p.artikel.nummer}: Bitte genau {menge.normalize():f} Seriennummer(n) auswählen "
                                 f"({len(sn)} gewählt).")
            list(Seriennummer.objects.select_for_update().filter(mandant=we.mandant, artikel=p.artikel, nummer__in=sn)
                 .values_list("pk", flat=True))  # Sperre: nicht gleichzeitig verkaufen und zurücksenden
            frei = set(seriennummern_im_lager(p))
            fehlt = [n for n in sn if n not in frei]
            if fehlt:
                raise ValueError(f"{p.artikel.nummer}: Seriennummer {fehlt[0]} ist nicht (mehr) im Lager.")
        elif sn:
            raise ValueError(f"{p.artikel.nummer}: Zu dieser Position gibt es keine Seriennummern.")
    r = Ruecksendung.objects.create(mandant=we.mandant, lieferant=we.lieferant, wareneingang=we, nummer=nummer(we.mandant, "WR"),
                                    art=art, grund=grund[:1000], benutzer=benutzer)
    for p, menge, sn in zeilen_:
        RuecksendungPosition.objects.create(ruecksendung=r, we_position=p, artikel=p.artikel, menge=menge, ek=p.ek,
                                            seriennummern="\n".join(sn))
        Lagerbewegung.objects.create(mandant=we.mandant, artikel=p.artikel, art=Bewegungsart.RUECKSENDUNG, menge=-menge,
                                     bemerkung=f"{r.nummer} an {we.lieferant.name1}"[:200], benutzer=benutzer)  # prüft Bestand ≥ 0
        if sn and Seriennummer.objects.filter(mandant=we.mandant, artikel=p.artikel, nummer__in=sn, status=Seriennummer.IM_LAGER) \
                .update(status=Seriennummer.ZURUECK, position=None) != len(sn):
            raise ValueError(f"{p.artikel.nummer}: Eine gewählte Seriennummer ist inzwischen nicht mehr im Lager – bitte neu wählen.")
    r.historie_eintrag(f"angelegt ({r.get_art_display()}), {len(zeilen_)} Position(en) aus dem Lager gebucht – {grund}", benutzer)
    r.save(update_fields=["historie"])
    return r


def _ruecksendung_offen(r):
    r = Ruecksendung.objects.select_for_update().get(pk=r.pk)
    if r.status not in RUECKSENDUNG_OFFEN:
        raise ValueError(f"Die Rücksendung {r.nummer} ist schon erledigt ({r.get_status_display()}).")
    return r


@transaction.atomic
def ruecksendung_versendet(r, benutzer=None) -> Ruecksendung:
    r = _ruecksendung_offen(r)
    if r.status != RuecksendungStatus.ANGELEGT:
        raise ValueError("Die Rücksendung ist schon als versendet vermerkt.")
    r.status, r.versendet_am = RuecksendungStatus.VERSENDET, timezone.localdate()
    r.historie_eintrag("an den Lieferanten versendet", benutzer)
    r.save()
    return r


@transaction.atomic
def ruecksendung_gutschrift(r, betrag, benutzer=None) -> Ruecksendung:
    """Gutschrift des Lieferanten erhalten (Betrag netto, Pflicht). Auch nach einer Rücksendung zum Austausch möglich,
    wenn der Lieferant keinen Ersatz liefert."""
    r = _ruecksendung_offen(r)
    if betrag is None or not betrag.is_finite() or betrag < 0:
        raise ValueError("Bitte den Betrag der Gutschrift (netto) eintragen.")
    if betrag >= Decimal("10000000000"):
        raise ValueError("Der Betrag der Gutschrift ist zu groß.")
    r.status, r.gutschrift_betrag, r.erledigt_am = RuecksendungStatus.GUTSCHRIFT, betrag.quantize(Decimal("0.01")), timezone.localdate()
    r.versendet_am = r.versendet_am or r.erledigt_am
    r.historie_eintrag(f"Gutschrift erhalten: {r.gutschrift_betrag} € netto (erwartet {r.summe_netto} €)", benutzer)
    r.save()
    return r


@transaction.atomic
def ruecksendung_ersatz(r, benutzer=None) -> Wareneingang:
    """Ersatz kommt: legt einen (noch nicht gebuchten) Wareneingang mit denselben Artikeln, Mengen und EK an. Gebucht wird er
    wie jeder Wareneingang (dort auch die neuen Seriennummern); zur Lieferantenrechnung gehört er nicht."""
    r = _ruecksendung_offen(r)
    if r.lieferant.gesperrt:
        raise ValueError(f"Lieferant {r.lieferant.name1} ist gesperrt – bitte erst entsperren.")
    we = wareneingang_anlegen(r.mandant, r.lieferant, benutzer=benutzer)
    we.lieferschein = f"Ersatz zu {r.nummer}"
    we.save(update_fields=["lieferschein"])
    for p in r.positionen.select_related("artikel"):
        WareneingangPosition.objects.create(wareneingang=we, artikel=p.artikel, menge=p.menge, ek=p.ek)
    r.status, r.ersatz_wareneingang, r.erledigt_am = RuecksendungStatus.ERSATZ, we, timezone.localdate()
    r.versendet_am = r.versendet_am or r.erledigt_am
    r.historie_eintrag(f"Ersatz erhalten: Wareneingang {we.nummer} angelegt (noch zu buchen)", benutzer)
    r.save()
    return we


@transaction.atomic
def ruecksendung_abgelehnt(r, grund, benutzer=None) -> Ruecksendung:
    """Der Lieferant nimmt nicht zurück: Die Ware kommt wieder ins Lager, die Seriennummern sind wieder im Lager."""
    from apps.lager.models import Seriennummer
    r = _ruecksendung_offen(r)
    grund = " ".join((grund or "").split())
    if not grund:
        raise ValueError("Bitte angeben, warum der Lieferant ablehnt.")
    for p in r.positionen.select_related("artikel"):
        Lagerbewegung.objects.create(mandant=r.mandant, artikel=p.artikel, art=Bewegungsart.RUECKSENDUNG, menge=p.menge,
                                     bemerkung=f"{r.nummer} abgelehnt, Ware zurück"[:200], benutzer=benutzer)
        sn = zeilen(p.seriennummern)
        if sn and Seriennummer.objects.filter(mandant=r.mandant, artikel=p.artikel, nummer__in=sn, status=Seriennummer.ZURUECK) \
                .update(status=Seriennummer.IM_LAGER, position=None) != len(sn):
            raise ValueError(f"{p.artikel.nummer}: Eine Seriennummer der Rücksendung ist inzwischen wieder im Lager oder verkauft – "
                             "bitte am Artikel prüfen, bevor die Ware zurückgebucht wird.")
    r.status, r.erledigt_am = RuecksendungStatus.ABGELEHNT, timezone.localdate()
    r.historie_eintrag(f"abgelehnt – Ware wieder im Lager: {grund}", benutzer)
    r.save()
    return r
