from collections import defaultdict
from decimal import Decimal

from django.db import transaction

from apps.verkauf.models import Belegart

from .models import Bewegungsart, Lagerbewegung

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
