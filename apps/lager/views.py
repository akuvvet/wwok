from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.db.models import Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.stammdaten.models import Artikel, ArtikelMandant
from apps.verkauf.views import mit_mandant

from .models import Bewegungsart, Lagerbewegung, bestand, reserviert
from .services import buche_manuell, inventur

MANUELL = [Bewegungsart.WARENEINGANG, Bewegungsart.EIGENVERBRAUCH, Bewegungsart.RETOURE, Bewegungsart.KORREKTUR]


def zahl(text, standard=None):
    text = (text or "").strip().replace(",", ".")
    if not text:
        return standard
    try:
        return Decimal(text)
    except InvalidOperation:
        raise ValueError(f"Ungültige Zahl: {text}")


def suche(q):
    artikel = Artikel.objects.filter(nicht_aktiv=False)
    for wort in q.split():
        artikel = artikel.filter(
            Q(nummer__icontains=wort) | Q(bezeichnung__icontains=wort) | Q(herstellernummer__icontains=wort)
            | Q(hersteller__name__icontains=wort) | Q(ean__icontains=wort))
    return artikel


@mit_mandant
def uebersicht(request):
    q = request.GET.get("q", "").strip()
    nur_bestand = request.GET.get("bestand") == "1"
    summen = {s["artikel"]: s["s"] for s in Lagerbewegung.objects.filter(mandant=request.mandant)
              .values("artikel").annotate(s=Sum("menge"))}
    zeilen, lagerwert = [], Decimal("0")
    artikel = suche(q).select_related("steuersatz") if q else Artikel.objects.filter(nicht_aktiv=False)
    if nur_bestand:
        artikel = artikel.filter(pk__in=[a for a, s in summen.items() if s])
    for a in artikel.order_by("nummer")[:300]:
        b = summen.get(a.pk, Decimal("0"))
        r = reserviert(request.mandant, a) if b or q else Decimal("0")
        zeilen.append({"artikel": a, "bestand": b, "reserviert": r, "verfuegbar": b - r,
                       "wert": b * a.durchschnitts_ek if a.durchschnitts_ek else None})
    for a in Artikel.objects.filter(pk__in=[k for k, s in summen.items() if s], durchschnitts_ek__isnull=False):
        lagerwert += summen[a.pk] * a.durchschnitts_ek
    return render(request, "lager/uebersicht.html", {
        "zeilen": zeilen, "q": q, "nur_bestand": nur_bestand, "lagerwert": lagerwert.quantize(Decimal("0.01"))})


@mit_mandant
def artikel_detail(request, artikel_id):
    artikel = get_object_or_404(Artikel, pk=artikel_id)
    daten = ArtikelMandant.objects.filter(artikel=artikel, mandant=request.mandant).first()
    b = bestand(request.mandant, artikel)
    r = reserviert(request.mandant, artikel)
    return render(request, "lager/artikel.html", {
        "artikel": artikel, "daten": daten, "bestand": b, "reserviert": r, "verfuegbar": b - r,
        "bewegungen": Lagerbewegung.objects.filter(mandant=request.mandant, artikel=artikel).select_related("beleg", "benutzer")[:100],
        "arten": [(a.value, a.label) for a in MANUELL]})


@mit_mandant
@require_POST
def buchen(request, artikel_id):
    artikel = get_object_or_404(Artikel, pk=artikel_id)
    try:
        menge = zahl(request.POST.get("menge"))
        if menge is None:
            raise ValueError("Menge fehlt.")
        buche_manuell(request.mandant, artikel, request.POST.get("art"), menge,
                      request.POST.get("bemerkung", "").strip(), request.user)
        messages.success(request, "Gebucht.")
    except ValueError as fehler:
        messages.error(request, str(fehler))
    return redirect("lager:artikel", artikel_id=artikel.pk)


@mit_mandant
@require_POST
def inventur_buchen(request, artikel_id):
    artikel = get_object_or_404(Artikel, pk=artikel_id)
    try:
        gezaehlt = zahl(request.POST.get("gezaehlt"))
        if gezaehlt is None or gezaehlt < 0:
            raise ValueError("Gezählter Bestand fehlt.")
        buchung = inventur(request.mandant, artikel, gezaehlt, request.user)
        messages.success(request, f"Differenz {buchung.menge:+} gebucht." if buchung else "Keine Differenz.")
    except ValueError as fehler:
        messages.error(request, str(fehler))
    return redirect("lager:artikel", artikel_id=artikel.pk)


@mit_mandant
@require_POST
def stamm(request, artikel_id):
    """Lagerplatz und Mindestbestand pro Mandant."""
    artikel = get_object_or_404(Artikel, pk=artikel_id)
    daten, _ = ArtikelMandant.objects.get_or_create(artikel=artikel, mandant=request.mandant)
    daten.lagerplatz = request.POST.get("lagerplatz", "").strip()[:40]
    mindest = request.POST.get("mindestbestand", "").strip()
    daten.mindestbestand = int(mindest) if mindest.isdigit() else None
    daten.save()
    messages.success(request, "Gespeichert.")
    return redirect("lager:artikel", artikel_id=artikel.pk)
