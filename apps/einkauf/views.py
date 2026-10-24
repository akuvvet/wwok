from datetime import date
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.stammdaten.models import Artikel, Lieferant
from apps.verkauf.mail import MailFehler, senden_an
from apps.verkauf.models import Beleg
from apps.verkauf.views import mit_mandant
from apps.werkstatt.models import Reparatur

from . import erechnung, sepa, services
from .models import (BestellPosition, BestellStatus, Bestellung, Lieferantenrechnung, Rma, RmaStatus, RechnungStatus,
                     Wareneingang, WareneingangPosition)
from .pdf import bestellung_pdf


def zahl(text, standard=None):
    text = (text or "").strip()
    if "," in text:
        text = text.replace(".", "").replace(",", ".")
    if not text:
        return standard
    try:
        return Decimal(text)
    except InvalidOperation:
        raise ValueError(f"Ungültige Zahl: {text}")


def tag(text, standard=None):
    try:
        return date.fromisoformat(text) if text else (standard or date.today())
    except ValueError:
        raise ValueError(f"Ungültiges Datum: {text}")


def schutz(ziel, *arg_namen):
    """Fehler als Meldung anzeigen und zur angegebenen Seite zurückleiten."""
    def deko(view):
        def umhuellt(request, *args, **kwargs):
            try:
                return view(request, *args, **kwargs)
            except (ValueError, services.Nummernkreis.DoesNotExist, sepa.SepaFehler, erechnung.ERechnungFehler) as fehler:
                messages.error(request, str(fehler))
            return redirect(ziel, **{n: kwargs[n] for n in arg_namen})
        umhuellt.__name__ = view.__name__
        return umhuellt
    return deko


# ---- Bestellungen ---------------------------------------------------------------------------------

@mit_mandant
def bestellungen(request):
    status = request.GET.get("status", "offen")
    liste = Bestellung.objects.filter(mandant=request.mandant).select_related("lieferant")
    if status == "offen":
        liste = liste.filter(status__in=[BestellStatus.ENTWURF, BestellStatus.BESTELLT, BestellStatus.TEIL])
    elif status in BestellStatus.values:
        liste = liste.filter(status=status)
    return render(request, "einkauf/bestellungen.html", {
        "bestellungen": liste[:200], "status": status, "lieferanten": Lieferant.objects.filter(gesperrt=False)})


@mit_mandant
@require_POST
def bestellung_neu(request):
    lieferant = get_object_or_404(Lieferant, pk=request.POST.get("lieferant"))
    b = services.bestellung_anlegen(request.mandant, lieferant)
    return redirect("einkauf:bestellung", bestellung_id=b.pk)


def bestellung_holen(request, bestellung_id):
    return get_object_or_404(Bestellung.objects.select_related("lieferant", "mandant"), pk=bestellung_id, mandant=request.mandant)


@mit_mandant
def bestellung_detail(request, bestellung_id):
    b = bestellung_holen(request, bestellung_id)
    return render(request, "einkauf/bestellung.html", {
        "b": b, "positionen": b.positionen.select_related("artikel"), "netto": b.summe_netto,
        "wareneingaenge": b.wareneingaenge.all(), "aus_beleg": b.aus_beleg})


@mit_mandant
@require_POST
@schutz("einkauf:bestellung", "bestellung_id")
def bestellung_position(request, bestellung_id):
    b = bestellung_holen(request, bestellung_id)
    code = request.POST.get("artikel", "").strip()
    artikel = services.artikel_per_code(code, b.lieferant) or Artikel.objects.filter(nummer__iexact=code).first()
    if artikel is None:
        raise ValueError(f"Artikel '{code}' nicht gefunden.")
    services.bestellposition(b, artikel, zahl(request.POST.get("menge"), Decimal("1")), zahl(request.POST.get("ek")))
    return redirect("einkauf:bestellung", bestellung_id=b.pk)


@mit_mandant
@require_POST
@schutz("einkauf:bestellung", "bestellung_id")
def bestellung_position_loeschen(request, bestellung_id, position_id):
    b = bestellung_holen(request, bestellung_id)
    pos = get_object_or_404(BestellPosition, pk=position_id, bestellung=b)
    if pos.geliefert:
        raise ValueError("Zur Position gibt es schon Wareneingänge.")
    pos.delete()
    return redirect("einkauf:bestellung", bestellung_id=b.pk)


@mit_mandant
@require_POST
@schutz("einkauf:bestellung", "bestellung_id")
def bestellung_bestellen(request, bestellung_id):
    b = bestellung_holen(request, bestellung_id)
    services.bestellen(b)
    messages.success(request, "Als bestellt markiert.")
    return redirect("einkauf:bestellung", bestellung_id=b.pk)


@mit_mandant
@require_POST
def bestellung_stornieren(request, bestellung_id):
    b = bestellung_holen(request, bestellung_id)
    if b.wareneingaenge.exists():
        messages.error(request, "Es gibt schon Wareneingänge – nicht stornierbar.")
    else:
        b.status = BestellStatus.STORNIERT
        b.save(update_fields=["status"])
    return redirect("einkauf:bestellung", bestellung_id=b.pk)


@mit_mandant
def bestellung_pdf_ansicht(request, bestellung_id):
    b = bestellung_holen(request, bestellung_id)
    antwort = HttpResponse(bestellung_pdf(b), content_type="application/pdf")
    antwort["Content-Disposition"] = f'inline; filename="{b.nummer}.pdf"'
    return antwort


@mit_mandant
def bestellung_mail(request, bestellung_id):
    b = bestellung_holen(request, bestellung_id)
    ap = b.lieferant.ansprechpartner.filter(Q(mandant=request.mandant) | Q(mandant__isnull=True)).exclude(email="").first()
    an = (ap.email if ap else b.lieferant.email) or ""
    betreff = f"Bestellung {b.nummer} von {request.mandant.firmenname}"
    text = (f"Sehr geehrte Damen und Herren,\n\nanbei unsere Bestellung {b.nummer}. Bitte bestätigen Sie den Eingang "
            f"und den voraussichtlichen Liefertermin.\n\nMit freundlichen Grüßen\n{request.mandant.firmenname}")
    if request.method == "POST":
        an, betreff, text = request.POST.get("an", ""), request.POST.get("betreff", ""), request.POST.get("text", "")
        try:
            senden_an(request.mandant, an, betreff, text, bestellung_pdf(b), f"{b.nummer}.pdf", bezug=b.nummer, benutzer=request.user)
            services.bestellen(b)
            messages.success(request, f"Bestellung an {an} gesendet.")
            return redirect("einkauf:bestellung", bestellung_id=b.pk)
        except (MailFehler, ValueError) as fehler:
            messages.error(request, str(fehler))
    return render(request, "einkauf/mail.html", {"b": b, "an": an, "betreff": betreff, "text": text})


@mit_mandant
@require_POST
def bestellung_aus_beleg(request, beleg_id):
    beleg = get_object_or_404(Beleg, pk=beleg_id, mandant=request.mandant)
    try:
        liste = services.bestellungen_aus_beleg(beleg)
    except ValueError as fehler:
        messages.error(request, str(fehler))
        return redirect("verkauf:beleg", beleg_id=beleg.pk)
    ohne = [p.bezeichnung for p in getattr(beleg, "_ohne_lieferant", [])]
    messages.success(request, f"{len(liste)} Bestellung(en) als Entwurf angelegt." +
                     (f" Ohne Lieferant geblieben: {', '.join(ohne)}." if ohne else ""))
    return redirect("einkauf:bestellung", bestellung_id=liste[0].pk) if len(liste) == 1 else redirect("einkauf:bestellungen")


@mit_mandant
def vorschlag(request):
    if request.method == "POST":
        auswahl = []
        for artikel_id in request.POST.getlist("auswahl"):
            auswahl.append((int(artikel_id), zahl(request.POST.get(f"menge_{artikel_id}"), Decimal("1"))))
        try:
            liste = services.bestellungen_aus_vorschlag(request.mandant, auswahl)
            messages.success(request, f"{len(liste)} Bestellung(en) als Entwurf angelegt.")
            return redirect("einkauf:bestellungen")
        except ValueError as fehler:
            messages.error(request, str(fehler))
    return render(request, "einkauf/vorschlag.html", {"zeilen": services.bestellvorschlag(request.mandant)})


# ---- Wareneingang ---------------------------------------------------------------------------------

@mit_mandant
def wareneingaenge(request):
    return render(request, "einkauf/wareneingaenge.html", {
        "liste": Wareneingang.objects.filter(mandant=request.mandant).select_related("lieferant", "bestellung")[:200],
        "lieferanten": Lieferant.objects.filter(gesperrt=False),
        "offene": Bestellung.objects.filter(mandant=request.mandant, status__in=[BestellStatus.BESTELLT, BestellStatus.TEIL])})


@mit_mandant
@require_POST
def wareneingang_neu(request):
    if request.POST.get("bestellung"):
        b = get_object_or_404(Bestellung, pk=request.POST["bestellung"], mandant=request.mandant)
        we = services.wareneingang_anlegen(request.mandant, b.lieferant, b, request.user)
        services.wareneingang_aus_bestellung(we)
    else:
        lieferant = get_object_or_404(Lieferant, pk=request.POST.get("lieferant"))
        we = services.wareneingang_anlegen(request.mandant, lieferant, None, request.user)
    return redirect("einkauf:wareneingang", we_id=we.pk)


def we_holen(request, we_id):
    return get_object_or_404(Wareneingang.objects.select_related("lieferant", "bestellung"), pk=we_id, mandant=request.mandant)


@mit_mandant
def wareneingang_detail(request, we_id):
    we = we_holen(request, we_id)
    return render(request, "einkauf/wareneingang.html", {
        "we": we, "positionen": we.positionen.select_related("artikel", "bestellposition"), "netto": we.summe_netto})


@mit_mandant
@require_POST
@schutz("einkauf:wareneingang", "we_id")
def wareneingang_scan(request, we_id):
    """Scan-Feld (Bluetooth-Scanner sendet Code + Enter): jeder Scan erhöht die Menge um 1."""
    we = we_holen(request, we_id)
    code = request.POST.get("code", "").strip()
    artikel = services.artikel_per_code(code, we.lieferant)
    if artikel is None:
        raise ValueError(f"Code '{code}' keinem Artikel zugeordnet.")
    services.wareneingang_erfassen(we, artikel, zahl(request.POST.get("menge"), Decimal("1")), zahl(request.POST.get("ek")),
                                   request.POST.get("seriennummern", ""))
    return redirect("einkauf:wareneingang", we_id=we.pk)


@mit_mandant
@require_POST
@schutz("einkauf:wareneingang", "we_id")
def wareneingang_zeile_aendern(request, we_id, position_id):
    we = we_holen(request, we_id)
    if we.gebucht:
        raise ValueError("Der Wareneingang ist bereits gebucht.")
    pos = get_object_or_404(WareneingangPosition, pk=position_id, wareneingang=we)
    if request.POST.get("aktion") == "loeschen":
        pos.delete()
    else:
        pos.menge = zahl(request.POST.get("menge"), pos.menge)
        pos.ek = zahl(request.POST.get("ek"), pos.ek)
        pos.seriennummern = request.POST.get("seriennummern", "")
        pos.save()
    return redirect("einkauf:wareneingang", we_id=we.pk)


@mit_mandant
@require_POST
@schutz("einkauf:wareneingang", "we_id")
def wareneingang_buchen(request, we_id):
    we = we_holen(request, we_id)
    we.lieferschein = request.POST.get("lieferschein", we.lieferschein).strip()
    we.save()
    anzahl = services.wareneingang_buchen(we, request.user)
    messages.success(request, f"{anzahl} Position(en) ins Lager gebucht.")
    return redirect("einkauf:wareneingang", we_id=we.pk)


# ---- Lieferantenrechnungen / SEPA -----------------------------------------------------------------

@mit_mandant
def rechnungen(request):
    status = request.GET.get("status", "offen")
    liste = Lieferantenrechnung.objects.filter(mandant=request.mandant).select_related("lieferant")
    if status in RechnungStatus.values:
        liste = liste.filter(status=status)
    elif status == "offen":
        liste = liste.exclude(status=RechnungStatus.BEZAHLT)
    zeilen = [(r, r.abgleich(), r.zahlbetrag()) for r in liste[:300]]
    return render(request, "einkauf/rechnungen.html", {
        "zeilen": zeilen, "status": status, "lieferanten": Lieferant.objects.filter(gesperrt=False),
        "heute": date.today().isoformat(), "summe": sum((z[2] for z in zeilen), Decimal("0"))})


@mit_mandant
@require_POST
@schutz("einkauf:rechnungen")
def rechnung_neu(request):
    lieferant = get_object_or_404(Lieferant, pk=request.POST.get("lieferant"))
    netto, brutto = zahl(request.POST.get("netto")), zahl(request.POST.get("brutto"))
    if brutto is None or not request.POST.get("rechnungsnummer", "").strip():
        raise ValueError("Rechnungsnummer und Bruttobetrag sind Pflicht.")
    if netto is None:
        netto = (brutto / Decimal("1.19")).quantize(Decimal("0.01"))
    r = services.rechnung_erfassen(request.mandant, lieferant, request.POST["rechnungsnummer"].strip(),
                                   tag(request.POST.get("datum")), netto, brutto)
    return redirect("einkauf:rechnung", rechnung_id=r.pk)


@mit_mandant
@require_POST
@schutz("einkauf:rechnungen")
def rechnung_erechnung(request):
    datei = request.FILES.get("datei")
    if not datei:
        raise ValueError("Bitte eine XRechnung-/ZUGFeRD-Datei (XML oder PDF) wählen.")
    daten = erechnung.lese(datei.name, datei.read())
    lieferant = Lieferant.objects.filter(ust_id__iexact=daten["ust_id"]).first() if daten["ust_id"] else None
    lieferant = lieferant or Lieferant.objects.filter(name1__iexact=daten["lieferant"]).first() \
        or Lieferant.objects.filter(name1__icontains=daten["lieferant"][:20]).first() if daten["lieferant"] else lieferant
    if lieferant is None:
        raise ValueError(f"Lieferant '{daten['lieferant']}' nicht gefunden – bitte zuerst als Lieferant anlegen.")
    r = services.rechnung_erfassen(request.mandant, lieferant, daten["nummer"], daten["datum"], daten["netto"],
                                   daten["brutto"], daten["steuer"], quelle="e-rechnung")
    if daten["faellig"]:
        r.faellig = daten["faellig"]
        r.save()
    messages.success(request, f"E-Rechnung {r.rechnungsnummer} von {lieferant.name1} übernommen.")
    return redirect("einkauf:rechnung", rechnung_id=r.pk)


@mit_mandant
def rechnung_detail(request, rechnung_id):
    r = get_object_or_404(Lieferantenrechnung.objects.select_related("lieferant"), pk=rechnung_id, mandant=request.mandant)
    if request.method == "POST":
        ids = request.POST.getlist("wareneingang")
        r.wareneingaenge.set(Wareneingang.objects.filter(pk__in=ids, mandant=request.mandant, lieferant=r.lieferant))
        messages.success(request, "Verknüpfung gespeichert.")
        return redirect("einkauf:rechnung", rechnung_id=r.pk)
    verknuepft = set(r.wareneingaenge.values_list("pk", flat=True))
    kandidaten = Wareneingang.objects.filter(mandant=request.mandant, lieferant=r.lieferant, gebucht=True) \
        .filter(Q(rechnungen__isnull=True) | Q(pk__in=verknuepft)).distinct()
    return render(request, "einkauf/rechnung.html", {
        "r": r, "abgleich": r.abgleich(), "zahlbetrag": r.zahlbetrag(), "kandidaten": kandidaten, "verknuepft": verknuepft,
        "heute": date.today().isoformat()})


@mit_mandant
@require_POST
@schutz("einkauf:rechnung", "rechnung_id")
def rechnung_bezahlt(request, rechnung_id):
    r = get_object_or_404(Lieferantenrechnung, pk=rechnung_id, mandant=request.mandant)
    r.status = RechnungStatus.BEZAHLT
    r.bezahlt_am = tag(request.POST.get("datum"))
    r.bezahlter_betrag = zahl(request.POST.get("betrag"), r.zahlbetrag(r.bezahlt_am))
    r.save()
    return redirect("einkauf:rechnung", rechnung_id=r.pk)


@mit_mandant
@require_POST
@schutz("einkauf:rechnungen")
def sepa_export(request):
    auswahl = Lieferantenrechnung.objects.filter(pk__in=request.POST.getlist("auswahl"), mandant=request.mandant) \
        .exclude(status=RechnungStatus.BEZAHLT).select_related("lieferant")
    ausfuehrung = tag(request.POST.get("ausfuehrung"))
    daten = sepa.erzeuge(request.mandant, list(auswahl), ausfuehrung)
    auswahl.update(status=RechnungStatus.EXPORTIERT)
    antwort = HttpResponse(daten, content_type="application/xml")
    antwort["Content-Disposition"] = f'attachment; filename="SEPA_{request.mandant.kurzname}_{ausfuehrung:%Y%m%d}.xml"'
    return antwort


# ---- RMA --------------------------------------------------------------------------------------------

@mit_mandant
def rma_liste(request):
    return render(request, "einkauf/rma_liste.html", {
        "liste": Rma.objects.filter(mandant=request.mandant).select_related("lieferant"),
        "lieferanten": Lieferant.objects.filter(gesperrt=False)})


@mit_mandant
@require_POST
@schutz("einkauf:rma_liste")
def rma_neu(request):
    lieferant = get_object_or_404(Lieferant, pk=request.POST.get("lieferant"))
    artikel = Artikel.objects.filter(nummer__iexact=request.POST.get("artikel", "").strip()).first()
    bezeichnung = request.POST.get("bezeichnung", "").strip() or (artikel.bezeichnung if artikel else "")
    if not bezeichnung or not request.POST.get("fehler", "").strip():
        raise ValueError("Bezeichnung und Fehlerbeschreibung sind Pflicht.")
    rma = services.rma_anlegen(request.mandant, lieferant, bezeichnung, request.POST["fehler"].strip(), artikel,
                               request.POST.get("seriennummer", "").strip())
    return redirect("einkauf:rma", rma_id=rma.pk)


@mit_mandant
def rma_aus_reparatur(request, reparatur_id):
    """Werkstattauftrag in eine Lieferanten-RMA übernehmen (Lieferant wählen)."""
    rep = get_object_or_404(Reparatur, pk=reparatur_id, mandant=request.mandant)
    if request.method == "POST":
        lieferant = get_object_or_404(Lieferant, pk=request.POST.get("lieferant"))
        rma = services.rma_anlegen(request.mandant, lieferant, rep.geraet, rep.fehler, rep.artikel, rep.seriennummer, rep)
        messages.success(request, f"{rma.nummer} angelegt; Werkstattauftrag steht auf „in Lieferanten-RMA“.")
        return redirect("einkauf:rma", rma_id=rma.pk)
    return render(request, "einkauf/rma_aus_reparatur.html", {"rep": rep, "lieferanten": Lieferant.objects.filter(gesperrt=False)})


@mit_mandant
def rma_detail(request, rma_id):
    rma = get_object_or_404(Rma.objects.select_related("lieferant", "reparatur"), pk=rma_id, mandant=request.mandant)
    if request.method == "POST":
        try:
            rma.status = request.POST.get("status", rma.status)
            rma.lieferanten_rma_nr = request.POST.get("lieferanten_rma_nr", "").strip()
            rma.gutschrift_betrag = zahl(request.POST.get("gutschrift_betrag"))
            rma.notiz = request.POST.get("notiz", "")
            if rma.status == RmaStatus.EINGESENDET and not rma.eingesendet_am:
                rma.eingesendet_am = date.today()
            if rma.status in (RmaStatus.ERSATZ, RmaStatus.GUTSCHRIFT, RmaStatus.ABGESCHLOSSEN, RmaStatus.ABGELEHNT) and not rma.erledigt_am:
                rma.erledigt_am = date.today()
            rma.save()
            messages.success(request, "Gespeichert.")
        except ValueError as fehler:
            messages.error(request, str(fehler))
        return redirect("einkauf:rma", rma_id=rma.pk)
    return render(request, "einkauf/rma.html", {"rma": rma, "stati": RmaStatus.choices})
