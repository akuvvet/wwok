"""Artikel als Vorlage kopieren (Issue 15). Die Regeln, was übernommen wird, stehen nur hier; Artikelmaske (Lager) und
Verwaltung → Artikel rufen dieselben Funktionen. Gespeichert wird erst im Formular – diese Funktionen lesen die Vorlage
und legen nichts an (Ausnahme: `bilder_duplizieren`, das erst beim Speichern des neuen Artikels läuft)."""
import logging

from django.core.files.base import ContentFile

from .models import MAX_BILDER, Artikel, ArtikelBild, ArtikelMandant, Artikeltyp, Stammnummer

KOPIE_ZUSATZ = " (Kopie)"

# Übernommen. Bewusst NICHT: Nummer (neu aus dem Nummernkreis), EAN, Herstellernummer (hängt an den Preislisten), EOL/Nachfolger,
# Import-EK/Herkunft, Bestände, Seriennummern, ESD-Keys, Lieferantenzuordnung, Shop-Zuordnungen, Beleg- und Verkaufshistorie.
ARTIKEL_FELDER = [
    "typ", "kategorie", "leistungskategorie", "hersteller", "bezeichnung", "bezeichnung_erweitert", "mengeneinheit", "steuersatz",
    "garantie_monate", "laenge_cm", "breite_cm", "hoehe_cm", "gewicht_kg", "sperrgut", "zolltarifnummer", "esd_key", "kosten",
    "ek_manuell", "vk1", "vk2", "vk3", "aufschlag_vk1", "aufschlag_vk2", "aufschlag_vk3",
    "online", "kurzbeschreibung", "keywords", "meta_titel", "meta_description", "meta_keywords", "nicht_aktiv",
]
MANDANT_FELDER = ["ek2", "ek2_fix", "vk_fix1", "vk_fix2", "vk_fix3"]


def kopie_bezeichnung(bezeichnung: str) -> str:
    """Mit Zusatz „(Kopie)“, gekürzt auf die Feldlänge."""
    grenze = Artikel._meta.get_field("bezeichnung").max_length - len(KOPIE_ZUSATZ)
    return bezeichnung[:grenze] + KOPIE_ZUSATZ


def kopie_werte(quelle: Artikel) -> dict:
    """Feldname → Wert für den neuen Artikel; Fremdschlüssel als Schlüsselwert (Feldname + „_id“ ist der Schlüssel im Dict)."""
    werte = {}
    for name in ARTIKEL_FELDER:
        feld = Artikel._meta.get_field(name)
        werte[feld.attname] = getattr(quelle, feld.attname)
    werte["bezeichnung"] = kopie_bezeichnung(quelle.bezeichnung)
    return werte


def kopie_initial(quelle: Artikel) -> dict:
    """Wie `kopie_werte`, aber mit Feldnamen (Fremdschlüssel = Schlüsselwert) – für Formulare mit `initial` (Verwaltung)."""
    return {Artikel._meta.get_field(n).name: w for n, w in kopie_werte(quelle).items()}


def neuer_artikel(quelle: Artikel) -> Artikel:
    """Ungespeicherter Artikel mit den Werten der Vorlage und der nächsten freien Nummer des Nummernkreises."""
    werte = kopie_werte(quelle)
    if werte["typ"] == Artikeltyp.P:  # Lieferantenprodukte entstehen nur aus Preislisten; die Kopie wird ein Lagerartikel
        werte["typ"] = Artikeltyp.A
    return Artikel(nummer=Stammnummer.vorschlag(werte["typ"]), **werte)


def mandant_werte(quelle: Artikel, mandant) -> dict:
    """EK2 und Preis-Festlegungen des Mandanten. Lagerplatz, Bestandsgrenzen, Ø-EK und letzter EK gehören nicht zur Vorlage."""
    md = ArtikelMandant.objects.filter(artikel=quelle, mandant=mandant).first()
    return {n: getattr(md, n) for n in MANDANT_FELDER} if md else {}


def komponenten_werte(quelle: Artikel) -> list:
    """Komponenten einer Gruppe als (Artikel, Menge); bei anderen Typen leer."""
    if quelle.typ != Artikeltyp.G:
        return []
    return [(k.komponente, k.menge) for k in quelle.komponenten.select_related("komponente")]


def bild_duplizieren(bild: ArtikelBild, ziel: Artikel, position: int, alt_text: str | None = None) -> ArtikelBild | None:
    """Legt die Datei des Bildes für `ziel` neu an (eigene Datei, nicht verlinkt). Fehlt die Datei auf der Platte, wird das Bild
    übersprungen (None)."""
    try:
        with bild.bild.open("rb") as datei:
            inhalt = datei.read()
    except (OSError, ValueError) as fehler:
        logging.getLogger(__name__).warning("Bild %s nicht kopiert: %s", bild.bild.name, fehler)
        return None
    neu = ArtikelBild(artikel=ziel, position=position, alt_text=bild.alt_text if alt_text is None else alt_text)
    neu.bild.save(bild.bild.name.rsplit("/", 1)[-1], ContentFile(inhalt), save=False)
    neu.save()
    return neu


def bilder_duplizieren(quelle: Artikel, ziel: Artikel) -> int:
    """Hängt Kopien aller Bilder der Vorlage hinter die vorhandenen Bilder des Ziels (höchstens MAX_BILDER). Anzahl der Kopien."""
    belegt = ziel.bilder.count()
    anzahl = 0
    for bild in quelle.bilder.all():
        if belegt + anzahl >= MAX_BILDER:
            break
        if bild_duplizieren(bild, ziel, ziel.bilder.count() + 1):
            anzahl += 1
    return anzahl
