from collections import defaultdict
from decimal import Decimal

from django.db import transaction

from apps.verkauf.models import Belegart

from .models import Bewegungsart, Lagerbewegung, Seriennummer, bestand

LAGERFUEHREND = "A"  # nur Typ A (Lagerartikel) wird gebucht


def bucht_bestand(beleg) -> bool:
    """Lieferschein bucht immer ab; eine Rechnung nur, wenn kein Lieferschein in der Belegkette liegt."""
    if beleg.art == Belegart.LIEFERSCHEIN:
        return True
    if beleg.art != Belegart.RECHNUNG:
        return False
    vorher = beleg.vorgaenger
    while vorher is not None:
        if vorher.art == Belegart.LIEFERSCHEIN:
            return False
        vorher = vorher.vorgaenger
    return True


@transaction.atomic
def buche_beleg(beleg, benutzer=None) -> int:
    """Bucht die Lagerartikel des Belegs ab (einmal je Beleg). Rückgabe: Zahl der Bewegungen."""
    if not bucht_bestand(beleg) or Lagerbewegung.objects.filter(beleg=beleg, art=Bewegungsart.VERKAUF).exists():
        return 0
    mengen = defaultdict(Decimal)
    artikel = {}
    for p in beleg.positionen.select_related("artikel"):
        if p.artikel_id and p.artikel.typ == LAGERFUEHREND:
            mengen[p.artikel_id] += p.menge
            artikel[p.artikel_id] = p.artikel
    for artikel_id, menge in mengen.items():
        Lagerbewegung.objects.create(
            mandant=beleg.mandant, artikel=artikel[artikel_id], art=Bewegungsart.VERKAUF, menge=-menge,
            beleg=beleg, bemerkung=str(beleg), benutzer=benutzer)
    return len(mengen)


@transaction.atomic
def buche_manuell(mandant, artikel, art, menge, bemerkung="", benutzer=None):
    if art in (Bewegungsart.EIGENVERBRAUCH, Bewegungsart.VERKAUF):
        menge = -abs(menge)
    elif art in (Bewegungsart.WARENEINGANG, Bewegungsart.RETOURE, Bewegungsart.ANFANGSBESTAND):
        menge = abs(menge)
    if menge == 0:
        raise ValueError("Menge darf nicht 0 sein.")
    return Lagerbewegung.objects.create(mandant=mandant, artikel=artikel, art=art, menge=menge,
                                        bemerkung=bemerkung, benutzer=benutzer)


@transaction.atomic
def inventur(mandant, artikel, gezaehlt: Decimal, benutzer=None):
    """Differenzbuchung auf den gezählten Bestand."""
    from .models import bestand
    differenz = gezaehlt - bestand(mandant, artikel)
    if differenz == 0:
        return None
    return Lagerbewegung.objects.create(
        mandant=mandant, artikel=artikel, art=Bewegungsart.INVENTUR, menge=differenz,
        bemerkung=f"Inventur: gezählt {gezaehlt}", benutzer=benutzer)


def zeilen(text: str) -> list:
    return list(dict.fromkeys(z.strip() for z in (text or "").replace(",", chr(10)).splitlines() if z.strip()))


@transaction.atomic
def seriennummern_einlagern(mandant, artikel, text, bemerkung=""):
    neu = []
    for nr in zeilen(text):
        sn, angelegt = Seriennummer.objects.get_or_create(
            mandant=mandant, artikel=artikel, nummer=nr, defaults={"eingang_bemerkung": bemerkung})
        if not angelegt:
            if sn.status == Seriennummer.IM_LAGER:
                raise ValueError(f"Seriennummer {nr} ist bereits im Lager.")
            sn.status, sn.position = Seriennummer.IM_LAGER, None  # Retoure: wieder eingelagert
            sn.eingang_bemerkung = bemerkung
            sn.save()
        neu.append(sn)
    return neu


@transaction.atomic
def seriennummern_zuordnen(position, text):
    """Ordnet einer Belegposition Seriennummern aus dem Lager zu (nur Lagerartikel, nicht mehr als die Menge)."""
    beleg = position.beleg
    nummern = zeilen(text)
    if len(nummern) > position.menge:
        raise ValueError(f"Mehr Seriennummern ({len(nummern)}) als Menge ({position.menge:f}).")
    for sn in position.seriennummern.all():  # vorherige Zuordnung dieser Position lösen
        sn.status, sn.position = Seriennummer.IM_LAGER, None
        sn.save()
    for nr in nummern:
        sn = Seriennummer.objects.filter(mandant=beleg.mandant, artikel=position.artikel, nummer=nr).first()
        if sn is None or sn.status != Seriennummer.IM_LAGER:
            raise ValueError(f"Seriennummer {nr} ist für {position.artikel.nummer} nicht im Lager.")
        sn.status, sn.position = Seriennummer.VERKAUFT, position
        sn.save()


@transaction.atomic
def umbuchen(von_mandant, nach_mandant, artikel, menge, benutzer=None):
    if von_mandant == nach_mandant:
        raise ValueError("Quell- und Zielmandant sind gleich.")
    if menge <= 0:
        raise ValueError("Menge muss positiv sein.")
    text = f"Umbuchung {von_mandant.kurzname} → {nach_mandant.kurzname}"
    Lagerbewegung.objects.create(mandant=von_mandant, artikel=artikel, art=Bewegungsart.UMBUCHUNG, menge=-menge,
                                 bemerkung=text, benutzer=benutzer)
    Lagerbewegung.objects.create(mandant=nach_mandant, artikel=artikel, art=Bewegungsart.UMBUCHUNG, menge=menge,
                                 bemerkung=text, benutzer=benutzer)


@transaction.atomic
def retoure_aus_gutschrift(gutschrift, benutzer=None) -> int:
    """Bucht die Lagerartikel einer abgeschlossenen Gutschrift wieder ins Lager (einmal je Gutschrift)."""
    if gutschrift.art != Belegart.GUTSCHRIFT or not gutschrift.abgeschlossen:
        raise ValueError("Nur zu einer abgeschlossenen Gutschrift möglich.")
    if Lagerbewegung.objects.filter(beleg=gutschrift, art=Bewegungsart.RETOURE).exists():
        raise ValueError("Diese Gutschrift wurde bereits ins Lager zurückgebucht.")
    mengen, artikel = defaultdict(Decimal), {}
    for p in gutschrift.positionen.select_related("artikel"):
        if p.artikel_id and p.artikel.typ == LAGERFUEHREND:
            mengen[p.artikel_id] += p.menge
            artikel[p.artikel_id] = p.artikel
    for artikel_id, menge in mengen.items():
        Lagerbewegung.objects.create(mandant=gutschrift.mandant, artikel=artikel[artikel_id], art=Bewegungsart.RETOURE,
                                     menge=menge, beleg=gutschrift, bemerkung=str(gutschrift), benutzer=benutzer)
    return len(mengen)
