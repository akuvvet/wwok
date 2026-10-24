from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.kern.models import Mandant
from apps.stammdaten.models import (Ansprechpartner, Anschriftart, Artikel, Kunde, Zahlungsbedingung)

from .models import Beleg, BelegGesperrt, Belegart, Position


def dezimal(text, standard=None):
    text = (text or "").strip().replace(".", "").replace(",", ".") if "," in (text or "") else (text or "").strip()
    if not text:
        return standard
    try:
        return Decimal(text)
    except InvalidOperation:
        raise ValueError(f"Ungültige Zahl: {text}")


def mandant_oder_none(request):
    mandant_id = request.session.get("mandant_id")
    if mandant_id:
        return Mandant.objects.filter(pk=mandant_id, aktiv=True).first()
    aktive = Mandant.objects.filter(aktiv=True)
    if aktive.count() == 1:
        request.session["mandant_id"] = aktive[0].pk
        return aktive[0]
    return None


def mit_mandant(view):
    """Login nötig und ein Mandant gewählt; sonst zur Startseite."""
    @login_required
    def umhuellt(request, *args, **kwargs):
        mandant = mandant_oder_none(request)
        if mandant is None:
            messages.info(request, "Bitte zuerst einen Mandanten wählen.")
            return redirect("verkauf:start")
        request.mandant = mandant
        return view(request, *args, **kwargs)
    umhuellt.__name__ = view.__name__
    return umhuellt


def beleg_holen(request, beleg_id):
    return get_object_or_404(Beleg.objects.select_related("kunde", "mandant"), pk=beleg_id, mandant=request.mandant)


@login_required
def start(request):
    if request.method == "POST":
        mandant = get_object_or_404(Mandant, pk=request.POST.get("mandant"), aktiv=True)
        request.session["mandant_id"] = mandant.pk
        return redirect("verkauf:start")
    mandant = mandant_oder_none(request)
    q = request.GET.get("q", "").strip()
    treffer = None
    if mandant and q:
        if q == "*":
            filt = Q()
        elif q.endswith("*"):
            filt = Q(name1__istartswith=q[:-1]) | Q(nummer__istartswith=q[:-1])
        else:
            filt = Q(name1__icontains=q) | Q(nummer__icontains=q) | Q(ort__icontains=q)
        treffer = Kunde.objects.filter(filt, anonymisiert=False)[:100]
        if len(treffer) == 1:
            return redirect("verkauf:kunde", kunde_id=treffer[0].pk)
    return render(request, "verkauf/start.html", {
        "mandant": mandant, "mandanten": Mandant.objects.filter(aktiv=True), "q": q, "treffer": treffer})


@mit_mandant
def kunde_detail(request, kunde_id):
    kunde = get_object_or_404(Kunde, pk=kunde_id)
    belege = Beleg.objects.filter(kunde=kunde, mandant=request.mandant).select_related("kunde")
    art = request.GET.get("art", "")
    if art == "offen":
        belege = belege.filter(abgeschlossen=False)
    elif art in Belegart.values:
        belege = belege.filter(art=art)
    mandantendaten = kunde.mandantendaten.filter(mandant=request.mandant).first()
    return render(request, "verkauf/kunde.html", {
        "kunde": kunde, "belege": belege[:200], "filter": art, "arten": Belegart.choices,
        "kreditlimit": mandantendaten.kreditlimit if mandantendaten else None,
        "ansprechpartner": kunde.ansprechpartner.all()})


def adresse_text(kunde, art):
    a = kunde.anschriften.filter(art=art).order_by("-standard").first()
    quelle = a or kunde
    zeilen = [quelle.name1, quelle.name2, quelle.name3, quelle.strasse, f"{quelle.plz} {quelle.ort}".strip()]
    if quelle.land and quelle.land != "DE":
        zeilen.append(quelle.land)
    return "\n".join(z for z in zeilen if z)


@mit_mandant
@require_POST
def beleg_neu(request, kunde_id):
    kunde = get_object_or_404(Kunde, pk=kunde_id)
    art = request.POST.get("art")
    if art not in Belegart.values:
        raise Http404
    if kunde.gesperrt:
        messages.error(request, "Kunde ist gesperrt – kein neuer Beleg.")
        return redirect("verkauf:kunde", kunde_id=kunde.pk)
    beleg = Beleg.objects.create(
        mandant=request.mandant, art=art, kunde=kunde,
        ansprechpartner=kunde.ansprechpartner.filter(hauptkontakt=True).first(),
        zahlungsbedingung=kunde.zahlungsbedingung,
        rechnungsanschrift=adresse_text(kunde, Anschriftart.RECHNUNG),
        versandanschrift=adresse_text(kunde, Anschriftart.VERSAND))
    return redirect("verkauf:beleg", beleg_id=beleg.pk)


@mit_mandant
def beleg_detail(request, beleg_id):
    beleg = beleg_holen(request, beleg_id)
    return render(request, "verkauf/beleg.html", {
        "beleg": beleg, "positionen": beleg.positionen.select_related("artikel"),
        "zahlungsbedingungen": Zahlungsbedingung.objects.all(),
        "ansprechpartner": beleg.kunde.ansprechpartner.all()})


def sperr_schutz(view):
    def umhuellt(request, *args, **kwargs):
        try:
            return view(request, *args, **kwargs)
        except BelegGesperrt as fehler:
            messages.error(request, str(fehler))
        except ValueError as fehler:
            messages.error(request, str(fehler))
        return redirect("verkauf:beleg", beleg_id=kwargs["beleg_id"])
    umhuellt.__name__ = view.__name__
    return umhuellt


@mit_mandant
@require_POST
@sperr_schutz
def beleg_kopf(request, beleg_id):
    beleg = beleg_holen(request, beleg_id)
    beleg.versandart = request.POST.get("versandart", "").strip()
    beleg.zahlungsbedingung = Zahlungsbedingung.objects.filter(pk=request.POST.get("zahlungsbedingung") or None).first()
    if not beleg.gesperrt:
        beleg.ansprechpartner = Ansprechpartner.objects.filter(
            pk=request.POST.get("ansprechpartner") or None, kunde=beleg.kunde).first()
        beleg.rechnungsanschrift = request.POST.get("rechnungsanschrift", "")
        beleg.versandanschrift = request.POST.get("versandanschrift", "")
        beleg.bemerkung = request.POST.get("bemerkung", "")
        beleg.versandkosten = dezimal(request.POST.get("versandkosten"), Decimal("0"))
    beleg.save()
    if not beleg.gesperrt:
        beleg.berechne()
    messages.success(request, "Gespeichert.")
    return redirect("verkauf:beleg", beleg_id=beleg.pk)


@mit_mandant
@require_POST
@sperr_schutz
def position_neu(request, beleg_id):
    beleg = beleg_holen(request, beleg_id)
    typ = request.POST.get("typ", "A")
    nummer = request.POST.get("artikel", "").strip()
    artikel = Artikel.objects.filter(nummer__iexact=nummer, nicht_aktiv=False).select_related("steuersatz").first() if nummer else None
    if nummer and not artikel:
        raise ValueError(f"Artikel '{nummer}' nicht gefunden.")
    bezeichnung = request.POST.get("bezeichnung", "").strip() or (artikel.bezeichnung if artikel else "")
    if not bezeichnung:
        raise ValueError("Artikel oder Bezeichnung fehlt.")
    preis = dezimal(request.POST.get("preis"))
    if preis is None:
        preis = vorschlag_preis(artikel, beleg.kunde) if artikel else None
    if preis is None:
        raise ValueError("Preis fehlt.")
    Position.objects.create(
        beleg=beleg, typ=artikel.typ if artikel else typ, artikel=artikel, bezeichnung=bezeichnung,
        menge=dezimal(request.POST.get("menge"), Decimal("1")), einzelpreis=preis,
        rabatt_prozent=dezimal(request.POST.get("rabatt"), Decimal("0")),
        steuer_prozent=artikel.steuersatz.prozent if artikel else Decimal("19"))
    beleg.berechne()
    return redirect("verkauf:beleg", beleg_id=beleg.pk)


@mit_mandant
@require_POST
@sperr_schutz
def position_loeschen(request, beleg_id, position_id):
    beleg = beleg_holen(request, beleg_id)
    get_object_or_404(Position, pk=position_id, beleg=beleg).delete()
    beleg.berechne()
    return redirect("verkauf:beleg", beleg_id=beleg.pk)


@mit_mandant
@require_POST
@sperr_schutz
def beleg_abschliessen(request, beleg_id):
    beleg = beleg_holen(request, beleg_id)
    beleg.abschliessen()
    messages.success(request, f"{beleg} abgeschlossen.")
    return redirect("verkauf:beleg", beleg_id=beleg.pk)


@mit_mandant
@require_POST
@sperr_schutz
def beleg_folgebeleg(request, beleg_id):
    beleg = beleg_holen(request, beleg_id)
    neu = beleg.folgebeleg_erzeugen()
    return redirect("verkauf:beleg", beleg_id=neu.pk)


def vorschlag_preis(artikel, kunde):
    preis = getattr(artikel, f"vk{kunde.preisstufe}", None)
    return preis if preis is not None else artikel.vk1


@mit_mandant
def api_artikel(request):
    """Artikelsuche für die Positionserfassung: Preis nach Preisstufe, letzter Preis des Kunden."""
    q = request.GET.get("q", "").strip()
    kunde = Kunde.objects.filter(pk=request.GET.get("kunde")).first()
    if len(q) < 2:
        return JsonResponse({"treffer": []})
    artikel = Artikel.objects.filter(nicht_aktiv=False)
    for wort in q.split():  # jedes Wort muss irgendwo vorkommen, Reihenfolge egal
        artikel = artikel.filter(
            Q(nummer__icontains=wort) | Q(bezeichnung__icontains=wort) | Q(herstellernummer__icontains=wort)
            | Q(bezeichnung_erweitert__icontains=wort) | Q(hersteller__name__icontains=wort) | Q(ean__icontains=wort))
    artikel = artikel.select_related("steuersatz")[:20]
    treffer = []
    for a in artikel:
        letzte = None
        if kunde:
            p = Position.objects.filter(artikel=a, beleg__kunde=kunde, beleg__mandant=request.mandant) \
                .select_related("beleg").order_by("-beleg__belegdatum", "-id").first()
            if p:
                letzte = {"preis": str(p.einzelpreis), "datum": p.beleg.belegdatum.strftime("%d.%m.%Y")}
        preis = vorschlag_preis(a, kunde) if kunde else a.vk1
        treffer.append({"nummer": a.nummer, "bezeichnung": a.bezeichnung, "typ": a.typ,
                        "preis": str(preis) if preis is not None else "", "letzter": letzte})
    return JsonResponse({"treffer": treffer})
