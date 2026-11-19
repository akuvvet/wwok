"""Schnellbedienung (Strg+K): Treffer über Kunden, Artikel, Belege, Lieferanten, Bestellungen und Werkstattaufträge des aktiven Mandanten.

Rechte: Alle diese Ansichten stehen jedem angemeldeten Benutzer mit gewähltem Mandanten offen (Admin-Pflicht gibt es nur bei
Änderungen, siehe rechte.py); die Suche liefert nur Links zu Leseseiten und keine Preise oder Bankdaten.
Mandanten: Belege, Bestellungen und Werkstattaufträge werden strikt auf request.mandant begrenzt."""
from django.db.models import Case, Exists, IntegerField, OuterRef, Q, Value, When
from django.http import JsonResponse
from django.urls import reverse

from apps.einkauf.models import Bestellung
from apps.stammdaten.models import KENNUNGEN, Artikel, Kunde, Lieferant
from apps.verkauf.models import Beleg, BelegUmwandlung
from apps.verkauf.views import mit_mandant
from apps.werkstatt.models import Reparatur

MIN_ZEICHEN = 2
MAX_JE_GRUPPE = 6
MAX_WOERTER = 6
MAX_LAENGE = 100
KENNUNG_NAMEN = {"ean": "EAN", "amazon_asin": "ASIN", "amazon_sku": "SKU", "amazon_fnsku": "FNSKU", "isbn": "ISBN", "upc": "UPC", "ebay_id": "eBay"}


def _filter(woerter, felder, extra=None):
    """Jedes Wort muss in mindestens einem der Felder vorkommen (Reihenfolge egal)."""
    gesamt = Q()
    for wort in woerter:
        teil = Q()
        for feld in felder:
            teil |= Q(**{feld + "__icontains": wort})
        if extra is not None:
            teil |= extra(wort)
        gesamt &= teil
    return gesamt


def _exakt_zuerst(abfrage, q, feld="nummer"):
    """Exakter Treffer der Nummer zuerst, danach nach Nummer."""
    return abfrage.annotate(_exakt=Case(When(**{feld + "__iexact": q}, then=Value(0)), default=Value(1), output_field=IntegerField())
                            ).order_by("_exakt", feld)


def _name(zeile, prefix=""):
    return " ".join(t for t in (zeile.get(prefix + "name1"), zeile.get(prefix + "name2")) if t)


def suchen(q, mandant):
    """Liefert die Trefferliste (Liste von Wörterbüchern) für den Suchtext q."""
    q = " ".join(q.split())[:MAX_LAENGE]
    if len(q) < MIN_ZEICHEN:
        return []
    woerter = q.split(" ")[:MAX_WOERTER]
    treffer = []

    def hinzu(gruppe, titel, zusatz, url):
        treffer.append({"gruppe": gruppe, "titel": titel, "zusatz": zusatz, "url": url})

    kunden = Kunde.objects.filter(_filter(woerter, ["nummer", "name1", "name2", "name3", "ort"]), anonymisiert=False)
    for z in _exakt_zuerst(kunden, q).values("pk", "nummer", "name1", "name2", "ort")[:MAX_JE_GRUPPE]:
        hinzu("Kunde", f"{z['nummer']} – {_name(z)}", z["ort"], reverse("verkauf:kunde", args=[z["pk"]]))

    artikel = Artikel.objects.filter(_filter(woerter, ["nummer", "bezeichnung", "herstellernummer", *KENNUNGEN]))
    for z in _exakt_zuerst(artikel, q).values("pk", "nummer", "bezeichnung", "nicht_aktiv", *KENNUNGEN)[:MAX_JE_GRUPPE]:
        # Zusatz: die Kennung, über die der Artikel gefunden wurde (zuerst die EAN), sonst die erste vorhandene
        gefunden = [k for k in KENNUNGEN if z[k] and any(w.lower() in z[k].lower() for w in woerter)] or [k for k in KENNUNGEN if z[k]]
        zusatz = "nicht aktiv" if z["nicht_aktiv"] else (f"{KENNUNG_NAMEN[gefunden[0]]} {z[gefunden[0]]}" if gefunden else "")
        hinzu("Artikel", f"{z['nummer']} – {z['bezeichnung']}", zusatz, reverse("lager:artikel", args=[z["pk"]]))

    # Belege des aktiven Mandanten; auch die alten Nummern umgewandelter Belege (Angebot -> Bestellung -> Rechnung)
    alt = lambda wort: Exists(BelegUmwandlung.objects.filter(beleg=OuterRef("pk"), von_nummer__icontains=wort))
    belege = Beleg.objects.filter(_filter(woerter, ["nummer", "kunde__name1"], alt), mandant=mandant, nummer__isnull=False)
    for z in _exakt_zuerst(belege, q).values("pk", "nummer", "art", "kunde__name1", "kunde__name2", "kunde__anonymisiert")[:MAX_JE_GRUPPE]:
        art = dict(Beleg._meta.get_field("art").choices).get(z["art"], "Beleg")
        kunde = "" if z["kunde__anonymisiert"] else _name(z, "kunde__")
        hinzu("Beleg", z["nummer"], f"{art} · {kunde}" if kunde else art, reverse("verkauf:beleg", args=[z["pk"]]))

    lieferanten = Lieferant.objects.filter(_filter(woerter, ["nummer", "name1", "name2", "kurzname"]), anonymisiert=False)
    for z in _exakt_zuerst(lieferanten, q).values("pk", "nummer", "name1", "name2", "ort")[:MAX_JE_GRUPPE]:
        hinzu("Lieferant", f"{z['nummer']} – {_name(z)}", z["ort"], reverse("einkauf:lieferant", args=[z["pk"]]))

    bestellungen = Bestellung.objects.filter(_filter(woerter, ["nummer", "lieferant__name1"]), mandant=mandant)
    for z in _exakt_zuerst(bestellungen, q).values("pk", "nummer", "lieferant__name1")[:MAX_JE_GRUPPE]:
        hinzu("Bestellung", z["nummer"], z["lieferant__name1"], reverse("einkauf:bestellung", args=[z["pk"]]))

    reparaturen = Reparatur.objects.filter(_filter(woerter, ["nummer", "geraet", "kunde__name1"]), mandant=mandant)
    for z in _exakt_zuerst(reparaturen, q).values("pk", "nummer", "geraet", "kunde__name1", "kunde__anonymisiert")[:MAX_JE_GRUPPE]:
        kunde = "" if z["kunde__anonymisiert"] else z["kunde__name1"]
        hinzu("Werkstatt", z["nummer"], f"{z['geraet']} · {kunde}" if kunde else z["geraet"], reverse("werkstatt:detail", args=[z["pk"]]))

    return treffer


@mit_mandant
def schnellsuche(request):
    return JsonResponse({"treffer": suchen(request.GET.get("q", ""), request.mandant)})
