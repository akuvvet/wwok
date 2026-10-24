from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.stammdaten.models import Artikel, Kunde
from apps.verkauf.views import mit_mandant

from . import services
from .models import Reparatur, ReparaturPosition, Status
from .pdf import schein_pdf


def zahl(text, standard=None):
    text = (text or "").strip().replace(",", ".")
    if not text:
        return standard
    try:
        return Decimal(text)
    except InvalidOperation:
        raise ValueError(f"Ungültige Zahl: {text}")


def holen(request, reparatur_id):
    return get_object_or_404(Reparatur.objects.select_related("kunde", "mandant", "rechnung"), pk=reparatur_id,
                             mandant=request.mandant)


@mit_mandant
def liste(request):
    status = request.GET.get("status", "offen")
    q = request.GET.get("q", "").strip()
    reparaturen = Reparatur.objects.filter(mandant=request.mandant).select_related("kunde")
    if status == "offen":
        reparaturen = reparaturen.exclude(status__in=[Status.ABGEHOLT, Status.STORNIERT])
    elif status in Status.values:
        reparaturen = reparaturen.filter(status=status)
    for wort in q.split():
        reparaturen = reparaturen.filter(Q(nummer__icontains=wort) | Q(geraet__icontains=wort) | Q(seriennummer__icontains=wort)
                                         | Q(kunde__name1__icontains=wort) | Q(kunde__nummer__icontains=wort))
    return render(request, "werkstatt/liste.html", {
        "reparaturen": reparaturen[:300], "status": status, "q": q, "stati": Status.choices})


@mit_mandant
def annahme(request):
    kunde = Kunde.objects.filter(pk=request.GET.get("kunde") or None).first()
    if request.method == "POST":
        kunde = Kunde.objects.filter(nummer=request.POST.get("kundennummer", "").strip()).first()
        if kunde is None:
            messages.error(request, "Kundennummer nicht gefunden.")
        elif not request.POST.get("geraet", "").strip() or not request.POST.get("fehler", "").strip():
            messages.error(request, "Gerät und Fehlerbeschreibung sind Pflicht.")
        else:
            artikel = Artikel.objects.filter(nummer__iexact=request.POST.get("artikel", "").strip()).first()
            reparatur = services.annehmen(
                request.mandant, kunde, request.POST["geraet"].strip(), request.POST["fehler"].strip(),
                request.POST.get("seriennummer", "").strip(), request.POST.get("zubehoer", "").strip(), artikel,
                bool(request.POST.get("garantie")), request.user)
            meldung = services.statusmail(reparatur, request.user) if request.POST.get("mail") else None
            messages.success(request, f"{reparatur.nummer} angenommen. {meldung or ''}")
            return redirect("werkstatt:detail", reparatur_id=reparatur.pk)
    return render(request, "werkstatt/annahme.html", {"kunde": kunde, "post": request.POST})


@mit_mandant
def detail(request, reparatur_id):
    reparatur = holen(request, reparatur_id)
    return render(request, "werkstatt/detail.html", {
        "r": reparatur, "positionen": reparatur.positionen.select_related("artikel"), "stati": Status.choices,
        "netto": reparatur.summe_netto, "rma": reparatur.rmas.first()})


def schutz(view):
    def umhuellt(request, *args, **kwargs):
        try:
            return view(request, *args, **kwargs)
        except ValueError as fehler:
            messages.error(request, str(fehler))
        return redirect("werkstatt:detail", reparatur_id=kwargs["reparatur_id"])
    umhuellt.__name__ = view.__name__
    return umhuellt


@mit_mandant
@require_POST
@schutz
def status(request, reparatur_id):
    reparatur = holen(request, reparatur_id)
    meldung = services.status_setzen(reparatur, request.POST.get("status"), request.user)
    messages.success(request, f"Status: {reparatur.get_status_display()}. {meldung or ''}")
    return redirect("werkstatt:detail", reparatur_id=reparatur.pk)


@mit_mandant
@require_POST
@schutz
def position(request, reparatur_id):
    reparatur = holen(request, reparatur_id)
    art = request.POST.get("art", ReparaturPosition.TEIL)
    artikel = None
    nummer = request.POST.get("artikel", "").strip()
    if nummer:
        artikel = Artikel.objects.filter(nummer__iexact=nummer).select_related("steuersatz").first()
        if artikel is None:
            raise ValueError(f"Artikel '{nummer}' nicht gefunden.")
    bezeichnung = request.POST.get("bezeichnung", "").strip() or (artikel.bezeichnung if artikel else "")
    if not bezeichnung:
        raise ValueError("Bezeichnung fehlt.")
    preis = zahl(request.POST.get("preis"))
    if preis is None and artikel:
        from apps.stammdaten.preise import verkaufspreis
        preis = verkaufspreis(artikel, reparatur.kunde)[0]
    if preis is None:
        raise ValueError("Preis fehlt.")
    services.position_hinzufuegen(reparatur, art, bezeichnung, zahl(request.POST.get("menge"), Decimal("1")), preis, artikel)
    return redirect("werkstatt:detail", reparatur_id=reparatur.pk)


@mit_mandant
@require_POST
@schutz
def position_loeschen(request, reparatur_id, position_id):
    reparatur = holen(request, reparatur_id)
    if reparatur.rechnung_id or reparatur.teile_gebucht:
        raise ValueError("Es gibt bereits eine Rechnung bzw. gebuchte Teile; Positionen sind gesperrt.")
    get_object_or_404(ReparaturPosition, pk=position_id, reparatur=reparatur).delete()
    return redirect("werkstatt:detail", reparatur_id=reparatur.pk)


@mit_mandant
@require_POST
@schutz
def rechnung(request, reparatur_id):
    reparatur = holen(request, reparatur_id)
    beleg = services.rechnung_erzeugen(reparatur, request.user)
    messages.success(request, "Rechnung als Entwurf angelegt – prüfen und abschließen.")
    return redirect("verkauf:beleg", beleg_id=beleg.pk)


@mit_mandant
@require_POST
@schutz
def garantie_buchen(request, reparatur_id):
    reparatur = holen(request, reparatur_id)
    anzahl = services.garantieteile_buchen(reparatur, request.user)
    messages.success(request, f"{anzahl} Teil(e) als Werkstattverbrauch abgebucht.")
    return redirect("werkstatt:detail", reparatur_id=reparatur.pk)


@mit_mandant
@require_POST
def notiz(request, reparatur_id):
    reparatur = holen(request, reparatur_id)
    reparatur.interne_notiz = request.POST.get("interne_notiz", "")
    reparatur.garantie = bool(request.POST.get("garantie")) if not reparatur.rechnung_id else reparatur.garantie
    reparatur.seriennummer = request.POST.get("seriennummer", reparatur.seriennummer).strip()
    reparatur.save()
    messages.success(request, "Gespeichert.")
    return redirect("werkstatt:detail", reparatur_id=reparatur.pk)


@mit_mandant
def schein(request, reparatur_id, art):
    reparatur = holen(request, reparatur_id)
    antwort = HttpResponse(schein_pdf(reparatur, abholung=art == "abhol"), content_type="application/pdf")
    antwort["Content-Disposition"] = f'inline; filename="{reparatur.nummer}-{art}.pdf"'
    return antwort


@mit_mandant
def reklamation(request, beleg_id):
    """Kunde reklamiert ein Gerät: aus einer Rechnung oder einem Lieferschein einen Werkstattauftrag anlegen.
    Seriennummer, Artikel und Garantie (Belegdatum + Garantiemonate des Artikels, sonst 24 Monate) werden vorbelegt."""
    from datetime import date

    from apps.verkauf.models import Beleg, Belegart
    beleg = get_object_or_404(Beleg.objects.select_related("kunde"), pk=beleg_id, mandant=request.mandant,
                              art__in=[Belegart.RECHNUNG, Belegart.LIEFERSCHEIN])
    heute = date.today()
    zeilen = []
    for p in beleg.positionen.filter(artikel__isnull=False).exclude(artikel__typ__in=["L", "G"]).select_related("artikel"):
        monate = p.artikel.garantie_monate or 24
        jahr, monat = divmod(beleg.belegdatum.month - 1 + monate, 12)
        try:
            ende = beleg.belegdatum.replace(year=beleg.belegdatum.year + jahr, month=monat + 1)
        except ValueError:  # 29.02. → 28.02.
            ende = beleg.belegdatum.replace(year=beleg.belegdatum.year + jahr, month=monat + 1, day=28)
        zeilen.append({"position": p, "seriennummern": ", ".join(sn.nummer for sn in p.seriennummern.all()),
                       "garantie_bis": ende, "in_garantie": ende >= heute})
    if request.method == "POST":
        try:
            position = next(z for z in zeilen if str(z["position"].pk) == request.POST.get("position"))
        except StopIteration:
            messages.error(request, "Bitte das reklamierte Gerät wählen.")
            return render(request, "werkstatt/reklamation.html", {"beleg": beleg, "zeilen": zeilen})
        if not request.POST.get("fehler", "").strip():
            messages.error(request, "Bitte beschreiben, was defekt ist.")
            return render(request, "werkstatt/reklamation.html", {"beleg": beleg, "zeilen": zeilen})
        p = position["position"]
        reparatur = services.annehmen(
            request.mandant, beleg.kunde, p.bezeichnung[:200], request.POST["fehler"].strip(),
            request.POST.get("seriennummer", "").strip(), "", p.artikel, bool(request.POST.get("garantie")), request.user)
        reparatur.interne_notiz = f"Reklamation zu {beleg} vom {beleg.belegdatum:%d.%m.%Y} (Garantie bis {position['garantie_bis']:%d.%m.%Y})"
        reparatur.save()
        messages.success(request, f"Werkstattauftrag {reparatur.nummer} zur Reklamation angelegt.")
        return redirect("werkstatt:detail", reparatur_id=reparatur.pk)
    return render(request, "werkstatt/reklamation.html", {"beleg": beleg, "zeilen": zeilen})
