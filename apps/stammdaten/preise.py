"""
Verkaufspreise (Pflichtenheft Abschnitt 7): Preisstufen VK1-VK3, Aufschlag auf den EK (global, je Kategorie,
je Artikel überschreibbar) und Kundensonderpreise.

Reihenfolge: Sonderpreis des Kunden -> fest eingetragener VK der Stufe -> EK + Aufschlag
(Artikel vor Kategorie [Unterkategorie vor Oberkategorie] vor global).
"""
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from .models import Kundenpreis, PreisVorgabe

CENT = Decimal("0.01")


def aufschlag(artikel, stufe: int):
    """Aufschlag in Prozent für die Preisstufe oder None, wenn nirgends einer hinterlegt ist."""
    wert = getattr(artikel, f"aufschlag_vk{stufe}", None)
    if wert is not None:
        return wert
    kategorie, schutz = artikel.kategorie, 0
    while kategorie is not None and schutz < 10:  # Unterkategorie vor Oberkategorie
        wert = getattr(kategorie, f"aufschlag_vk{stufe}", None)
        if wert is not None:
            return wert
        kategorie, schutz = kategorie.eltern, schutz + 1
    vorgabe = PreisVorgabe.objects.first()
    return getattr(vorgabe, f"aufschlag_vk{stufe}", None) if vorgabe else None


def stufenpreis(artikel, stufe: int):
    """Netto-VK der Stufe (fest eingetragen oder aus EK + Aufschlag); None, wenn nicht ermittelbar."""
    fest = getattr(artikel, f"vk{stufe}", None)
    if fest is not None:
        return fest
    prozent = aufschlag(artikel, stufe)
    ek = artikel.ek_errechnet
    if prozent is not None and ek:
        return (ek * (Decimal("100") + prozent) / 100).quantize(CENT, rounding=ROUND_HALF_UP)
    return None


def sonderpreis(artikel, kunde, am=None):
    am = am or date.today()
    for k in Kundenpreis.objects.filter(kunde=kunde, artikel=artikel):
        if (k.gueltig_ab is None or k.gueltig_ab <= am) and (k.gueltig_bis is None or am <= k.gueltig_bis):
            return k.preis
    return None


def verkaufspreis(artikel, kunde=None, am=None):
    """Preisvorschlag für Belege. Gibt (Preis, Quelle) zurück; Quelle: 'sonderpreis', 'stufe' oder None."""
    if kunde is not None:
        preis = sonderpreis(artikel, kunde, am)
        if preis is not None:
            return preis, "sonderpreis"
        stufe = kunde.preisstufe
    else:
        stufe = 1
    preis = stufenpreis(artikel, stufe)
    if preis is None and stufe != 1:  # fehlt die Händlerstufe, gilt ersatzweise VK1
        preis = stufenpreis(artikel, 1)
    return preis, ("stufe" if preis is not None else None)


def preislistenpreis(eintrag, kunde=None):
    """Netto-VK für einen Preislistenartikel: EK des Lieferanten plus globaler Aufschlag der Preisstufe; None ohne Aufschlag."""
    stufe = kunde.preisstufe if kunde is not None else 1
    vorgabe = PreisVorgabe.objects.first()
    prozent = getattr(vorgabe, f"aufschlag_vk{stufe}", None) if vorgabe else None
    if prozent is None and stufe != 1 and vorgabe:
        prozent = vorgabe.aufschlag_vk1
    if prozent is None:
        return None
    return (eintrag.ek * (Decimal("100") + prozent) / 100).quantize(CENT, rounding=ROUND_HALF_UP)
