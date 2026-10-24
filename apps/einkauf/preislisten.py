"""Preislistenimport der Lieferanten (Pflichtenheft Abschnitt 7): CSV/TXT, Excel, XML; Upload, FTP, SFTP oder URL."""
import csv
import ftplib
import io
import os
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone

from apps.stammdaten.models import Artikel, ArtikelLieferant, Hersteller, Steuersatz

from .models import Dateityp, Preislistenimport, Preislistenprofil, Quelle

FELDER = ("bestellnummer", "herstellernummer", "ean", "bezeichnung", "hersteller", "kategorie", "ek",
          "bestand", "lieferzeit", "gewicht")


class PreislistenFehler(Exception):
    pass


# ---- Abruf ------------------------------------------------------------------------------------------

def passwort(profil) -> str:
    return os.environ.get(profil.passwort_variable, "")


def abrufen(profil):
    """Holt die Datei vom FTP-/SFTP-Server bzw. der URL; das Passwort steht in der .env (PREISLISTE_PW_<ID>)."""
    if profil.quelle == Quelle.FTP:
        puffer = io.BytesIO()
        try:
            with ftplib.FTP(timeout=60) as ftp:
                ftp.connect(profil.adresse, profil.port or 21)
                ftp.login(profil.benutzer or "anonymous", passwort(profil))
                ftp.retrbinary(f"RETR {profil.pfad}", puffer.write)
        except (ftplib.all_errors, OSError) as fehler:
            raise PreislistenFehler(f"FTP-Abruf fehlgeschlagen: {fehler}")
        return os.path.basename(profil.pfad), puffer.getvalue()
    if profil.quelle == Quelle.SFTP:
        try:
            import paramiko
            transport = paramiko.Transport((profil.adresse, profil.port or 22))
            transport.connect(username=profil.benutzer, password=passwort(profil))
            sftp = paramiko.SFTPClient.from_transport(transport)
            with sftp.open(profil.pfad, "rb") as datei:
                inhalt = datei.read()
            sftp.close()
            transport.close()
        except Exception as fehler:
            raise PreislistenFehler(f"SFTP-Abruf fehlgeschlagen: {fehler}")
        return os.path.basename(profil.pfad), inhalt
    if profil.quelle == Quelle.URL:
        url = profil.url_adresse
        if not url.lower().startswith(("http://", "https://")):
            raise PreislistenFehler("Bitte die vollständige Adresse (https://…) im Feld „FTP-Server bzw. URL“ eintragen.")
        try:
            anfrage = urllib.request.Request(url)
            if profil.benutzer:
                if not passwort(profil):
                    raise PreislistenFehler(f"Das Passwort fehlt: {profil.passwort_variable} in der .env eintragen "
                                            "und den Dienst neu starten.")
                import base64
                anfrage.add_header("Authorization", "Basic " + base64.b64encode(
                    f"{profil.benutzer}:{passwort(profil)}".encode()).decode())
            with urllib.request.urlopen(anfrage, timeout=300) as antwort:
                return os.path.basename(url.split("?")[0].rstrip("/")) or "preisliste.csv", antwort.read()
        except PreislistenFehler:
            raise
        except urllib.error.HTTPError as fehler:
            hinweis = " – Benutzer oder Passwort prüfen." if fehler.code in (401, 403) else ""
            raise PreislistenFehler(f"Download fehlgeschlagen: Der Server antwortet mit {fehler.code} {fehler.reason}{hinweis}")
        except Exception as fehler:
            raise PreislistenFehler(f"Download fehlgeschlagen: {fehler}")
    raise PreislistenFehler("Dieses Profil wird per Datei-Upload gefüllt.")


# ---- Lesen ------------------------------------------------------------------------------------------

def spaltenindex(kopf, angabe: str, name: str):
    angabe = (angabe or "").strip()
    if not angabe:
        return None
    if angabe.isdigit():
        return int(angabe) - 1
    for i, k in enumerate(kopf or []):
        if str(k).strip().lower() == angabe.lower():
            return i
    raise PreislistenFehler(f"Spalte '{angabe}' ({name}) nicht in der Datei gefunden.")


def tabelle(profil, inhalt: bytes) -> list:
    """Zeilen als Listen (CSV, Excel)."""
    if profil.dateityp == Dateityp.CSV:
        try:
            text = inhalt.decode(profil.zeichensatz)
        except UnicodeDecodeError:
            raise PreislistenFehler(f"Die Datei lässt sich nicht als {profil.zeichensatz} lesen (Zeichensatz im Profil prüfen).")
        trenner = "\t" if profil.trennzeichen in ("\\t", "tab", "TAB") else (profil.trennzeichen or ";")
        kwargs = {"quotechar": profil.textbegrenzer} if profil.textbegrenzer else {"quoting": csv.QUOTE_NONE}
        return [z for z in csv.reader(io.StringIO(text), delimiter=trenner, **kwargs)]
    if profil.dateityp == Dateityp.XLSX:
        from openpyxl import load_workbook
        blatt = load_workbook(io.BytesIO(inhalt), read_only=True, data_only=True).worksheets[0]
        return [["" if v is None else v for v in z] for z in blatt.iter_rows(values_only=True)]
    if profil.dateityp == Dateityp.XLS:
        import xlrd
        blatt = xlrd.open_workbook(file_contents=inhalt, encoding_override="cp1252").sheet_by_index(0)
        return [blatt.row_values(i) for i in range(blatt.nrows)]
    raise PreislistenFehler("Unbekannter Dateityp.")


def text(wert) -> str:
    if isinstance(wert, float):
        return str(int(wert)) if wert == int(wert) else str(wert)
    return "" if wert is None else str(wert).strip()


def zeilen_lesen(profil, inhalt: bytes) -> list:
    """Liste von (zeilennummer, {feld: text}) gemäß Feldzuordnung."""
    zuordnung = {f: getattr(profil, f"sp_{f}") for f in FELDER}
    ergebnis = []
    if profil.dateityp == Dateityp.XML:
        if not profil.xml_satz:
            raise PreislistenFehler("Für XML bitte das Element je Artikel im Profil angeben.")
        try:
            wurzel = ET.fromstring(inhalt)
        except ET.ParseError as fehler:
            raise PreislistenFehler(f"XML nicht lesbar: {fehler}")
        for nr, satz in enumerate((e for e in wurzel.iter() if e.tag.rsplit("}", 1)[-1] == profil.xml_satz), start=1):
            werte = {k.rsplit("}", 1)[-1]: (v or "") for k, v in satz.attrib.items()}
            werte.update({c.tag.rsplit("}", 1)[-1]: (c.text or "").strip() for c in satz})
            ergebnis.append((nr, {f: text(werte.get(a, "")) for f, a in zuordnung.items() if a}))
        return ergebnis
    zeilen = tabelle(profil, inhalt)
    kopf = zeilen[0] if profil.kopfzeile and zeilen else None
    index = {f: spaltenindex(kopf, a, f) for f, a in zuordnung.items()}
    if index["bestellnummer"] is None or index["ek"] is None:
        raise PreislistenFehler("Bestellnummer und EK müssen zugeordnet sein.")
    for nr, z in enumerate(zeilen, start=1):
        if nr < max(profil.erste_datenzeile, 2 if profil.kopfzeile else 1) or not any(text(c) for c in z):
            continue
        ergebnis.append((nr, {f: text(z[i]) if i is not None and i < len(z) else "" for f, i in index.items()}))
    return ergebnis


def zahl(wert: str, dezimal: str) -> Decimal:
    t = wert.replace("€", "").replace("EUR", "").replace(" ", "").replace("\xa0", "")
    t = t.replace(".", "").replace(",", ".") if dezimal == "," and ("," in t or t.count(".") > 1) else t.replace(",", "") if dezimal == "." else t
    try:
        return Decimal(t)
    except InvalidOperation:
        raise ValueError(f"Zahl nicht lesbar: '{wert}'")


# ---- Import -----------------------------------------------------------------------------------------

def artikel_finden(zeile: dict):
    if zeile.get("herstellernummer"):
        a = Artikel.objects.filter(herstellernummer__iexact=zeile["herstellernummer"]).first()
        if a:
            return a
    if zeile.get("ean"):
        return Artikel.objects.filter(ean=zeile["ean"]).first()
    return None


def vorschau(profil, inhalt: bytes, anzahl: int = 15) -> dict:
    """Erste Zeilen mit Zuordnung und Abgleichergebnis anzeigen, ohne etwas zu speichern."""
    zeilen = zeilen_lesen(profil, inhalt)
    zeilen_vorschau = []
    for nr, z in zeilen[:anzahl]:
        al = ArtikelLieferant.objects.filter(lieferant=profil.lieferant, bestellnummer=z.get("bestellnummer", "")).select_related("artikel").first()
        artikel = al.artikel if al else artikel_finden(z)
        try:
            ek = zahl(z.get("ek", ""), profil.dezimaltrennzeichen)
        except ValueError as fehler:
            ek = str(fehler)
        zeilen_vorschau.append({"nr": nr, "daten": z, "ek": ek, "artikel": artikel,
                                "ergebnis": "bekannt" if artikel else ("wird angelegt" if profil.neue_artikel else "wird übersprungen")})
    return {"gesamt": len(zeilen), "zeilen": zeilen_vorschau}


def importieren(profil, inhalt: bytes, dateiname: str = "", benutzer=None) -> Preislistenimport:
    zeilen = zeilen_lesen(profil, inhalt)
    protokoll = Preislistenimport(profil=profil, dateiname=dateiname[:200], gelesen=len(zeilen), benutzer=benutzer)
    fehler = []
    jetzt = timezone.now()
    steuer = Steuersatz.objects.filter(nummer=1).first() or Steuersatz.objects.order_by("-prozent").first()
    hersteller = {}
    for h in Hersteller.objects.order_by("kennzeichen"):
        hersteller.setdefault(h.name.lower(), h)
    for nr, z in zeilen:
        try:
            with transaction.atomic():
                ergebnis = zeile_importieren(profil, z, jetzt, steuer, hersteller)
            setattr(protokoll, ergebnis, getattr(protokoll, ergebnis) + 1)
        except (ValueError, PreislistenFehler) as grund:
            protokoll.fehler += 1
            if len(fehler) < 200:
                fehler.append({"zeile": nr, "grund": str(grund), "daten": z})
    protokoll.fehlerzeilen = fehler
    protokoll.meldung = f"{protokoll.neu} neu, {protokoll.geaendert} geändert, {protokoll.unveraendert} unverändert, " \
                        f"{protokoll.uebersprungen} übersprungen, {protokoll.fehler} Fehler"
    protokoll.save()
    profil.letzter_lauf = jetzt
    profil.save(update_fields=["letzter_lauf"])
    return protokoll


def zeile_importieren(profil, z: dict, jetzt, steuer, hersteller) -> str:
    """Gibt 'neu', 'geaendert', 'unveraendert' oder 'uebersprungen' zurück."""
    bestellnr = z.get("bestellnummer", "").strip()
    if not bestellnr:
        raise ValueError("Bestellnummer fehlt.")
    ek = zahl(z.get("ek", ""), profil.dezimaltrennzeichen).quantize(Decimal("0.0001"))
    if ek < 0:
        raise ValueError("EK ist negativ.")
    bestand = None
    if z.get("bestand"):
        bestand = int(zahl(z["bestand"], profil.dezimaltrennzeichen))
    lieferzeit = int(zahl(z["lieferzeit"], profil.dezimaltrennzeichen)) if z.get("lieferzeit") else None

    al = ArtikelLieferant.objects.filter(lieferant=profil.lieferant, bestellnummer=bestellnr).select_related("artikel").first()
    artikel = al.artikel if al else artikel_finden(z)
    angelegt = False
    if artikel is None:
        if not profil.neue_artikel:
            return "uebersprungen"
        if not z.get("bezeichnung"):
            raise ValueError("Unbekannter Artikel ohne Bezeichnung kann nicht angelegt werden.")
        if steuer is None:
            raise ValueError("Es ist kein Steuersatz angelegt.")
        nummer = f"P{profil.lieferant.nummer}-{bestellnr}"[:40]
        gewicht = zahl(z["gewicht"], profil.dezimaltrennzeichen) if z.get("gewicht") else None
        artikel = Artikel.objects.create(
            nummer=nummer, typ="P", bezeichnung=z["bezeichnung"][:255], herstellernummer=z.get("herstellernummer", "")[:100],
            ean=z.get("ean", "")[:14], steuersatz=steuer, hersteller=hersteller.get(z.get("hersteller", "").lower()),
            gewicht_kg=gewicht)
        angelegt = True
    else:  # fehlende Stammdaten ergänzen, vorhandene nicht überschreiben
        geaendert = False
        if not artikel.herstellernummer and z.get("herstellernummer"):
            artikel.herstellernummer, geaendert = z["herstellernummer"][:100], True
        if not artikel.ean and z.get("ean"):
            artikel.ean, geaendert = z["ean"][:14], True
        if geaendert:
            artikel.save()

    if al is None:
        ArtikelLieferant.objects.create(
            artikel=artikel, lieferant=profil.lieferant, bestellnummer=bestellnr, ek=ek, bestand=bestand,
            lieferzeit_tage=lieferzeit, preisliste=profil.name[:150], aktualisiert_am=jetzt)
        return "neu"
    aenderung = (al.ek != ek) or (bestand is not None and al.bestand != bestand) or (lieferzeit is not None and al.lieferzeit_tage != lieferzeit)
    al.ek, al.preisliste, al.aktualisiert_am = ek, profil.name[:150], jetzt
    if bestand is not None:
        al.bestand = bestand
    if lieferzeit is not None:
        al.lieferzeit_tage = lieferzeit
    al.save()
    return "geaendert" if aenderung else "unveraendert"


def faellige_profile(jetzt=None):
    jetzt = jetzt or timezone.now()
    for p in Preislistenprofil.objects.filter(aktiv=True, intervall_stunden__gt=0).exclude(quelle=Quelle.UPLOAD):
        if p.letzter_lauf is None or (jetzt - p.letzter_lauf).total_seconds() >= p.intervall_stunden * 3600:
            yield p
