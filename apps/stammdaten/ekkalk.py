"""
VK-Kalkulation mit mehreren EK-Quellen (Issue 12, Schritt 1). Alles gilt pro Mandant.

ek_kalk (erster Treffer gilt):
 1. ek2_fix                         -> ek2
 2. Bestand > 0                     -> max(ek_avg, ek_import falls vorhanden)
 3. Bestand = 0 und ek_import       -> ek_import
 4. Bestand = 0 und ek_we           -> ek_we
 5. ek2 vorhanden                   -> ek2
 6. sonst                           -> keiner (kein VK)
Bei Bestand > 0 ohne ek_avg (Altdaten ohne Wareneingang) fällt die Kette auf ek_import, dann ek2 zurück.
"""
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal

from .models import ArtikelMandant, Artikeltyp, EkHerkunft, PreisVorgabe, _ek_quelle
from .preise import aufschlag

EK_STELLEN = Decimal("0.0001")
CENT = Decimal("0.01")
SENKUNG_AUTO_GRENZE = Decimal("10")  # Prozent; einstellbar folgt mit den Radar-Einstellungen

QUELLEN = {"ek2_fix": "EK2 (fix)", "ek_avg": "Ø-EK", "ek_import": "Import-EK", "ek_we": "letzter Wareneingang",
           "ek2": "EK2", "keine": "kein EK"}


def ek_import(artikel):
    """Günstigster Preislisten-EK: erst der übernommene Import-EK, sonst der günstigste Eintrag der Preislisten. Gilt für alle Mandanten."""
    if artikel.durchschnitts_ek and artikel.ek_herkunft != EkHerkunft.WARENEINGANG:
        return artikel.durchschnitts_ek
    if artikel.pk:
        al = artikel.lieferanten.filter(ek__gt=0).order_by("ek").first()
        if al:
            return al.ek
    return None


@dataclass(frozen=True)
class Kalkulation:
    ek_kalk: Decimal | None
    quelle: str  # Schlüssel aus QUELLEN

    @property
    def quelle_text(self):
        return QUELLEN[self.quelle]


def kalkulations_ek(bestand, ek_avg=None, ek_import=None, ek_we=None, ek2=None, ek2_fix=False) -> Kalkulation:
    """Reine Rechenregel, ohne Datenbank."""
    if ek2_fix and ek2 is not None:
        return Kalkulation(ek2, "ek2_fix")
    if bestand > 0:
        if ek_avg is not None:
            if ek_import is not None and ek_import > ek_avg:
                return Kalkulation(ek_import, "ek_import")
            return Kalkulation(ek_avg, "ek_avg")
        if ek_import is not None:
            return Kalkulation(ek_import, "ek_import")
    else:
        if ek_import is not None:
            return Kalkulation(ek_import, "ek_import")
        if ek_we is not None:
            return Kalkulation(ek_we, "ek_we")
    if ek2 is not None:
        return Kalkulation(ek2, "ek2")
    return Kalkulation(None, "keine")


def kalkulation(artikel, mandant, bestand=None) -> Kalkulation:
    """ek_kalk und Quelle eines Lagerartikels (Typ A) für den Mandanten. Gruppen und Leistungen behalten ihre eigene Regel."""
    if artikel.typ != Artikeltyp.A:
        ek, _ = _ek_quelle(artikel)
        return Kalkulation(ek, "ek2" if ek is not None else "keine")
    if bestand is None:
        from apps.lager.models import bestand as lagerbestand
        bestand = lagerbestand(mandant, artikel)
    am = ArtikelMandant.objects.filter(artikel=artikel, mandant=mandant).first()
    return kalkulations_ek(bestand, am.ek_avg if am else None, ek_import(artikel), am.ek_we if am else None,
                           am.ek2 if am else None, am.ek2_fix if am else False)


def neuer_durchschnitts_ek(bestand_alt, ek_avg_alt, menge, preis):
    """Gleitender Ø-EK nach einem Wareneingang, 4 Nachkommastellen. Verkäufe, Abgänge und Retouren rufen das nicht auf."""
    if bestand_alt <= 0 or ek_avg_alt is None:
        return preis.quantize(EK_STELLEN, rounding=ROUND_HALF_UP)
    wert = (bestand_alt * ek_avg_alt + menge * preis) / (bestand_alt + menge)
    return wert.quantize(EK_STELLEN, rounding=ROUND_HALF_UP)


def wareneingang_ek(mandant, artikel, bestand_alt, menge, preis):
    """Hält Ø-EK und letzten Wareneingangs-EK im Mandanten fest. Aufruf vor der Lagerbuchung (bestand_alt = Bestand davor)."""
    if artikel.typ not in (Artikeltyp.A, Artikeltyp.P):  # P wird mit dem Wareneingang zum Lagerartikel
        return None
    am, _ = ArtikelMandant.objects.get_or_create(artikel=artikel, mandant=mandant)
    am.ek_avg = neuer_durchschnitts_ek(bestand_alt, am.ek_avg, menge, preis)
    am.ek_we = preis.quantize(EK_STELLEN, rounding=ROUND_HALF_UP)
    am.save(update_fields=["ek_avg", "ek_we"])
    return am


# ---- VK -------------------------------------------------------------------------------------------

def marge_stufe(artikel, stufe):
    """Zielmarge der Stufe als Bruchteil (0,2 = 20 %): Artikel vor Kategorie vor global. Die hinterlegten Aufschläge (% auf den EK)
    sind gleichwertig umgerechnet: Marge = Aufschlag / (100 + Aufschlag); EK × (1 + a) = EK / (1 − m)."""
    prozent = aufschlag(artikel, stufe)
    return None if prozent is None else prozent / (Decimal("100") + prozent)


def runden(vk, auf_99=None):
    if auf_99 is None:
        vorgabe = PreisVorgabe.objects.first()
        auf_99 = bool(vorgabe and vorgabe.rundung_99)
    if auf_99:  # nächster Preis auf ,99 nach oben, damit die Marge nie unter das Ziel rutscht
        return (vk - Decimal("0.99")).to_integral_value(rounding=ROUND_CEILING) + Decimal("0.99")
    return vk.quantize(CENT, rounding=ROUND_HALF_UP)


def vk_vorschlag_aus(ek_kalk, marge, auf_99=None):
    """VK = ek_kalk / (1 − marge); None ohne EK oder Marge."""
    if ek_kalk is None or marge is None or marge >= 1:
        return None
    return runden(ek_kalk / (1 - marge), auf_99)


def vk_vorschlag(artikel, mandant, stufe=1, bestand=None):
    return vk_vorschlag_aus(kalkulation(artikel, mandant, bestand).ek_kalk, marge_stufe(artikel, stufe))


def vk_uebernahme(vk_alt, vk_neu, vk_fix=False, senkung_grenze=SENKUNG_AUTO_GRENZE):
    """Was passiert mit dem neuen VK? 'fix' | 'neu' (kein VK bisher) | 'gleich' | 'erhoehung' | 'senkung_auto' | 'senkung_pruefen'.
    Erhöhung und Senkung bis zur Grenze (10 %) werden automatisch übernommen, größere Senkungen nur per Klick (Liste „VK-Senkung prüfen“)."""
    if vk_neu is None:
        return "gleich"
    if vk_fix:
        return "fix"
    if vk_alt is None:
        return "neu"
    if vk_neu == vk_alt:
        return "gleich"
    if vk_neu > vk_alt:
        return "erhoehung"
    senkung = (vk_alt - vk_neu) / vk_alt * 100
    return "senkung_auto" if senkung <= senkung_grenze else "senkung_pruefen"
