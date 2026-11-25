"""Produktivstart (Entscheidung des Nutzers 25.11.2026): alle Probedaten löschen, Einstellungen behalten, Belegnummern auf Anfang.

Bleibt: Benutzer, Mandanten (Firmendaten, Logo, Bank, Mail, Steuer-ID), Steuersätze, Zahlungsbedingungen, Anreden, Kategorien,
Hersteller, Textbausteine/Mailvorlagen, Versandarten, Mahnstufen, Preisvorgaben, Shop-Einstellungen, Preislisten-Profile mit
FTP-Zugängen und die Lieferanten, an denen ein Profil hängt (nur Stammdaten), Stammnummern (Zähler für Kunden-, Artikel- und
Lieferantennummern stellt der Nutzer selbst unter „Laufende Nummern“ ein).
Gelöscht: alle Belege und Positionen, Zahlungen, Mahnungen, Bank, Kasse, Lastschriften, Lager, Seriennummern, Bestellungen,
Wareneingänge, Rücksendungen, Lieferantenrechnungen und -gutschriften, RMA, Werkstatt, ESD, Artikel (samt Bildern), Kunden,
übrige Lieferanten, Preislistenartikel und Importläufe, Shop-Abgleich, Änderungsprotokoll; die Belegnummernkreise werden geleert
(nächste Nummer = 1, unter „Laufende Nummern“ frei einstellbar, auch RE/GS).

Jedes Modell der Projekt-Apps muss in BEHALTEN, LOESCHEN oder TEILWEISE stehen – ein Test erzwingt das für neue Modelle.
"""
import gzip
import os

from django.apps import apps
from django.core import management
from django.db import connection, models, transaction
from django.db.models import Q
from django.utils import timezone

PROJEKT_APPS = ("kern", "stammdaten", "verkauf", "einkauf", "lager", "finanzen", "esd", "shop", "werkstatt")

BEHALTEN = {
    "kern.Mandant", "kern.Listenansicht", "kern.Zahlungskonto", "kern.Plattformkonto",
    "stammdaten.Zahlungsbedingung", "stammdaten.Anrede", "stammdaten.Steuersatz", "stammdaten.Kategorie",
    "stammdaten.Leistungskategorie", "stammdaten.Hersteller", "stammdaten.PreisVorgabe", "stammdaten.Stammnummer",
    "verkauf.Versandart", "verkauf.Mailvorlage", "verkauf.Briefprodukt",
    "finanzen.Mahnstufe",
    "einkauf.Preislistenprofil", "einkauf.Bestpreisplan",
    "shop.Shop", "shop.ShopKategorie",
}

LOESCHEN = {
    "kern.AenderungsProtokoll",
    "stammdaten.Kunde", "stammdaten.KundeMandant", "stammdaten.Kundenanschrift", "stammdaten.Artikel", "stammdaten.ArtikelMandant",
    "stammdaten.ArtikelBild", "stammdaten.ArtikelLieferant", "stammdaten.GruppenKomponente", "stammdaten.Kundenpreis",
    "stammdaten.Preislistenartikel", "stammdaten.UstIdPruefung", "stammdaten.RadarGesehen",
    "verkauf.Nummernkreis", "verkauf.Beleg", "verkauf.BelegUmwandlung", "verkauf.BelegVorgang", "verkauf.BelegUebernahme",
    "verkauf.MailVersand", "verkauf.Position", "verkauf.BelegArbeitsstand", "verkauf.Abo", "verkauf.AboPosition", "verkauf.Versand",
    "verkauf.Wiedervorlage", "verkauf.ERechnungArchiv",
    "lager.Lagerbewegung", "lager.Reparaturbewegung", "lager.Seriennummer",
    "finanzen.Zahlung", "finanzen.Gutschriftverrechnung", "finanzen.Anzahlungsverrechnung", "finanzen.Mahnung", "finanzen.Bankumsatz",
    "finanzen.Kassenbuch", "finanzen.Lastschrift",
    "werkstatt.Reparatur", "werkstatt.ReparaturPosition",
    "einkauf.Bestellung", "einkauf.BestellPosition", "einkauf.Wareneingang", "einkauf.WareneingangPosition",
    "einkauf.Lieferantenrechnung", "einkauf.Lieferantenteilzahlung", "einkauf.Rma", "einkauf.Preislistenimport",
    "einkauf.Preispruefung", "einkauf.Ruecksendung", "einkauf.RuecksendungPosition", "einkauf.Lieferantengutschrift",
    "einkauf.LieferantengutschriftVerrechnung", "einkauf.LieferantengutschriftZahlung",
    "esd.ESDKey", "esd.ESDVergabe",
    "shop.ShopBestellung", "shop.ShopArtikel", "shop.SyncProtokoll",
}

# Lieferanten mit Preislisten-Profil bleiben (nur Stammdaten), ebenso ihre Mandantendaten und Ansprechpartner
TEILWEISE = {"stammdaten.Lieferant", "stammdaten.LieferantMandant", "stammdaten.Ansprechpartner"}

BESTAETIGUNG = "PRODUKTIVSTART"


def projektmodelle() -> list:
    """Konkrete Modelle der Projekt-Apps (ohne Proxys wie Leistungsartikel, die dieselbe Tabelle nutzen)."""
    return [m for m in apps.get_models() if m._meta.app_label in PROJEKT_APPS and not m._meta.proxy]


def label(modell) -> str:
    return f"{modell._meta.app_label}.{modell.__name__}"


def unklassifiziert() -> list:
    return sorted(label(m) for m in projektmodelle() if label(m) not in BEHALTEN | LOESCHEN | TEILWEISE)


def _teilweise():
    """(Queryset der zu löschenden Zeilen) je teilweise geleertem Modell."""
    Lieferant = apps.get_model("stammdaten", "Lieferant")
    Preislistenprofil = apps.get_model("einkauf", "Preislistenprofil")
    bleiben = set(Preislistenprofil.objects.values_list("lieferant_id", flat=True))
    weg = Lieferant.objects.exclude(pk__in=bleiben)
    return {
        "stammdaten.Lieferant": weg,
        "stammdaten.LieferantMandant": apps.get_model("stammdaten", "LieferantMandant").objects.exclude(lieferant_id__in=bleiben),
        "stammdaten.Ansprechpartner": apps.get_model("stammdaten", "Ansprechpartner").objects.filter(
            Q(kunde__isnull=False) | (Q(lieferant__isnull=False) & ~Q(lieferant_id__in=bleiben))),
    }


def vorschau() -> dict:
    """Anzahl je Modell: was gelöscht wird und was bleibt (für die Ausgabe vor dem Ausführen)."""
    teil = _teilweise()
    loeschen, behalten = {}, {}
    for m in projektmodelle():
        name, gesamt = label(m), m.objects.count()
        if name in LOESCHEN:
            loeschen[name] = gesamt
        elif name in TEILWEISE:
            weg = teil[name].count()
            loeschen[name] = weg
            behalten[name] = gesamt - weg
        else:
            behalten[name] = gesamt
    return {"loeschen": loeschen, "behalten": behalten, "unklassifiziert": unklassifiziert()}


def sichern(ordner: str) -> str:
    """Vollständige Datensicherung (dumpdata, gzip) vor dem Löschen; gibt den Dateipfad zurück."""
    os.makedirs(ordner, exist_ok=True)
    pfad = os.path.join(ordner, f"vor_produktivstart_{timezone.localtime():%Y%m%d_%H%M%S}.json.gz")
    with gzip.open(pfad, "wt", encoding="utf-8") as datei:
        management.call_command("dumpdata", "--natural-foreign", "--exclude", "contenttypes", "--exclude", "auth.permission",
                                "--exclude", "sessions", stdout=datei)
    return pfad


def _dateien(modell, qs) -> list:
    felder = [f.name for f in modell._meta.fields if isinstance(f, models.FileField)]
    if not felder:
        return []
    dateien = []
    for zeile in qs.only(*felder):
        for feld in felder:
            datei = getattr(zeile, feld)
            if datei and datei.name:
                dateien.append((datei.storage, datei.name))
    return dateien


def ausfuehren(bestaetigung: str) -> dict:
    """Löscht alles aus LOESCHEN und die TEILWEISE-Zeilen in einer Transaktion. Fremdschlüssel werden dabei kurz nicht geprüft
    (die Tabellen verweisen kreuz und quer, auch mit PROTECT), am Ende aber vollständig kontrolliert: bleibt ein Verweis ins
    Leere, wird alles zurückgerollt. Dateien (Artikelbilder) werden erst nach erfolgreichem Abschluss entfernt."""
    if bestaetigung != BESTAETIGUNG:
        raise ValueError(f"Zum Ausführen bitte genau „{BESTAETIGUNG}“ als Bestätigung angeben.")
    if unklassifiziert():
        raise ValueError("Nicht zugeordnete Tabellen: " + ", ".join(unklassifiziert()))
    nach_name = {label(m): m for m in projektmodelle()}
    teil = _teilweise()
    geloescht, dateien = {}, []
    with connection.constraint_checks_disabled():
        with transaction.atomic():
            ziele = [(name, nach_name[name].objects.all()) for name in sorted(LOESCHEN)] + \
                    [(name, qs) for name, qs in teil.items()]
            # automatische Zwischentabellen (Mehrfachbeziehungen) der geleerten Modelle mitleeren
            for name, qs in ziele:
                modell = nach_name[name]
                for feld in modell._meta.many_to_many:
                    durch = feld.remote_field.through
                    if durch._meta.auto_created:
                        quelle = feld.m2m_field_name()
                        durch.objects.filter(**{f"{quelle}__in": qs.values("pk")})._raw_delete(durch.objects.db)
            for name, qs in ziele:
                dateien += _dateien(nach_name[name], qs)
                geloescht[name] = qs._raw_delete(qs.db)
            tabellen = [m._meta.db_table for m in projektmodelle()] + \
                       [f.remote_field.through._meta.db_table for m in projektmodelle() for f in m._meta.many_to_many]
            connection.check_constraints(table_names=tabellen)  # wirft IntegrityError → alles wird zurückgerollt
    for speicher, name in dateien:
        try:
            speicher.delete(name)
        except OSError:
            pass
    return geloescht
