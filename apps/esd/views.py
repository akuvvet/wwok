from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.stammdaten.models import Artikel, Kategorie
from apps.verkauf.mail import MailFehler, senden_an
from apps.verkauf.models import Beleg, Position
from apps.verkauf.views import mit_mandant

from . import services
from .models import ESDKey, ESDVergabe, KeyStatus, VergabeStatus


def software_kategorien():
    """IDs der Kategorie 'Software' samt aller Unterkategorien; None, wenn es keine solche Kategorie gibt."""
    wurzeln = list(Kategorie.objects.filter(name__icontains="software", eltern__isnull=True).values_list("pk", flat=True))
    if not wurzeln:
        return None
    ids = set(wurzeln)
    for _ in range(5):
        ids |= set(Kategorie.objects.filter(eltern_id__in=ids).values_list("pk", flat=True))
    return ids


@mit_mandant
def uebersicht(request):
    zeilen = []
    for a in Artikel.objects.filter(esd_key=True).order_by("nummer"):
        zeilen.append({"artikel": a, **services.bestand(request.mandant, a)})
    q = request.GET.get("q", "").strip()
    ids = software_kategorien()
    kandidaten = []
    if q:  # nur Artikel der Kategorie Software anbieten
        kandidaten = list(kandidaten_suchen(q)[:50])
    return render(request, "esd/uebersicht.html", {"zeilen": zeilen, "q": q, "kandidaten": kandidaten, "software_vorhanden": ids is not None})


def kandidaten_suchen(q):
    """Software-Artikel (Typ A) zu einer Suche mit beliebigen Wörtern; '*' = alle."""
    ids = software_kategorien()
    qs = Artikel.objects.filter(nicht_aktiv=False, typ="A")
    if ids is not None:
        qs = qs.filter(kategorie_id__in=ids)
    for wort in ([] if q == "*" else q.split()):
        qs = qs.filter(Q(nummer__icontains=wort) | Q(bezeichnung__icontains=wort) | Q(herstellernummer__icontains=wort)
                       | Q(hersteller__name__icontains=wort))
    return qs.select_related("kategorie").order_by("nummer")


@mit_mandant
def suche(request):
    """Suchfeld mit Vorschlagsliste für 'Keys hinzufügen'."""
    q = request.GET.get("q", "").strip()
    if len(q) < 2 and q != "*":
        return JsonResponse({"treffer": []})
    return JsonResponse({"treffer": [
        {"pk": a.pk, "nummer": a.nummer, "bezeichnung": a.bezeichnung, "kategorie": a.kategorie.name if a.kategorie else "",
         "esd_key": a.esd_key} for a in kandidaten_suchen(q)[:40]]})


@mit_mandant
@require_POST
def aktivieren(request, artikel_id):
    """Artikel der Kategorie Software als ESD-Key-Artikel kennzeichnen, damit Keys importiert werden können."""
    a = get_object_or_404(Artikel, pk=artikel_id, typ="A")
    ids = software_kategorien()
    if ids is not None and a.kategorie_id not in ids:
        messages.error(request, "Keys lassen sich nur für Artikel der Kategorie Software hinzufügen.")
        return redirect("esd:uebersicht")
    if not a.esd_key:
        a.esd_key = True
        a.save()
    return redirect("esd:artikel", artikel_id=a.pk)


@mit_mandant
def artikel(request, artikel_id):
    a = get_object_or_404(Artikel, pk=artikel_id, esd_key=True)
    if request.method == "POST":
        try:
            maximal = int(request.POST.get("max_vergaben") or 1)
            neu, doppelt = services.importieren(request.mandant, a, request.POST.get("keys", ""), maximal)
            messages.success(request, f"{neu} Key(s) importiert, {doppelt} bereits vorhanden.")
        except ValueError as fehler:
            messages.error(request, str(fehler))
        return redirect("esd:artikel", artikel_id=a.pk)
    status = request.GET.get("status", "")
    keys = ESDKey.objects.filter(mandant=request.mandant, artikel=a).prefetch_related("vergaben__kunde")
    if status == "frei":
        keys = [k for k in keys if k.frei > 0]
    elif status in KeyStatus.values:
        keys = keys.filter(status=status)
    seite = Paginator(keys, 50).get_page(request.GET.get("seite"))
    parameter = request.GET.copy()
    parameter.pop("seite", None)
    return render(request, "esd/artikel.html", {
        "artikel": a, "seite": seite, "parameter": parameter.urlencode(), "status": status,
        "bestand": services.bestand(request.mandant, a)})


@mit_mandant
@require_POST
def key_status(request, key_id):
    key = get_object_or_404(ESDKey, pk=key_id, mandant=request.mandant)
    key.status = KeyStatus.GESPERRT if key.status == KeyStatus.AKTIV else KeyStatus.AKTIV
    key.save(update_fields=["status"])
    return redirect("esd:artikel", artikel_id=key.artikel_id)


def beleg_holen(request, beleg_id):
    return get_object_or_404(Beleg.objects.select_related("kunde", "mandant"), pk=beleg_id, mandant=request.mandant)


@mit_mandant
def beleg(request, beleg_id):
    b = beleg_holen(request, beleg_id)
    positionen = [p for p in b.positionen.select_related("artikel") if p.artikel_id and p.artikel.esd_key]
    return render(request, "esd/beleg.html", {
        "b": b, "vergaben": b.esd_vergaben.select_related("key", "position").order_by("position_id", "id"),
        "positionen": [{"position": p, "vergeben": services.vergeben_menge(p), "bestand": services.bestand(b.mandant, p.artikel)["frei"]}
                       for p in positionen]})


@mit_mandant
@require_POST
def zuordnen(request, beleg_id):
    b = beleg_holen(request, beleg_id)
    position = get_object_or_404(Position, pk=request.POST.get("position"), beleg=b)
    try:
        anzahl = int(request.POST.get("anzahl") or 1)
        vergaben = services.zuordnen(position, anzahl, bool(request.POST.get("doppelt")), request.POST.get("bemerkung", ""), request.user)
        messages.success(request, f"{len(vergaben)} Key(s) zugeordnet.")
    except ValueError as fehler:
        messages.error(request, str(fehler))
    return redirect("esd:beleg", beleg_id=b.pk)


@mit_mandant
@require_POST
def rueckgabe(request, vergabe_id):
    v = get_object_or_404(ESDVergabe.objects.select_related("beleg"), pk=vergabe_id, beleg__mandant=request.mandant)
    try:
        services.rueckgabe(v, request.POST.get("entscheidung"), request.user)
        messages.success(request, "Key freigegeben." if request.POST.get("entscheidung") == "freigeben" else "Key gesperrt.")
    except ValueError as fehler:
        messages.error(request, str(fehler))
    return redirect("esd:beleg", beleg_id=v.beleg_id)


@mit_mandant
def mail(request, beleg_id):
    b = beleg_holen(request, beleg_id)
    gruppen = services.keys_text(b)
    if not gruppen:
        messages.error(request, "Zu diesem Beleg wurden noch keine Keys vergeben.")
        return redirect("esd:beleg", beleg_id=b.pk)
    from apps.verkauf.mail import adressen, anrede
    ap = b.ansprechpartner
    an = (ap.email if ap and ap.email else b.kunde.email) or ""
    betreff = f"Ihre Lizenzschlüssel zu {b.get_art_display()} {b.nummer or ''}".strip()
    zeilen = "\n".join(f"{name}:\n  " + "\n  ".join(keys) for name, keys in gruppen)
    text = (f"{anrede(b)},\n\nanbei Ihre Lizenzschlüssel zu {b.get_art_display()} {b.nummer or ''}:\n\n{zeilen}\n\n"
            f"Mit freundlichen Grüßen\n{b.mandant.firmenname}")
    if request.method == "POST":
        an, betreff, text = request.POST.get("an", ""), request.POST.get("betreff", ""), request.POST.get("text", "")
        try:
            senden_an(b.mandant, an, betreff, text, beleg=b, bezug="ESD-Keys", benutzer=request.user)
            messages.success(request, f"Keys an {an} gesendet.")
            return redirect("esd:beleg", beleg_id=b.pk)
        except MailFehler as fehler:
            messages.error(request, str(fehler))
    return render(request, "esd/mail.html", {"b": b, "an": an, "betreff": betreff, "text": text})
