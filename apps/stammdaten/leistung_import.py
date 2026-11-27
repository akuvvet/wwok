"""Leistungsartikel (L) und Artikel (A) aus einer CSV- oder Excel-Datei einlesen. Die Spalten werden an der Kopfzeile erkannt.
Alles oder nichts: Bei einem Fehler in einer Zeile wird nichts importiert. Artikel-Import und Beispieldateien: Wunsch des Nutzers
26.11.2026."""
import io
import math
import re
import zipfile
from contextlib import nullcontext
from datetime import date, time, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from types import SimpleNamespace

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import DataError, IntegrityError, transaction
from django.db.models import Q

from apps.einkauf.assistent import format_erkennen
from apps.einkauf.models import Dateityp
from apps.einkauf.preislisten import tabelle, text, zahl

from .models import (BILD_FORMATE, MAX_BILDER, Artikel, ArtikelBild, Artikeltyp, EkHerkunft, Hersteller, Kategorie,
                     Leistungskategorie, Steuersatz)

SPALTEN = {
    "nummer": ("nummer", "nr", "artikelnummer", "artikelnr", "artnr", "leistungsnummer", "leistungsnr"),
    "bezeichnung": ("bezeichnung", "leistung", "benennung", "artikelbezeichnung", "name"),
    "bezeichnung_erweitert": ("erweitertebenennung", "erweitertebezeichnung", "beschreibung", "langtext"),
    "kategorie": ("kategorie", "leistungskategorie", "warengruppe", "gruppe"),
    "kosten": ("kosten", "aufwand", "ek", "ekpreis", "eknetto", "einkaufspreis"),
    "vk1": ("vk1", "vkpreis1", "vk1netto", "vk", "vkpreis", "vknetto", "verkaufspreis", "preis"),
    "vk2": ("vk2", "vkpreis2", "vk2netto"),
    "vk3": ("vk3", "vkpreis3", "vk3netto"),
    "mengeneinheit": ("mengeneinheit", "einheit", "me"),
    "steuersatz": ("steuersatz", "mwst", "ust", "steuer"),
    "nicht_aktiv": ("nichtaktiv", "inaktiv"),
}
# Artikel (Typ A): EK statt Kosten, dazu Hersteller, Kennungen, Gewicht, Garantie, Online und Kostenstelle
SPALTEN_A = {**{f: n for f, n in SPALTEN.items() if f != "kosten"},
             "kategorie": ("kategorie", "warengruppe", "gruppe", "kategorienummer"),
             "ek_manuell": ("ek", "ekpreis", "eknetto", "einkaufspreis", "ekpreisnetto"),
             "uvp": ("uvp", "uvpnetto"),
             "hersteller": ("hersteller", "marke", "herstellername"),
             "herstellernummer": ("herstellernummer", "herstellernr", "herstellerartikelnummer", "hstnr", "mpn"),
             "ean": ("ean", "gtin", "ean13", "barcode"),
             "gewicht_kg": ("gewicht", "gewichtkg"),
             "garantie_monate": ("garantie", "garantiemonate"),
             "online": ("online", "shop"),
             "kostenstelle": ("kostenstelle", "kost1")}
GELDFELDER = ("kosten", "vk1", "vk2", "vk3")
TEXTFELDER = ("bezeichnung", "bezeichnung_erweitert", "mengeneinheit")
GELDFELDER_A = ("ek_manuell", "vk1", "vk2", "vk3", "uvp")
TEXTFELDER_A = ("bezeichnung", "bezeichnung_erweitert", "mengeneinheit", "herstellernummer", "ean", "kostenstelle")
EINZEILIG = ("nummer", "bezeichnung", "kategorie", "mengeneinheit", "hersteller", "herstellernummer", "ean", "kostenstelle")
JA = {"1", "1.0", "1,0", "ja", "j", "x", "y", "yes", "true", "wahr"}
NEIN = {"0", "0.0", "0,0", "nein", "n", "no", "false", "falsch"}
DEUTSCH = re.compile(r"[1-9]\d{0,2}(\.\d{3})+(,\d{1,2})?|\d+(,\d{1,2})?")
MIT_PUNKT = re.compile(r"\d+\.\d{1,2}")
# Bilder beim Artikelimport (Wunsch des Nutzers 27.11.2026): Spalten „Abbildung 1“ … „Abbildung 10“ (wie im Mention-Export)
# mit Dateiname oder Pfad – zählt nur der Dateiname –, die Dateien selbst kommen als ZIP. Höchstens 2 MB pro Bild.
BILDSPALTEN = {pos: (f"abbildung{pos}", f"bild{pos}", *(("abbildung", "bild", "hauptbild") if pos == 1 else ()))
               for pos in range(1, MAX_BILDER + 1)}
MAX_IMPORT_BILD = 2 * 1024 * 1024
MAX_ZIP = 100 * 1024 * 1024
MAX_ZIP_EINTRAEGE = 5000
BILDENDUNGEN = (".jpg", ".jpeg", ".png", ".webp")
EXCEL_FEHLER = re.compile(r"#(N/A|NULL!|DIV/0!|VALUE!|REF!|NAME\?|NUM!)", re.IGNORECASE)


class ImportFehler(Exception):
    def __init__(self, meldung, fehler=()):
        super().__init__(meldung)
        self.fehler = list(fehler)


def bilddateiname(wert) -> str:
    """Aus „C:\\Mention\\Bilder\\21882.jpg“ oder „bilder/21882.jpg“ wird „21882.jpg“."""
    return text(wert).replace("\\", "/").rsplit("/", 1)[-1].strip()


class BilderZip:
    """Bilddateien aus einem hochgeladenen ZIP, gefunden über den Dateinamen (Groß-/Kleinschreibung egal, Ordner im ZIP egal).
    Gelesen wird erst, wenn ein Bild gebraucht wird, und nie mehr als 2 MB je Datei."""

    def __init__(self, datei):
        try:
            self.zip = zipfile.ZipFile(datei)
        except (zipfile.BadZipFile, OSError, ValueError):
            raise ImportFehler("Die Bilder-Datei ist kein gültiges ZIP-Archiv.")
        eintraege = [i for i in self.zip.infolist() if not i.is_dir() and not i.filename.startswith("__MACOSX/")]
        if len(eintraege) > MAX_ZIP_EINTRAEGE:
            self.zip.close()
            raise ImportFehler(f"Das ZIP enthält mehr als {MAX_ZIP_EINTRAEGE} Dateien.")
        self.dateien = {}  # kleiner Name -> ZipInfo, None = mehrfach vorhanden
        for info in eintraege:
            name = bilddateiname(info.filename)
            if name and not name.startswith("."):
                self.dateien[name.lower()] = None if name.lower() in self.dateien else info

    @classmethod
    def oeffnen(cls, datei):
        return cls(datei) if datei else nullcontext(None)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.zip.close()

    def holen(self, name) -> bytes:
        schluessel = name.lower()
        if schluessel not in self.dateien:
            raise ValueError(f"Bild {name} fehlt im ZIP.")
        info = self.dateien[schluessel]
        if info is None:
            raise ValueError(f"Bild {name} steht mehrfach im ZIP (verschiedene Ordner).")
        if info.file_size > MAX_IMPORT_BILD:
            raise ValueError(f"Bild {name} ist größer als 2 MB.")
        try:
            with self.zip.open(info) as f:
                daten = f.read(MAX_IMPORT_BILD + 1)
        except (zipfile.BadZipFile, OSError, RuntimeError, NotImplementedError):  # beschädigt oder verschlüsselt
            raise ValueError(f"Bild {name} lässt sich nicht aus dem ZIP lesen.")
        if len(daten) > MAX_IMPORT_BILD:
            raise ValueError(f"Bild {name} ist größer als 2 MB.")
        return daten


def bild_pruefen(name, daten):
    """Nur echte JPG-, PNG- oder WEBP-Bilder (Inhalt, nicht nur die Endung)."""
    from PIL import Image
    if not name.lower().endswith(BILDENDUNGEN):
        raise ValueError(f"Bild {name}: nur JPG, PNG oder WEBP.")
    try:
        with Image.open(io.BytesIO(daten)) as bild:
            format_ = bild.format
            bild.verify()
    except Exception:  # Pillow meldet kaputte oder zu große Bilder mit verschiedenen Ausnahmen
        raise ValueError(f"Bild {name}: keine gültige Bilddatei.")
    if format_ not in BILD_FORMATE:
        raise ValueError(f"Bild {name}: nur JPG, PNG oder WEBP.")


def bilder_uebernehmen(artikel, geplante_bilder, bilder) -> int:
    """Setzt die Bilder an ihre Position; ein vorhandenes Bild dort wird ersetzt, ein gleiches bleibt. Gibt die Anzahl
    neuer oder ersetzter Bilder zurück."""
    anzahl = 0
    for pos, name in geplante_bilder:
        daten = bilder.holen(name)
        vorhanden = ArtikelBild.objects.filter(artikel=artikel, position=pos).first()
        if vorhanden:
            try:
                with vorhanden.bild.open("rb") as f:
                    if f.read(MAX_IMPORT_BILD + 1) == daten:
                        continue
            except (OSError, ValueError):
                pass  # Datei fehlt auf der Platte: neu speichern
            vorhanden.delete()
        ArtikelBild(artikel=artikel, position=pos).bild.save(name, ContentFile(daten), save=True)
        anzahl += 1
    return anzahl


def kopfname(wert) -> str:
    return "".join(c for c in text(wert).lower() if c.isalnum())


def spalten_finden(kopf, typ=Artikeltyp.L) -> dict:
    namen = [kopfname(k) for k in kopf]
    spalten = {}
    for feld, moegliche in (SPALTEN_A if typ == Artikeltyp.A else SPALTEN).items():
        for name in moegliche:
            if name in namen:
                spalten[feld] = namen.index(name)
                break
    if typ == Artikeltyp.A:
        for pos, moegliche in BILDSPALTEN.items():
            treffer = next((namen.index(n) for n in moegliche if n in namen), None)
            if treffer is not None:
                spalten[f"bild{pos}"] = treffer
    fehlend = [n for n, feld in (("Nummer", "nummer"), ("Bezeichnung", "bezeichnung")) if feld not in spalten]
    if fehlend:
        raise ImportFehler(f"In der Kopfzeile fehlt: {', '.join(fehlend)}.")
    return spalten


def zelle_pruefen(feld, wert):
    """Lehnt ab, was Excel statt eines Werts liefert: Datum, Wahrheitswert, Fehlerzelle. Ein Zeilenumbruch in einem
    einzeiligen Feld kommt meist von einem offenen Anführungszeichen, das die Folgezeilen verschluckt."""
    if ((isinstance(wert, bool) and feld not in ("nicht_aktiv", "online")) or isinstance(wert, (date, time, timedelta))
            or (isinstance(wert, float) and not math.isfinite(wert)) or (isinstance(wert, str) and EXCEL_FEHLER.fullmatch(wert))):
        raise ValueError(f"Spalte {feld}: Inhalt ungültig: '{text(wert)}'")
    if feld in EINZEILIG and isinstance(wert, str) and "\n" in wert:
        raise ValueError(f"Spalte {feld}: Zeilenumbruch in der Zelle, vermutlich fehlt ein Anführungszeichen.")


def geld(wert) -> Decimal:
    """Betrag aus einer Zelle: Zahl aus Excel oder Text wie 1.234,56 € oder 12.50. Alles Mehrdeutige wird abgelehnt,
    damit kein Preis still um den Faktor 1000 falsch wird; negative Beträge sind nicht erlaubt."""
    if isinstance(wert, (int, float)) and not isinstance(wert, bool):
        betrag = Decimal(str(wert))
    else:
        t = re.sub(r"€|EUR|\s", "", text(wert))
        if DEUTSCH.fullmatch(t):
            betrag = Decimal(t.replace(".", "").replace(",", "."))
        elif MIT_PUNKT.fullmatch(t):
            betrag = Decimal(t)
        else:
            raise ValueError(f"Betrag ungültig: '{text(wert)}'")
    if not betrag.is_finite() or betrag < 0 or betrag >= Decimal("1E10"):
        raise ValueError(f"Betrag ungültig: '{text(wert)}'")
    return betrag.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def steuersatz_finden(wert, saetze):
    """Steuersatz über die ID (1 = 19 %, 2 = 7 %) oder die Prozentzahl (19, 7 %)."""
    treffer = Steuersatz.finden(text(wert), saetze)
    if treffer is None:
        raise ValueError(f"Steuersatz unbekannt: '{text(wert)}'")
    return treffer


def datei_lesen(dateiname: str, inhalt: bytes) -> list:
    try:
        format_ = format_erkennen(dateiname, inhalt)
        if format_["dateityp"] == Dateityp.XML:
            raise ImportFehler("Bitte eine CSV- oder Excel-Datei verwenden.")
        return tabelle(SimpleNamespace(**format_), inhalt)
    except ImportFehler:
        raise
    except Exception:  # Bibliotheken für Excel und CSV melden kaputte Dateien mit jeweils eigenen Ausnahmen
        raise ImportFehler("Die Datei lässt sich nicht lesen. Bitte eine CSV- oder Excel-Datei verwenden.")


def kategorie_holen(wert, gemerkt, neue):
    """Vorhandene Kategorie über die Nummer, sonst den Namen (Vergleich durch die Datenbank, wie bei der Eindeutigkeit), oder neu
    anlegen. Eine reine Zahl ist die Mention-Nummer; der Name lässt sich danach in der Verwaltung ändern."""
    schluessel = wert.lower()
    if schluessel not in gemerkt:
        kategorie = (Leistungskategorie.objects.filter(nummer__iexact=wert).first()
                     or Leistungskategorie.objects.filter(name__iexact=wert).first())
        if kategorie is None:
            kategorie = Leistungskategorie.objects.create(name=wert, nummer=wert if wert.isdigit() and len(wert) <= 20 else None)
            neue.append(kategorie)
        gemerkt[schluessel] = kategorie
    return gemerkt[schluessel]


def kategorie_finden(wert):
    """Artikelkategorie (Typ A) über die Nummer, sonst den Namen. Gleicher Name an mehreren Stellen im Baum ist mehrdeutig;
    eine neue Kategorie wird erst beim Speichern angelegt (oberste Ebene, offline)."""
    treffer = Kategorie.objects.filter(nummer__iexact=wert).first()
    if treffer is None:
        namen = list(Kategorie.objects.filter(name__iexact=wert)[:2])
        if len(namen) > 1:
            raise ValueError(f"Kategorie „{wert}“ gibt es mehrfach – bitte die Kategorienummer angeben.")
        treffer = namen[0] if namen else None
    return treffer


def dezimal(wert, feld, stellen=3) -> Decimal:
    """Gewicht und Garantie: Zahl aus Excel oder Text mit Komma oder Punkt; nicht negativ."""
    try:
        if isinstance(wert, (int, float)) and not isinstance(wert, bool):
            d = Decimal(str(wert))
        else:
            t = re.sub(r"\s|kg", "", text(wert), flags=re.IGNORECASE)
            d = Decimal(t.replace(".", "").replace(",", ".") if "," in t else t)
    except InvalidOperation:
        raise ValueError(f"Spalte {feld}: keine Zahl: '{text(wert)}'")
    if not d.is_finite() or d < 0 or d >= Decimal("1E6"):
        raise ValueError(f"Spalte {feld}: Wert ungültig: '{text(wert)}'")
    return d.quantize(Decimal(1).scaleb(-stellen), rounding=ROUND_HALF_UP)


def ja_nein(zelle, feld):
    wert = text(zelle(feld)).lower()
    if wert not in JA | NEIN:
        raise ValueError(f"Spalte {feld}: Inhalt ungültig: '{wert}'")
    return wert in JA


def _zeile_uebernehmen(a, zelle, saetze, typ=Artikeltyp.L):
    """Trägt die gefüllten Zellen in den Artikel ein; leere Zellen lassen den vorhandenen Wert stehen."""
    for feld in (TEXTFELDER_A if typ == Artikeltyp.A else TEXTFELDER):
        if zelle(feld):
            setattr(a, feld, text(zelle(feld)))
    for feld in (GELDFELDER_A if typ == Artikeltyp.A else GELDFELDER):
        if zelle(feld) != "":
            setattr(a, feld, geld(zelle(feld)))
    if zelle("nicht_aktiv") != "":
        a.nicht_aktiv = ja_nein(zelle, "nicht_aktiv")
    if typ == Artikeltyp.A:
        if a.ean and not (a.ean.isdigit() and len(a.ean) in (8, 12, 13, 14)):
            raise ValueError(f"Spalte ean: 8, 12, 13 oder 14 Ziffern erwartet: '{a.ean}'")
        if zelle("gewicht_kg") != "":
            a.gewicht_kg = dezimal(zelle("gewicht_kg"), "gewicht")
        if zelle("garantie_monate") != "":
            monate = dezimal(zelle("garantie_monate"), "garantie", 0)
            if monate > 600:
                raise ValueError(f"Spalte garantie: Wert ungültig: '{text(zelle('garantie_monate'))}'")
            a.garantie_monate = int(monate)
        if zelle("online") != "":
            a.online = ja_nein(zelle, "online")
    if zelle("steuersatz") != "":
        a.steuersatz = steuersatz_finden(zelle("steuersatz"), saetze)


def importieren(dateiname: str, inhalt: bytes, typ=Artikeltyp.L, bilder=None, statistik=None):
    """Gibt (neu, geändert, unverändert, neue Kategorien) zurück. typ: L = Leistungsartikel, A = Artikel (Lagerartikel).
    bilder: BilderZip für die Spalten „Abbildung 1–10“; ohne ZIP werden diese Spalten übersprungen. In statistik (dict)
    stehen danach „bilder“ (neu oder ersetzt) und „ohne_zip“ (übersprungene Bildangaben)."""
    statistik = {} if statistik is None else statistik
    statistik.update(bilder=0, ohne_zip=0)
    zeilen = datei_lesen(dateiname, inhalt)
    if not zeilen:
        raise ImportFehler("Die Datei ist leer.")
    spalten = spalten_finden(zeilen[0], typ)
    saetze = list(Steuersatz.objects.all())
    standard = Steuersatz.standard()

    def zellen(zeile):
        def zelle(feld):
            i = spalten.get(feld)
            wert = zeile[i] if i is not None and i < len(zeile) else ""
            return wert.strip() if isinstance(wert, str) else ("" if wert is None else wert)
        return zelle

    fehler, gesehen, geplant = [], set(), []
    for nr, zeile in enumerate(zeilen[1:], start=2):
        if not any(text(w) for w in zeile):
            continue
        zelle = zellen(zeile)
        if any(text(w) for w in zeile[len(zeilen[0]):]):
            fehler.append(f"Zeile {nr}: Mehr Spalten als in der Kopfzeile. Bitte das Trennzeichen prüfen.")
            continue
        try:
            for feld in spalten:
                zelle_pruefen(feld, zelle(feld))
        except ValueError as e:
            fehler.append(f"Zeile {nr}: {e}")
            continue
        nummer = text(zelle("nummer"))
        if not nummer:
            fehler.append(f"Zeile {nr}: Die Nummer fehlt.")
            continue
        a = Artikel.objects.filter(nummer__iexact=nummer).first()  # die Datenbank vergleicht wie bei ihrer Eindeutigkeit
        schluessel = a.nummer.lower() if a else nummer.lower()
        if schluessel in gesehen:
            fehler.append(f"Zeile {nr}: Nummer {nummer} steht doppelt in der Datei.")
            continue
        gesehen.add(schluessel)
        neu = a is None
        if not neu and a.typ != typ:
            fehler.append(f"Zeile {nr}: {nummer} ist schon als {a.get_typ_display()} angelegt.")
            continue
        if neu:
            a = Artikel(nummer=nummer, typ=typ, steuersatz=standard)  # ohne Angabe: Standard ID 1
        felder = ((*TEXTFELDER_A, *GELDFELDER_A, "gewicht_kg", "garantie_monate", "online") if typ == Artikeltyp.A
                  else (*TEXTFELDER, *GELDFELDER))
        vorher = {f: getattr(a, f) for f in (*felder, "nicht_aktiv", "steuersatz_id")}
        try:
            _zeile_uebernehmen(a, zelle, saetze, typ)
            kategorie = text(zelle("kategorie"))
            hersteller = text(zelle("hersteller")) if typ == Artikeltyp.A else ""
            if typ == Artikeltyp.A and kategorie:
                kategorie_finden(kategorie)  # mehrdeutig: schon hier melden
            if len(hersteller) > 150:
                raise ValueError("Der Herstellername ist zu lang.")
            if neu and not a.bezeichnung:
                raise ValueError("Die Bezeichnung fehlt.")
            if neu and a.steuersatz_id is None:
                raise ValueError("Es ist noch kein Steuersatz angelegt.")
            a.clean_fields(exclude=["steuersatz", "leistungskategorie"])
        except ValueError as e:
            fehler.append(f"Zeile {nr}: {e}")
            continue
        except ValidationError as e:
            fehler.append(f"Zeile {nr}: " + " ".join(f"{a._meta.get_field(f).verbose_name}: {' '.join(m)}"
                                                     for f, m in e.message_dict.items()))
            continue
        if len(kategorie) > 150:
            fehler.append(f"Zeile {nr}: Der Kategoriename ist zu lang.")
            continue
        geplante_bilder = []
        try:
            for pos in range(1, MAX_BILDER + 1):
                name = bilddateiname(zelle(f"bild{pos}")) if f"bild{pos}" in spalten else ""
                if not name:
                    continue
                if bilder is None:
                    statistik["ohne_zip"] += 1
                    continue
                bild_pruefen(name, bilder.holen(name))
                geplante_bilder.append((pos, name))
        except ValueError as e:
            fehler.append(f"Zeile {nr}: {e}")
            continue
        geaendert = neu or any(getattr(a, f) != w for f, w in vorher.items())
        if typ == Artikeltyp.A and a.ek_manuell != vorher["ek_manuell"]:
            a.ek_herkunft_setzen(EkHerkunft.MANUELL)
        geplant.append((a, kategorie, hersteller, neu, geaendert, geplante_bilder))
    if fehler:
        raise ImportFehler("Nichts importiert. Bitte diese Zeilen in der Datei korrigieren:", fehler)

    zaehler = {"neu": 0, "geaendert": 0, "gleich": 0}
    kategorien, neue_kategorien = {}, []
    try:
        with transaction.atomic():
            hersteller_ = {}
            for a, kategorie, hersteller, neu, geaendert, geplante_bilder in geplant:
                if kategorie and typ == Artikeltyp.A:
                    eintrag = kategorien.get(kategorie.lower()) or kategorie_finden(kategorie)
                    if eintrag is None:
                        eintrag = Kategorie.objects.create(name=kategorie)  # neu: oberste Ebene, offline
                        neue_kategorien.append(eintrag)
                    kategorien[kategorie.lower()] = eintrag
                    if eintrag.pk != a.kategorie_id:
                        a.kategorie, geaendert = eintrag, True
                elif kategorie:
                    eintrag = kategorie_holen(kategorie, kategorien, neue_kategorien)
                    if eintrag.pk != a.leistungskategorie_id:
                        a.leistungskategorie, geaendert = eintrag, True
                if hersteller:
                    if hersteller.lower() not in hersteller_:
                        hersteller_[hersteller.lower()] = (Hersteller.objects.filter(name__iexact=hersteller).first()
                                                           or Hersteller.objects.create(name=hersteller))
                    if hersteller_[hersteller.lower()].pk != a.hersteller_id:
                        a.hersteller, geaendert = hersteller_[hersteller.lower()], True
                if geaendert:
                    a.save()
                if geplante_bilder:
                    anzahl = bilder_uebernehmen(a, geplante_bilder, bilder)
                    statistik["bilder"] += anzahl
                    geaendert = geaendert or anzahl > 0
                zaehler["neu" if neu else "geaendert" if geaendert else "gleich"] += 1
    except (IntegrityError, DataError):  # z. B. Büro und Buro: für die Datenbank kann das derselbe Eintrag sein
        raise ImportFehler("Nichts importiert: Zwei Nummern oder Kategorien gelten für die Datenbank als gleich. Bitte die Schreibweise in der Datei prüfen.")
    return zaehler["neu"], zaehler["geaendert"], zaehler["gleich"], len(neue_kategorien)
