from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.kern.models import Mandant
from apps.stammdaten.models import Artikel, ArtikelMandant, Kategorie, keine_leistung
from apps.stammdaten.preise import gruppen_kalkulation
from apps.verkauf.views import mit_mandant

from apps.verkauf.models import Beleg, Position

from .models import Bewegungsart, Lagerbewegung, Seriennummer, bestand, reserviert
from .services import (buche_manuell, inventur, retoure_aus_gutschrift, seriennummern_einlagern,
                       seriennummern_zuordnen, umbuchen, zeilen)

MANUELL = [Bewegungsart.WARENEINGANG, Bewegungsart.EIGENVERBRAUCH, Bewegungsart.RETOURE, Bewegungsart.KORREKTUR]


def zahl(text, standard=None):
    text = (text or "").strip().replace(",", ".")
    if not text:
        return standard
    try:
        wert = Decimal(text)
    except InvalidOperation:
        raise ValueError(f"Ungültige Zahl: {text}")
    if not wert.is_finite():
        raise ValueError(f"Ungültige Zahl: {text}")
    return wert


def suche(q):
    artikel = Artikel.objects.filter(nicht_aktiv=False)
    for wort in q.split():
        artikel = artikel.filter(
            Q(nummer__icontains=wort) | Q(bezeichnung__icontains=wort) | Q(herstellernummer__icontains=wort)
            | Q(hersteller__name__icontains=wort) | Q(ean__icontains=wort))
    return artikel


SEITENGROESSEN = (25, 50, 100, 200)


@mit_mandant
def uebersicht(request):
    q = request.GET.get("q", "").strip()
    nur_bestand = request.GET.get("bestand") == "1"
    kategorie = request.GET.get("kategorie", "")
    try:
        pro_seite = int(request.GET.get("pro_seite", 50))
    except ValueError:
        pro_seite = 50
    pro_seite = pro_seite if pro_seite in SEITENGROESSEN else 50
    summen = {s["artikel"]: s["s"] for s in Lagerbewegung.objects.filter(mandant=request.mandant)
              .values("artikel").annotate(s=Sum("menge"))}
    artikel = suche(q) if q else Artikel.objects.filter(nicht_aktiv=False)
    if kategorie.isdigit():  # gewählte Kategorie samt Unterkategorien (bis zu drei Ebenen)
        ids = {int(kategorie)}
        for _ in range(3):
            ids |= set(Kategorie.objects.filter(eltern_id__in=ids).values_list("pk", flat=True))
        artikel = artikel.filter(kategorie_id__in=ids)
    if nur_bestand:
        artikel = artikel.filter(pk__in=[a for a, s in summen.items() if s])
    seite = Paginator(artikel.select_related("steuersatz").order_by("nummer"), pro_seite).get_page(request.GET.get("seite"))
    zeilen = []
    for a in seite.object_list:
        b = summen.get(a.pk, Decimal("0"))
        r = reserviert(request.mandant, a) if b else Decimal("0")
        zeilen.append({"artikel": a, "bestand": b, "reserviert": r, "verfuegbar": b - r})
    lagerwert = Decimal("0")
    for a in Artikel.objects.filter(pk__in=[k for k, s in summen.items() if s], durchschnitts_ek__isnull=False):
        lagerwert += summen[a.pk] * a.durchschnitts_ek
    parameter = request.GET.copy()
    parameter.pop("seite", None)
    return render(request, "lager/uebersicht.html", {
        "zeilen": zeilen, "q": q, "nur_bestand": nur_bestand, "lagerwert": lagerwert.quantize(Decimal("0.01")),
        "seite": seite, "parameter": parameter.urlencode(), "kategorie": kategorie, "pro_seite": pro_seite,
        "seitengroessen": SEITENGROESSEN, "kategorien": Kategorie.objects.filter(eltern__isnull=True)})


@mit_mandant
def artikel_detail(request, artikel_id):
    artikel = get_object_or_404(Artikel, pk=artikel_id)
    daten = ArtikelMandant.objects.filter(artikel=artikel, mandant=request.mandant).first()
    b = bestand(request.mandant, artikel)
    r = reserviert(request.mandant, artikel)
    gruppe = None
    if artikel.typ == "G":  # Bestand einer Gruppe ergibt sich aus den Komponenten
        from .models import gruppen_verfuegbar
        gruppe = gruppen_verfuegbar(request.mandant, artikel)
    from apps.einkauf.models import WareneingangPosition
    from apps.einkauf.services import offen_bestellt
    letzter_we = WareneingangPosition.objects.filter(
        artikel=artikel, wareneingang__mandant=request.mandant, wareneingang__gebucht=True
    ).select_related("wareneingang__lieferant").order_by("-wareneingang__datum", "-id").first()
    return render(request, "lager/artikel.html", {
        "bestellt": offen_bestellt(request.mandant, artikel), "letzter_we": letzter_we,
        "lieferanten": artikel.lieferanten.select_related("lieferant").order_by("ek"),
        "gruppe_verfuegbar": gruppe, "kalkulation": gruppen_kalkulation(artikel) if artikel.typ == "G" else None,
        "artikel": artikel, "daten": daten, "bestand": b, "reserviert": r, "verfuegbar": b - r,
        "bewegungen": Lagerbewegung.objects.filter(mandant=request.mandant, artikel=artikel).select_related("beleg", "benutzer")[:100],
        "arten": [(a.value, a.label) for a in MANUELL],
        "seriennummern": Seriennummer.objects.filter(mandant=request.mandant, artikel=artikel).select_related("position__beleg")[:200],
        "andere_mandanten": Mandant.objects.filter(aktiv=True).exclude(pk=request.mandant.pk)})


@mit_mandant
@require_POST
def buchen(request, artikel_id):
    artikel = get_object_or_404(Artikel, pk=artikel_id)
    try:
        keine_leistung(artikel)
        menge = zahl(request.POST.get("menge"))
        if menge is None:
            raise ValueError("Menge fehlt.")
        art = request.POST.get("art")
        if art not in [a.value for a in MANUELL]:
            raise ValueError("Diese Buchungsart ist hier nicht erlaubt.")
        seriennummern = zeilen(request.POST.get("seriennummern"))
        if seriennummern and art not in (Bewegungsart.WARENEINGANG, Bewegungsart.RETOURE):
            raise ValueError("Seriennummern nur bei Wareneingang oder Retoure.")
        if seriennummern and len(seriennummern) != abs(menge):
            raise ValueError(f"{len(seriennummern)} Seriennummern, aber Menge {abs(menge):f}.")
        with transaction.atomic():  # schlägt die Seriennummer fehl, wird auch die Bestandsbuchung zurückgenommen
            buche_manuell(request.mandant, artikel, art, menge, request.POST.get("bemerkung", "").strip(), request.user)
            if seriennummern:
                seriennummern_einlagern(request.mandant, artikel, chr(10).join(seriennummern), request.POST.get("bemerkung", "").strip())
        messages.success(request, "Gebucht.")
    except ValueError as fehler:
        messages.error(request, str(fehler))
    return redirect("lager:artikel", artikel_id=artikel.pk)


@mit_mandant
@require_POST
def inventur_buchen(request, artikel_id):
    artikel = get_object_or_404(Artikel, pk=artikel_id)
    try:
        keine_leistung(artikel)
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
    try:
        keine_leistung(artikel)
    except ValueError as fehler:
        messages.error(request, str(fehler))
        return redirect("lager:artikel", artikel_id=artikel.pk)
    daten, _ = ArtikelMandant.objects.get_or_create(artikel=artikel, mandant=request.mandant)
    daten.lagerplatz = request.POST.get("lagerplatz", "").strip()[:40]
    mindest = request.POST.get("mindestbestand", "").strip()
    daten.mindestbestand = int(mindest) if mindest.isdigit() else None
    daten.save()
    messages.success(request, "Gespeichert.")
    return redirect("lager:artikel", artikel_id=artikel.pk)


@mit_mandant
@require_POST
def umbuchung(request, artikel_id):
    artikel = get_object_or_404(Artikel, pk=artikel_id)
    try:
        keine_leistung(artikel)
        ziel = get_object_or_404(Mandant, pk=request.POST.get("nach"), aktiv=True)
        umbuchen(request.mandant, ziel, artikel, zahl(request.POST.get("menge"), Decimal("0")), request.user)
        messages.success(request, f"Nach {ziel.kurzname} umgebucht.")
    except ValueError as fehler:
        messages.error(request, str(fehler))
    return redirect("lager:artikel", artikel_id=artikel.pk)


@mit_mandant
def seriennummer_suche(request):
    q = request.GET.get("q", "").strip()
    treffer = Seriennummer.objects.filter(mandant=request.mandant, nummer__icontains=q).select_related(
        "artikel", "position__beleg__kunde")[:100] if q else []
    from apps.werkstatt.models import Reparatur
    reparaturen = Reparatur.objects.filter(mandant=request.mandant, seriennummer__icontains=q).select_related("kunde")[:50] if q else []
    return render(request, "lager/seriennummern.html", {"q": q, "treffer": treffer, "reparaturen": reparaturen})


@mit_mandant
@require_POST
def position_seriennummern(request, position_id):
    position = get_object_or_404(Position.objects.select_related("beleg", "artikel"), pk=position_id,
                                 beleg__mandant=request.mandant)
    try:
        if not position.artikel_id:
            raise ValueError("Nur bei Artikelpositionen.")
        position.beleg.stand_pruefen()
        seriennummern_zuordnen(position, request.POST.get("seriennummern", ""))
        messages.success(request, "Seriennummern gespeichert.")
    except ValueError as fehler:
        messages.error(request, str(fehler))
    return redirect("verkauf:beleg", beleg_id=position.beleg_id)


@mit_mandant
@require_POST
def gutschrift_retoure(request, beleg_id):
    beleg = get_object_or_404(Beleg, pk=beleg_id, mandant=request.mandant)
    try:
        anzahl = retoure_aus_gutschrift(beleg, request.user)
        messages.success(request, f"{anzahl} Artikel ins Lager zurückgebucht.")
    except ValueError as fehler:
        messages.error(request, str(fehler))
    return redirect("verkauf:beleg", beleg_id=beleg.pk)


@mit_mandant
def inventur_liste(request):
    """Zählliste mit Erfassung: gezählte Mengen eintragen, Differenzen werden gebucht."""
    summen = {s["artikel"]: s["s"] for s in Lagerbewegung.objects.filter(mandant=request.mandant)
              .values("artikel").annotate(s=Sum("menge"))}
    plaetze = {d.artikel_id: d.lagerplatz for d in ArtikelMandant.objects.filter(mandant=request.mandant)}
    kategorie = request.GET.get("kategorie", "")
    artikel = Artikel.objects.filter(pk__in=[a for a, s in summen.items() if s], typ="A")
    if kategorie.isdigit():
        artikel = artikel.filter(kategorie_id=int(kategorie))
    if request.method == "POST":
        gebucht = 0
        for a in artikel:
            wert = request.POST.get(f"gezaehlt_{a.pk}", "").strip()
            if wert:
                try:
                    gebucht += bool(inventur(request.mandant, a, zahl(wert), request.user))
                except ValueError as fehler:
                    messages.error(request, f"{a.nummer}: {fehler}")
        messages.success(request, f"{gebucht} Differenzbuchung(en) erstellt.")
        return redirect(request.get_full_path())
    zeilen_ = sorted(({"artikel": a, "soll": summen.get(a.pk, Decimal("0")), "platz": plaetze.get(a.pk, "")}
                      for a in artikel.select_related("kategorie")), key=lambda z: (z["platz"], z["artikel"].nummer))
    return render(request, "lager/inventur.html", {
        "zeilen": zeilen_, "kategorie": kategorie, "kategorien": Kategorie.objects.filter(eltern__isnull=True)})
