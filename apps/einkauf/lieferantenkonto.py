"""Lieferant wie Kunde: Suche wie die Kundensuche und eine gemeinsame Liste der Einkaufsbelege
(Bestellungen, Wareneingänge, Lieferantenrechnungen, RMA) je Lieferant oder für alle Lieferanten."""
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from apps.stammdaten.models import lieferanten_auswahl

from .models import (RUECKSENDUNG_OFFEN, BestellStatus, Bestellung, Lieferantengutschrift, Lieferantenrechnung, RechnungStatus, Rma,
                     RmaStatus, Ruecksendung, Wareneingang)

LIEFERANTENSUCHE_FELDER = ["nummer", "kurzname", "name1", "name2", "name3", "ort",
                           "ansprechpartner__vorname", "ansprechpartner__nachname"]

ARTEN = [("BE", "Bestellung"), ("WE", "Wareneingang"), ("WR", "WE-Rücksendung"), ("LR", "Lieferantenrechnung"),
         ("LG", "Lieferanten-Gutschrift"), ("RMA", "RMA")]
BESTELLUNG_OFFEN = (BestellStatus.ENTWURF, BestellStatus.BESTELLT, BestellStatus.TEIL)
RMA_OFFEN = (RmaStatus.ANGELEGT, RmaStatus.EINGESENDET)


def lieferanten_suchen(q):
    """Wie die Kundensuche: jedes Wort in Nummer, Kurzname, Name, Ort oder Ansprechpartner; Reihenfolge egal,
    „*“ allein = alle, Stern am Ende = Wortanfang, „%“ wie LIKE. Gesperrte Lieferanten werden mit gefunden.
    Dieselbe Syntax wie bei Kunden: apps/stammdaten/partnersuche.py."""
    from apps.stammdaten.partnersuche import partner_filter
    return lieferanten_auswahl(gesperrte=True).filter(partner_filter(q, LIEFERANTENSUCHE_FELDER)).distinct()


def kontoauszug(mandant, lieferant):
    """Lieferantenkonto mit laufendem Saldo (positiv = wir schulden dem Lieferanten, negativ = Guthaben bei ihm):
    Rechnungen +, Zahlungsausgänge und Skonto −, Gutschriften −, Auszahlungen des Lieferanten auf Gutschriften +.
    Verrechnungen Gutschrift → Rechnung stehen nur zur Nachvollziehbarkeit da (Betrag in Klammern, Saldo unverändert)."""
    from decimal import Decimal

    from .models import LieferantengutschriftVerrechnung, LieferantengutschriftZahlung
    posten = []  # (datum, rang, id, text, betrag oder None, url, info)
    for r in Lieferantenrechnung.objects.filter(mandant=mandant, lieferant=lieferant).prefetch_related("verrechnungen", "teilzahlungen"):
        url = reverse("einkauf:rechnung", args=[r.pk])
        posten.append((r.datum, 0, r.pk, f"Rechnung {r.rechnungsnummer}", r.brutto, url, None))
        for t in r.teilzahlungen.all():
            if not t.storniert:
                posten.append((t.datum, 1, r.pk, f"Teilzahlung an Lieferanten zu Rechnung {r.rechnungsnummer}", -t.betrag, url, None))
        if r.status == RechnungStatus.BEZAHLT and r.bezahlt_am:
            if r.bezahlter_betrag:
                posten.append((r.bezahlt_am, 1, r.pk, f"Zahlung an Lieferanten zu Rechnung {r.rechnungsnummer}", -r.bezahlter_betrag, url, None))
            rest = r.brutto - r.verrechnet - r.teilgezahlt - (r.bezahlter_betrag or Decimal("0"))
            if rest:
                posten.append((r.bezahlt_am, 1, r.pk, f"Skonto/Differenz zu Rechnung {r.rechnungsnummer}", -rest, url, None))
    for g in Lieferantengutschrift.objects.filter(mandant=mandant, lieferant=lieferant).select_related("ruecksendung"):
        aus = f" (aus {g.ruecksendung.nummer})" if g.ruecksendung_id else ""
        posten.append((g.datum, 0, g.pk, f"Gutschrift {g.nummer}{aus}", -g.brutto, reverse("einkauf:gutschrift", args=[g.pk]), None))
    for v in LieferantengutschriftVerrechnung.objects.filter(gutschrift__mandant=mandant, gutschrift__lieferant=lieferant,
                                                             aufgehoben=False).select_related("gutschrift", "rechnung"):
        posten.append((v.datum, 2, v.pk, f"Verrechnet: Gutschrift {v.gutschrift.nummer} → Rechnung {v.rechnung.rechnungsnummer}",
                       None, reverse("einkauf:gutschrift", args=[v.gutschrift_id]), v.betrag))
    for z in LieferantengutschriftZahlung.objects.filter(gutschrift__mandant=mandant, gutschrift__lieferant=lieferant,
                                                         storniert=False).select_related("gutschrift"):
        posten.append((z.datum, 1, z.pk, f"Auszahlung des Lieferanten auf Gutschrift {z.gutschrift.nummer}", z.betrag,
                       reverse("einkauf:gutschrift", args=[z.gutschrift_id]), None))
    posten.sort(key=lambda p: p[:3])
    saldo, zeilen = Decimal("0"), []
    for datum, _, _, text, betrag, url, info in posten:
        if betrag is not None:
            saldo += betrag
        zeilen.append({"datum": datum, "text": text, "betrag": betrag, "info": info, "saldo": saldo, "url": url})
    return zeilen, saldo


def belege(mandant, lieferant=None, nur_offen=True, art="", ab=None):
    """Einkaufsbelege als einheitliche Zeilen, neueste zuerst. nur_offen: Bestellungen nicht geliefert/storniert,
    Wareneingänge nicht gebucht, Rechnungen nicht bezahlt, RMA angelegt oder eingesendet."""
    def filtern(qs, datumsfeld="datum"):
        qs = qs.filter(mandant=mandant)
        if lieferant is not None:
            qs = qs.filter(lieferant=lieferant)
        if ab:
            qs = qs.filter(**{f"{datumsfeld}__gte": ab})
        return qs.select_related("lieferant")

    zeilen = []
    if art in ("", "BE"):
        qs = filtern(Bestellung.objects.prefetch_related("positionen"))
        if nur_offen:
            qs = qs.filter(status__in=BESTELLUNG_OFFEN)
        for b in qs:
            zeilen.append({"art": "BE", "art_text": "Bestellung", "nummer": b.nummer, "vorlaeufig": b.vorlaeufig, "url": reverse("einkauf:bestellung", args=[b.pk]),
                           "datum": b.datum, "lieferant": b.lieferant, "bereich": "bestellung", "status": b.status,
                           "status_text": b.get_status_display(), "betrag": b.summe_netto, "betrag_art": "netto", "pk": b.pk})
    if art in ("", "WE"):
        qs = filtern(Wareneingang.objects.select_related("bestellung").prefetch_related("positionen"))
        if nur_offen:
            qs = qs.filter(gebucht=False)
        for w in qs:
            zeilen.append({"art": "WE", "art_text": "Wareneingang", "nummer": w.nummer, "url": reverse("einkauf:wareneingang", args=[w.pk]),
                           "datum": w.datum, "lieferant": w.lieferant, "bereich": "wareneingang",
                           "status": "gebucht" if w.gebucht else "offen", "status_text": "ins Lager gebucht" if w.gebucht else "noch nicht gebucht",
                           "betrag": w.summe_netto, "betrag_art": "netto", "bezug": w.bestellung.nummer if w.bestellung_id else "", "pk": w.pk})
    if art in ("", "LR"):
        qs = filtern(Lieferantenrechnung.objects.all())
        if nur_offen:
            qs = qs.exclude(status=RechnungStatus.BEZAHLT)
        for r in qs:
            zeilen.append({"art": "LR", "art_text": "Rechnung", "nummer": r.rechnungsnummer, "url": reverse("einkauf:rechnung", args=[r.pk]),
                           "datum": r.datum, "lieferant": r.lieferant, "bereich": "lieferantenrechnung", "status": r.status,
                           "status_text": r.get_status_display(), "betrag": r.brutto, "betrag_art": "brutto",
                           "faellig": r.faellig, "skonto_bis": r.skonto_bis, "pk": r.pk})
    if art in ("", "WR"):
        qs = filtern(Ruecksendung.objects.select_related("wareneingang").prefetch_related("positionen"))
        if nur_offen:
            qs = qs.filter(status__in=RUECKSENDUNG_OFFEN)
        for r in qs:
            zeilen.append({"art": "WR", "art_text": "Rücksendung", "nummer": r.nummer, "url": reverse("einkauf:ruecksendung", args=[r.pk]),
                           "datum": r.datum, "lieferant": r.lieferant, "bereich": "ruecksendung", "status": r.status,
                           "status_text": r.get_status_display(), "betrag": r.summe_netto, "betrag_art": "netto",
                           "bezug": r.wareneingang.nummer, "pk": r.pk})
    if art in ("", "LG"):
        # Gutschriften des Lieferanten; offen = Restguthaben > 0. Betrag = Restguthaben, Brutto steht im Bezug.
        qs = filtern(Lieferantengutschrift.objects.select_related("ruecksendung").prefetch_related("verrechnungen", "zahlungen"))
        for g in qs:
            rest = g.restguthaben
            if nur_offen and rest <= 0:
                continue
            bezug = f"aus {g.ruecksendung.nummer}, " if g.ruecksendung_id else ""
            zeilen.append({"art": "LG", "art_text": "Gutschrift", "nummer": g.nummer, "url": reverse("einkauf:gutschrift", args=[g.pk]),
                           "datum": g.datum, "lieferant": g.lieferant, "bereich": "lieferantengutschrift",
                           "status": "offen" if rest > 0 else "erledigt",
                           "status_text": "Restguthaben offen" if rest > 0 else "verrechnet/ausgezahlt",
                           "betrag": rest, "betrag_art": "Rest",
                           "bezug": f"{bezug}Gutschrift {g.brutto:.2f} € brutto".replace(".", ","), "pk": g.pk})
    if art in ("", "RMA"):
        qs = filtern(Rma.objects.all(), "angelegt_am__date")
        if nur_offen:
            qs = qs.filter(status__in=RMA_OFFEN)
        for r in qs:
            zeilen.append({"art": "RMA", "art_text": "RMA", "nummer": r.nummer, "url": reverse("einkauf:rma", args=[r.pk]),
                           "datum": timezone.localtime(r.angelegt_am).date(), "lieferant": r.lieferant, "bereich": "rma", "status": r.status,
                           "status_text": r.get_status_display(), "betrag": None, "betrag_art": "", "bezug": r.bezeichnung, "pk": r.pk})
    zeilen.sort(key=lambda z: (z["datum"], z["pk"]), reverse=True)
    return zeilen
