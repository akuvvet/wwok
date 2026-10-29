from decimal import Decimal

from django.db import transaction
from django.db.models import F, Sum

from apps.verkauf.models import Belegart

from .models import ESDKey, ESDVergabe, KeyStatus, VergabeStatus


def zeilen(text: str) -> list:
    """Eine Zeile je Key; optional 'Key;Anzahl' (Volumenkey) oder 'Key<Tab>Anzahl'."""
    ergebnis = []
    for z in (text or "").splitlines():
        z = z.strip()
        if not z:
            continue
        teile = [t.strip() for t in z.replace("\t", ";").split(";")]
        anzahl = int(teile[1]) if len(teile) > 1 and teile[1].isdigit() else None
        ergebnis.append((teile[0], anzahl))
    return ergebnis


@transaction.atomic
def importieren(mandant, artikel, text, max_vergaben=1):
    """Legt Keys an; bereits vorhandene Keys werden gezählt, nicht doppelt angelegt. Rückgabe: (neu, doppelt)."""
    if max_vergaben < 1:
        raise ValueError("Die maximale Vergabeanzahl muss mindestens 1 sein.")
    neu = doppelt = 0
    for key, anzahl in zeilen(text):
        _, angelegt = ESDKey.objects.get_or_create(
            mandant=mandant, artikel=artikel, key=key, defaults={"max_vergaben": anzahl or max_vergaben})
        neu += angelegt
        doppelt += not angelegt
    return neu, doppelt


class CsvFehler(ValueError):
    def __init__(self, fehler):
        self.fehler = fehler
        super().__init__(f"{len(fehler)} Fehler in der Datei – es wurde nichts importiert.")


def csv_importieren(inhalt: bytes):
    """CSV mit vier Spalten: Mandant (Kurzname); Artikelnummer; Key; Anzahl Vergaben. Alles oder nichts:
    bei Fehlern (CsvFehler mit Zeilenmeldungen) wird nichts angelegt. Rückgabe: (neu, doppelt, artikel_anzahl)."""
    import csv

    from apps.kern.models import Mandant
    from apps.stammdaten.models import Artikel

    from .views import software_kategorien

    try:
        text = inhalt.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = inhalt.decode("cp1252")
    zeilen_ = [z for z in text.splitlines() if z.strip()]
    if not zeilen_:
        raise ValueError("Die Datei ist leer.")
    trenner = ";" if zeilen_[0].count(";") >= zeilen_[0].count(",") else ","
    mandanten = {m.kurzname.lower(): m for m in Mandant.objects.all()}
    kategorien = software_kategorien()
    artikel_cache, fehler, eintraege, gesehen = {}, [], [], set()
    for nr, teile in enumerate(csv.reader(zeilen_, delimiter=trenner), start=1):
        teile = [t.strip() for t in teile]
        if nr == 1 and (len(teile) < 4 or not teile[3].isdigit()):
            continue  # Kopfzeile
        if len(teile) < 4 or not all(teile[:4]):
            fehler.append(f"Zeile {nr}: vier Spalten erwartet (Mandant; Artikelnummer; Key; Anzahl).")
            continue
        mandant = mandanten.get(teile[0].lower())
        if mandant is None:
            fehler.append(f"Zeile {nr}: Mandant „{teile[0]}“ unbekannt.")
            continue
        if not teile[3].isdigit() or not 1 <= int(teile[3]) <= 100000:
            fehler.append(f"Zeile {nr}: Anzahl „{teile[3]}“ ist keine ganze Zahl ab 1.")
            continue
        if len(teile[2]) > 250:
            fehler.append(f"Zeile {nr}: Key ist länger als 250 Zeichen.")
            continue
        if teile[1] not in artikel_cache:
            artikel_cache[teile[1]] = Artikel.objects.filter(nummer=teile[1], typ="A").first()
        artikel = artikel_cache[teile[1]]
        if artikel is None:
            fehler.append(f"Zeile {nr}: Artikel „{teile[1]}“ nicht gefunden (Typ A).")
            continue
        if kategorien is not None and artikel.kategorie_id not in kategorien:
            fehler.append(f"Zeile {nr}: Artikel „{teile[1]}“ gehört nicht zur Kategorie Software.")
            continue
        schluessel = (mandant.pk, artikel.pk, teile[2])
        if schluessel in gesehen:
            fehler.append(f"Zeile {nr}: Key {teile[2]} steht in der Datei mehrfach.")
            continue
        gesehen.add(schluessel)
        eintraege.append((mandant, artikel, teile[2], int(teile[3])))
    if fehler:
        raise CsvFehler(fehler)
    if not eintraege:
        raise ValueError("Die Datei enthält keine Keys.")
    neu = doppelt = 0
    with transaction.atomic():
        for mandant, artikel, key, anzahl in eintraege:
            if not artikel.esd_key:
                artikel.esd_key = True
                artikel.save()
            _, angelegt = ESDKey.objects.get_or_create(mandant=mandant, artikel=artikel, key=key,
                                                       defaults={"max_vergaben": anzahl})
            neu += angelegt
            doppelt += not angelegt
    return neu, doppelt, len({e[1].pk for e in eintraege})


def bestand(mandant, artikel) -> dict:
    keys = ESDKey.objects.filter(mandant=mandant, artikel=artikel)
    aktiv = keys.filter(status=KeyStatus.AKTIV)
    frei = aktiv.aggregate(s=Sum(F("max_vergaben") - F("vergeben")))["s"] or 0
    return {"keys": keys.count(), "frei": max(frei, 0), "gesperrt": keys.filter(status=KeyStatus.GESPERRT).count(),
            "vergeben": keys.aggregate(s=Sum("vergeben"))["s"] or 0}


def hat_key_vom_kunden(key, kunde) -> bool:
    return ESDVergabe.objects.filter(key=key, kunde=kunde, status=VergabeStatus.AKTIV).exists()


@transaction.atomic
def zuordnen(position, anzahl, erlaube_doppelt=False, bemerkung="", benutzer=None) -> list:
    """Vergibt Keys an die Position; ein Key geht nie zweimal an denselben Kunden, außer der Verkäufer entscheidet
    ausdrücklich anders (erlaube_doppelt, mit Begründung)."""
    beleg, artikel = position.beleg, position.artikel
    if artikel is None or not artikel.esd_key:
        raise ValueError("Der Artikel ist nicht als ESD-Key-Artikel gekennzeichnet.")
    if erlaube_doppelt and not bemerkung.strip():
        raise ValueError("Für die erneute Vergabe an denselben Kunden bitte eine Begründung angeben.")
    vergaben = []
    ausgeschlossen = 0
    kandidaten = list(ESDKey.objects.select_for_update().filter(
        mandant=beleg.mandant, artikel=artikel, status=KeyStatus.AKTIV, vergeben__lt=F("max_vergaben")).order_by("-vergeben", "id"))
    for _ in range(anzahl):
        key = None
        for k in kandidaten:
            if k.frei <= 0:
                continue
            if not erlaube_doppelt and hat_key_vom_kunden(k, beleg.kunde):
                ausgeschlossen += 1
                continue
            key = k
            break
        if key is None:
            hinweis = (" Ein Key wäre nur durch erneute Vergabe an denselben Kunden möglich – das muss der Verkäufer ausdrücklich bestätigen."
                       if ausgeschlossen else "")
            raise ValueError(f"Nicht genug ESD-Keys für {artikel.nummer} ({len(vergaben)} von {anzahl} verfügbar).{hinweis}")
        key.vergeben += 1
        key.save(update_fields=["vergeben"])
        vergaben.append(ESDVergabe.objects.create(key=key, kunde=beleg.kunde, beleg=beleg, position=position,
                                                  bemerkung=bemerkung[:300], benutzer=benutzer))
    return vergaben


def vergeben_menge(position) -> int:
    """Bereits aktive Vergaben der Position bzw. der Quellposition (Lieferschein → Rechnung)."""
    n = position.esd_vergaben.filter(status=VergabeStatus.AKTIV).count()
    if position.quellposition_id:
        n = max(n, position.quellposition.esd_vergaben.filter(status=VergabeStatus.AKTIV).count())
    return n


@transaction.atomic
def beleg_zuordnen(beleg, benutzer=None) -> int:
    """Automatische Zuordnung beim Verkauf (Lieferschein oder Rechnung); bereits versorgte Positionen werden übersprungen."""
    if beleg.art not in (Belegart.LIEFERSCHEIN, Belegart.RECHNUNG):
        return 0
    anzahl = 0
    for p in beleg.positionen.select_related("artikel", "quellposition"):
        if p.artikel_id and p.artikel.esd_key:
            fehlt = int(p.menge) - vergeben_menge(p)
            if fehlt > 0:
                anzahl += len(zuordnen(p, fehlt, benutzer=benutzer))
    return anzahl


@transaction.atomic
def rueckgabe(vergabe, entscheidung, benutzer=None):
    """Retoure: der Key wird freigegeben (wieder vergebbar) oder gesperrt (nie wieder vergeben)."""
    if vergabe.status != VergabeStatus.AKTIV:
        raise ValueError("Diese Vergabe ist bereits zurückgenommen.")
    key = ESDKey.objects.select_for_update().get(pk=vergabe.key_id)
    if entscheidung == "freigeben":
        key.vergeben = max(key.vergeben - 1, 0)
        vergabe.status = VergabeStatus.FREIGEGEBEN
    elif entscheidung == "sperren":
        key.status = KeyStatus.GESPERRT
        vergabe.status = VergabeStatus.GESPERRT
    else:
        raise ValueError("Bitte freigeben oder sperren wählen.")
    key.save()
    vergabe.save()


def keys_text(beleg) -> list:
    """Aktive Keys des Belegs je Position als (Bezeichnung, [Keys])."""
    ergebnis = {}
    for v in beleg.esd_vergaben.filter(status=VergabeStatus.AKTIV).select_related("key", "position"):
        name = v.position.bezeichnung if v.position_id else v.key.artikel.bezeichnung
        ergebnis.setdefault(name, []).append(v.key.key)
    return list(ergebnis.items())
