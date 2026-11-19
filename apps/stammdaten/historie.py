"""Historie eines Artikels aus dem Änderungsprotokoll (kern.AenderungsProtokoll): wer hat wann was von alt auf neu geändert.

Berücksichtigt werden der Artikel selbst, seine Mandantendaten (nur die des aktiven Mandanten) und seine Lieferantenzuordnungen."""
from django.apps import apps
from django.db import models

from apps.kern.models import AenderungsProtokoll

HISTORIE_MAX = 200
AKTIONEN = {"anlegen": "angelegt", "aendern": "geändert", "loeschen": "gelöscht"}
TABELLEN = {"stammdaten.Artikel": "", "stammdaten.ArtikelMandant": "Mandantendaten", "stammdaten.ArtikelLieferant": "Lieferant"}


def eingabe(artikel):
    """(Zeitpunkt, Benutzer) der Anlage: Zeitpunkt aus dem Artikel, Benutzer aus dem Protokoll (None, wenn nicht ermittelbar, z. B. Import)."""
    eintrag = (AenderungsProtokoll.objects.filter(tabelle="stammdaten.Artikel", datensatz_id=str(artikel.pk), aktion="anlegen")
               .select_related("benutzer").order_by("zeitpunkt").first())
    return artikel.angelegt_am, (eintrag.benutzer if eintrag else None)


def _wert(feld, wert, cache):
    """Lesbarer Wert: Fremdschlüssel als Text des Datensatzes, Wahrheitswerte als Ja/Nein, leer als „–“."""
    if wert in (None, "", []):
        return "–"
    if isinstance(wert, bool):
        return "ja" if wert else "nein"
    if isinstance(feld, models.ForeignKey):
        schluessel = (feld.related_model, wert)
        if schluessel not in cache:
            objekt = feld.related_model.objects.filter(pk=wert).first()
            cache[schluessel] = str(objekt) if objekt else f"#{wert}"
        return cache[schluessel]
    if isinstance(feld, models.ManyToManyField) and isinstance(wert, list):
        namen = []
        for pk in wert:
            schluessel = (feld.related_model, pk)
            if schluessel not in cache:
                objekt = feld.related_model.objects.filter(pk=pk).first()
                cache[schluessel] = str(objekt) if objekt else f"#{pk}"
            namen.append(cache[schluessel])
        return ", ".join(namen)
    return str(wert)


def _aenderungen(modell, vorher, nachher, cache):
    """Nur die Felder, die sich unterscheiden: [(Bezeichnung, alt, neu)]; bei Neuanlage und Löschung gibt es keine Einzelwerte."""
    if vorher is None or nachher is None:
        return []
    zeilen = []
    for name in sorted(set(vorher) | set(nachher)):
        alt, neu = vorher.get(name), nachher.get(name)
        if alt == neu:
            continue
        try:
            feld = modell._meta.get_field(name)
        except Exception:  # Feld inzwischen entfernt: Name als Bezeichnung
            feld = None
        bezeichnung = str(getattr(feld, "verbose_name", name)) if feld is not None else name
        zeilen.append((bezeichnung[:1].upper() + bezeichnung[1:], _wert(feld, alt, cache), _wert(feld, neu, cache)))
    return zeilen


def historie(artikel, mandant, grenze=HISTORIE_MAX):
    """Neueste Einträge zuerst, höchstens `grenze`: Zeitpunkt, Benutzer, Bereich, Aktion, Änderungen (Bezeichnung, alt, neu)."""
    abfragen = models.Q(tabelle="stammdaten.Artikel", datensatz_id=str(artikel.pk))
    mandantendaten = [str(pk) for pk in artikel.mandantendaten.filter(mandant=mandant).values_list("pk", flat=True)]
    if mandantendaten:
        abfragen |= models.Q(tabelle="stammdaten.ArtikelMandant", datensatz_id__in=mandantendaten)
    lieferanten = [str(pk) for pk in artikel.lieferanten.values_list("pk", flat=True)]
    if lieferanten:
        abfragen |= models.Q(tabelle="stammdaten.ArtikelLieferant", datensatz_id__in=lieferanten)
    cache = {}
    ergebnis = []
    for p in AenderungsProtokoll.objects.filter(abfragen).select_related("benutzer").order_by("-zeitpunkt", "-pk")[:grenze]:
        modell = apps.get_model(p.tabelle)
        ergebnis.append({"zeitpunkt": p.zeitpunkt, "benutzer": p.benutzer, "bereich": TABELLEN.get(p.tabelle, p.tabelle),
                         "aktion": AKTIONEN.get(p.aktion, p.aktion), "aenderungen": _aenderungen(modell, p.vorher, p.nachher, cache)})
    return ergebnis
