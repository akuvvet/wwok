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


def erloeskonto(mandant, prozent: Decimal, beleg=None, typ="") -> str:
    """Steuerart des Kunden EG-Ausland/Drittland (Entscheidung des Nutzers 26.11.2026, SKR03): innergem. Lieferung 8125,
    Leistung an EU-Unternehmen 8338, Ausfuhr 8120 – am Mandanten änderbar."""
    if beleg is not None and beleg.steuerfrei:
        if beleg.steuerart == 2:
            return (mandant.konto_eg_leistung or "8338") if typ == "L" else (mandant.konto_eg_lieferung or "8125")
        return mandant.konto_ausfuhr or "8120"
    if prozent == Decimal("19"):
        return mandant.konto_erloes_19
    if prozent == Decimal("7"):
        return mandant.konto_erloes_7
    return mandant.konto_erloes_0


def beleg_zeilen(beleg):
    """(Erlöskonto, brutto) je Steuersatz und Erlöskonto, Debitor an Erlöse. Versandkosten laufen mit dem höchsten Satz auf dessen
    Standardkonto. Positionen mit 19 %, deren Artikel eine zugeordnete Kostenstelle hat, laufen auf deren Erlöskonto (Vorgabe des
    Steuerberaters 25.11.2026, z. B. Apetito = 8401). Rabatt und Rundung je Steuersatz wie bisher; die Summe je Satz wird auf die
    Konten anteilig verteilt, der Rest auf das letzte Konto – die Belegsumme bleibt auf den Cent gleich."""
    mandant = beleg.mandant
    zuordnung = mandant.kostenstellen_zuordnung()
    netto, je_konto = defaultdict(Decimal), defaultdict(lambda: defaultdict(Decimal))
    for p in beleg.positionen.select_related("artikel"):
        konto = erloeskonto(mandant, p.steuer_prozent, beleg, p.typ)
        stelle = (p.artikel.kostenstelle if p.artikel_id else "").strip().lower()
        if p.steuer_prozent == Decimal("19") and stelle in zuordnung:
            konto = zuordnung[stelle]
        netto[p.steuer_prozent] += p.netto_gesamt
        je_konto[p.steuer_prozent][konto] += p.netto_gesamt
    netto = defaultdict(Decimal, beleg.nach_rabatt(netto)[0])  # Belegrabatt wirkt je Steuersatz
    nebenkosten = beleg.versandkosten + beleg.verpackung + beleg.versicherung
    if nebenkosten:
        hoechster = max(netto, default=Decimal("19"))
        netto[hoechster] += nebenkosten
        # Versand folgt der Ware: das Konto der größten Position dieses Satzes (ohne MwSt: Lieferung vor Leistung)
        ziel = max(je_konto[hoechster].items(), key=lambda e: e[1])[0] if beleg.steuerfrei and je_konto[hoechster] \
            else erloeskonto(mandant, hoechster, beleg)
        je_konto[hoechster][ziel] += nebenkosten
    for satz, basis in sorted(netto.items()):
        brutto = runden(basis) + runden(basis * satz / 100)
        konten = sorted(je_konto[satz].items()) or [(erloeskonto(mandant, satz, beleg), Decimal("1"))]
        gesamt = sum((w for _, w in konten), Decimal("0"))
        rest = brutto
        for i, (konto, wert) in enumerate(konten):
            teil = rest if i == len(konten) - 1 else runden(brutto * wert / gesamt) if gesamt else Decimal("0")
            rest -= teil
            if teil:
                yield konto, teil


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
        for konto, brutto in beleg_zeilen(b):
            zeilen.append([komma(brutto), "S" if b.art == Belegart.RECHNUNG else "H", "EUR", "", "", "", debitor(b.kunde),
                           konto, "", b.belegdatum.strftime("%d%m"), b.nummer, "", "",
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
