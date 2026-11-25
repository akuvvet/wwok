"""DATEV-Export (Buchungsstapel, Format EXTF 700 / Kategorie 21) für Rechnungen, Gutschriften und – je Mandant zuschaltbar – Zahlungen.

Vorgabe SKR03: Debitoren = Kundennummer (Personenkonto), Erlöse und Bank/Kasse je Mandant einstellbar.
Bitte vor dem ersten Einsatz mit dem Steuerberater abstimmen (Kontenrahmen, Buchungsschlüssel).
"""
import csv
import io
from collections import defaultdict
from decimal import Decimal

from django.utils import timezone

from apps.verkauf.models import Beleg, Belegart, runden

from .models import Zahlart, Zahlung

SPALTEN = ["Umsatz (ohne Soll/Haben-Kz)", "Soll/Haben-Kennzeichen", "WKZ Umsatz", "Kurs", "Basis-Umsatz",
           "WKZ Basis-Umsatz", "Konto", "Gegenkonto (ohne BU-Schlüssel)", "BU-Schlüssel", "Belegdatum",
           "Belegfeld 1", "Belegfeld 2", "Skonto", "Buchungstext"]


def komma(wert: Decimal) -> str:
    return f"{wert:.2f}".replace(".", ",")


def debitor(kunde) -> str:
    return kunde.nummer if kunde.nummer.isdigit() else "10000"


def erloeskonto(mandant, prozent: Decimal) -> str:
    if prozent == Decimal("19"):
        return mandant.konto_erloes_19
    if prozent == Decimal("7"):
        return mandant.konto_erloes_7
    return mandant.konto_erloes_0


def beleg_zeilen(beleg):
    """Je Steuersatz eine Buchung (brutto) Debitor an Erlöse; Versandkosten laufen mit dem höchsten Satz."""
    netto = defaultdict(Decimal)
    for p in beleg.positionen.all():
        netto[p.steuer_prozent] += p.netto_gesamt
    netto = defaultdict(Decimal, beleg.nach_rabatt(netto)[0])  # Belegrabatt wirkt je Steuersatz
    nebenkosten = beleg.versandkosten + beleg.verpackung + beleg.versicherung
    if nebenkosten:
        netto[max(netto, default=Decimal("19"))] += nebenkosten
    for satz, basis in sorted(netto.items()):
        yield satz, runden(basis) + runden(basis * satz / 100)


def zahlungszeilen(mandant, von, bis) -> list:
    """Bank/Kasse/Skonto an Debitor. Bereinigt (Wunsch des Nutzers 25.11.2026): eine Zahlung und ihr Storno, die beide im Zeitraum
    liegen, entfallen (netto null). Anzahlungen auf Bestellung/Proforma tragen in Belegfeld 1 die Rechnungsnummer, sobald sie mit
    einer Rechnung verrechnet sind (Bestell-/Proformanummer dann in Belegfeld 2), damit der offene Posten ausgeglichen wird."""
    from .models import Anzahlungsverrechnung
    zahlungen = list(Zahlung.objects.filter(mandant=mandant, datum__range=(von, bis)).select_related("beleg__kunde").order_by("datum", "id"))
    im_zeitraum = {z.pk for z in zahlungen}
    paare = {z.pk for z in zahlungen if z.storno_zu_id in im_zeitraum} | {z.storno_zu_id for z in zahlungen if z.storno_zu_id in im_zeitraum}
    rechnung_zu = {}  # Anzahlungsbeleg → Nummer der Rechnung, mit der er (noch gültig) verrechnet ist
    for v in Anzahlungsverrechnung.objects.filter(mandant=mandant, storno_zu__isnull=True, storniert_durch__isnull=True,
                                                   quelle_id__in={z.beleg_id for z in zahlungen}).select_related("rechnung").order_by("id"):
        rechnung_zu.setdefault(v.quelle_id, v.rechnung.nummer)
    zeilen = []
    for z in zahlungen:
        if z.pk in paare:
            continue
        skonto = z.zahlart == Zahlart.SKONTO  # kein Geldeingang: Skontokonto an Debitor
        konto = mandant.konto_skonto if skonto else mandant.konto_kasse if z.zahlart == Zahlart.BAR else mandant.konto_bank
        feld1, feld2 = z.beleg.nummer, ""
        if z.beleg.art not in (Belegart.RECHNUNG, Belegart.GUTSCHRIFT) and z.beleg_id in rechnung_zu:
            feld1, feld2 = rechnung_zu[z.beleg_id], z.beleg.nummer
        zeilen.append([komma(abs(z.betrag)), "S" if z.betrag > 0 else "H", "EUR", "", "", "", konto, debitor(z.beleg.kunde),
                       "", z.datum.strftime("%d%m"), feld1, feld2, "", f"{'Skonto' if skonto else 'Zahlung'} {z.beleg.kunde.name1}"[:60]])
    return zeilen


def export(mandant, von, bis) -> bytes:
    zeilen = []
    belege = Beleg.objects.filter(mandant=mandant, abgeschlossen=True, belegdatum__range=(von, bis),
                                  art__in=[Belegart.RECHNUNG, Belegart.GUTSCHRIFT]).select_related("kunde").order_by("belegdatum", "id")
    for b in belege:
        for satz, brutto in beleg_zeilen(b):
            zeilen.append([komma(brutto), "S" if b.art == Belegart.RECHNUNG else "H", "EUR", "", "", "", debitor(b.kunde),
                           erloeskonto(mandant, satz), "", b.belegdatum.strftime("%d%m"), b.nummer, "", "",
                           f"{b.get_art_display()} {b.kunde.name1}"[:60]])
    if mandant.datev_zahlungen:  # sonst bucht der Steuerberater die Bank selbst (Entscheidung des Nutzers 25.11.2026)
        zeilen += zahlungszeilen(mandant, von, bis)
    ausgabe = io.StringIO()
    schreiber = csv.writer(ausgabe, delimiter=";", quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
    jetzt = timezone.now().strftime("%Y%m%d%H%M%S000")
    schreiber.writerow(["EXTF", 700, 21, "Buchungsstapel", 12, jetzt, "", "WW", "", "", mandant.datev_berater or "",
                        mandant.datev_mandant or "", von.strftime("%Y0101"), 4, von.strftime("%Y%m%d"), bis.strftime("%Y%m%d"),
                        "wwok Export", "", 1, 0, 0, "EUR"])
    schreiber.writerow(SPALTEN)
    for z in zeilen:
        schreiber.writerow(z)
    return ausgabe.getvalue().encode("cp1252", errors="replace")
