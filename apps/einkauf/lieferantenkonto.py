"""Lieferant wie Kunde: Suche wie die Kundensuche und eine gemeinsame Liste der Einkaufsbelege
(Bestellungen, Wareneingänge, Lieferantenrechnungen, RMA) je Lieferant oder für alle Lieferanten."""
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from apps.stammdaten.models import lieferanten_auswahl

from .models import BestellStatus, Bestellung, Lieferantenrechnung, RechnungStatus, Rma, RmaStatus, Wareneingang

LIEFERANTENSUCHE_FELDER = ["nummer", "kurzname", "name1", "name2", "name3", "ort",
                           "ansprechpartner__vorname", "ansprechpartner__nachname"]

ARTEN = [("BE", "Bestellung"), ("WE", "Wareneingang"), ("LR", "Lieferantenrechnung"), ("RMA", "RMA")]
BESTELLUNG_OFFEN = (BestellStatus.ENTWURF, BestellStatus.BESTELLT, BestellStatus.TEIL)
RMA_OFFEN = (RmaStatus.ANGELEGT, RmaStatus.EINGESENDET)


def lieferanten_suchen(q):
    """Wie die Kundensuche: jedes Wort in Nummer, Kurzname, Name, Ort oder Ansprechpartner; Reihenfolge egal,
    „*“ allein = alle, Stern am Ende = Wortanfang. Gesperrte Lieferanten werden mit gefunden."""
    filt = Q()
    if q != "*":
        vergleich = "istartswith" if q.endswith("*") else "icontains"
        for wort in q.rstrip("*").split():
            filt &= Q(*[Q(**{f"{feld}__{vergleich}": wort}) for feld in LIEFERANTENSUCHE_FELDER], _connector=Q.OR)
    return lieferanten_auswahl(gesperrte=True).filter(filt).distinct()


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
            zeilen.append({"art": "BE", "art_text": "Bestellung", "nummer": b.nummer, "url": reverse("einkauf:bestellung", args=[b.pk]),
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
