"""Artikel als Vorlage kopieren (Issue 15). Die Regeln, was übernommen wird, stehen nur hier; Artikelmaske (Lager) und
Verwaltung → Artikel rufen dieselben Funktionen. Gespeichert wird erst im Formular – diese Funktionen lesen die Vorlage
und legen nichts an (Ausnahme: `bilder_duplizieren`, das erst beim Speichern des neuen Artikels läuft)."""
import logging

from django.core.files.base import ContentFile
from django.db.models import Q

from .models import (MAX_BILDER, Artikel, ArtikelBild, ArtikelLieferant, ArtikelMandant, Artikeltyp, EkHerkunft, Hersteller, Stammnummer,
                     Steuersatz)

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


# ---- P → A: Preislistenartikel (Typ P) als neuen Lagerartikel (Typ A) übernehmen (Issue 24) ----------------------------------
# Anders als A → A bleiben EAN und Herstellernummer erhalten (dasselbe Produkt; über die Herstellernummer hängen die Preislisten dran).

def preislisten_gruppe(eintrag) -> list:
    """Alle Preislistenzeilen derselben Herstellernummer (alle Lieferanten); ohne Herstellernummer nur die Zeile selbst."""
    if not eintrag.herstellernummer:
        return [eintrag]
    return list(type(eintrag).objects.filter(herstellernummer__iexact=eintrag.herstellernummer).select_related("lieferant")
                .order_by("ek", "pk"))


def dubletten_zu_preisliste(eintrag) -> list:
    """Vorhandene A-Artikel mit gleicher Herstellernummer oder EAN (plus der bereits verknüpfte Artikel der Zeile)."""
    bedingung = Q(pk=eintrag.artikel_id) if eintrag.artikel_id else Q(pk__in=[])
    if eintrag.herstellernummer:
        bedingung |= Q(herstellernummer__iexact=eintrag.herstellernummer)
    if eintrag.ean:
        bedingung |= Q(ean=eintrag.ean)
    return list(Artikel.objects.filter(bedingung).filter(typ=Artikeltyp.A).order_by("nummer"))


def neuer_artikel_aus_preisliste(eintrag) -> Artikel:
    """Ungespeicherter Lagerartikel aus einer Preislistenzeile. Der günstigste Preislisten-EK der Gruppe wird Import-EK (Herkunft
    „Import“, maßgebend nach der EK-Rangfolge); VK1–VK3 bleiben leer und folgen EK + Aufschlag. Nicht online, Bestand 0."""
    steuer = Steuersatz.objects.filter(nummer=1).first() or Steuersatz.objects.order_by("-prozent").first()
    hersteller = Hersteller.objects.filter(name__iexact=eintrag.hersteller).first() if eintrag.hersteller else None
    artikel = Artikel(
        nummer=Stammnummer.vorschlag(Artikeltyp.A), typ=Artikeltyp.A, bezeichnung=eintrag.bezeichnung[:Artikel._meta.get_field("bezeichnung").max_length],
        hersteller=hersteller, herstellernummer=eintrag.herstellernummer, ean=eintrag.ean, gewicht_kg=eintrag.gewicht_kg,
        laenge_cm=eintrag.laenge_cm, breite_cm=eintrag.breite_cm, hoehe_cm=eintrag.hoehe_cm,
        steuersatz=steuer, online=False)
    preise = [z.ek for z in preislisten_gruppe(eintrag) if z.ek and z.ek > 0]
    if preise:
        artikel.durchschnitts_ek = min(preise)
        artikel.ek_herkunft_setzen(EkHerkunft.IMPORT)
    return artikel


def preisliste_verknuepfen(artikel: Artikel, eintrag) -> None:
    """Nach dem Speichern: alle Zeilen der Herstellernummer werden Lieferantenzeilen des Artikels und mit ihm verknüpft;
    fehlt der Hersteller im Stamm, wird er angelegt."""
    if artikel.hersteller_id is None and eintrag.hersteller:
        artikel.hersteller = Hersteller.objects.filter(name__iexact=eintrag.hersteller).first() or Hersteller.objects.create(
            name=eintrag.hersteller[:150])
        artikel.save(update_fields=["hersteller"])
    for z in preislisten_gruppe(eintrag):
        ArtikelLieferant.objects.get_or_create(
            lieferant=z.lieferant, bestellnummer=z.bestellnummer,
            defaults={"artikel": artikel, "ek": z.ek, "bestand": z.bestand, "lieferzeit_tage": z.lieferzeit_tage,
                      "preisliste": z.preisliste, "aktualisiert_am": z.aktualisiert_am})
        if z.artikel_id is None:
            z.artikel = artikel
            z.save(update_fields=["artikel"])


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
