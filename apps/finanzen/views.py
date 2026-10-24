from datetime import date
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.stammdaten.models import Kunde
from apps.verkauf.mail import MailFehler, senden
from apps.verkauf.models import Beleg, Belegart
from apps.verkauf.views import mit_mandant

from . import services
from .mahntext import text_fuer
from .models import Mahnung, Zahlart
from .pdf import mahnung_pdf


def betrag(text):
    try:
        return Decimal((text or "").strip().replace(".", "").replace(",", ".") if "," in (text or "") else (text or "").strip())
    except InvalidOperation:
        raise ValueError(f"Ungültiger Betrag: {text}")


def datum(text):
    try:
        return date.fromisoformat(text) if text else date.today()
    except ValueError:
        raise ValueError(f"Ungültiges Datum: {text}")


@mit_mandant
def offene_posten(request):
    kunde = Kunde.objects.filter(pk=request.GET.get("kunde") or None).first()
    zeilen = services.offene_rechnungen(request.mandant, kunde)
    if request.GET.get("nur") == "ueberfaellig":
        zeilen = [z for z in zeilen if z["ueberfaellig"] > 0]
    summe = sum((z["offen"] for z in zeilen), Decimal("0"))
    ueberfaellig = sum((z["offen"] for z in zeilen if z["ueberfaellig"] > 0), Decimal("0"))
    return render(request, "finanzen/offene_posten.html", {
        "zeilen": zeilen, "summe": summe, "ueberfaellig": ueberfaellig, "kunde": kunde,
        "nur": request.GET.get("nur", "")})


@mit_mandant
def beleg_op(request, beleg_id):
    beleg = get_object_or_404(Beleg, pk=beleg_id, mandant=request.mandant)
    return render(request, "finanzen/beleg_op.html", {
        "beleg": beleg, "zahlungen": beleg.zahlungen.all(), "bezahlt": services.bezahlt(beleg),
        "gutschrift": services.gutgeschrieben(beleg), "offen": services.offen(beleg),
        "faellig": services.faellig_am(beleg), "mahnungen": beleg.mahnungen.all(), "zahlarten": Zahlart.choices,
        "heute": date.today().isoformat()})


@mit_mandant
@require_POST
def zahlung_buchen(request, beleg_id):
    beleg = get_object_or_404(Beleg, pk=beleg_id, mandant=request.mandant)
    try:
        services.zahlung_buchen(beleg, betrag(request.POST.get("betrag")), datum(request.POST.get("datum")),
                                request.POST.get("zahlart", Zahlart.UEBERWEISUNG),
                                request.POST.get("bemerkung", "").strip(), request.user)
        messages.success(request, "Zahlung gebucht.")
    except ValueError as fehler:
        messages.error(request, str(fehler))
    return redirect("finanzen:beleg", beleg_id=beleg.pk)


@mit_mandant
def kontoauszug(request, kunde_id):
    kunde = get_object_or_404(Kunde, pk=kunde_id)
    zeilen, saldo = services.kontoauszug(request.mandant, kunde)
    return render(request, "finanzen/kontoauszug.html", {"kunde": kunde, "zeilen": zeilen, "saldo": saldo})


@mit_mandant
def mahnwesen(request):
    return render(request, "finanzen/mahnwesen.html", {
        "vorschlag": services.mahnvorschlag(request.mandant),
        "bisher": Mahnung.objects.filter(beleg__mandant=request.mandant).select_related("beleg", "beleg__kunde")[:50]})


@mit_mandant
@require_POST
def mahnungen_erstellen(request):
    angelegt = 0
    for eintrag in request.POST.getlist("auswahl"):  # "<beleg_id>:<stufe>"
        beleg_id, stufe = eintrag.split(":")
        beleg = get_object_or_404(Beleg, pk=beleg_id, mandant=request.mandant)
        try:
            services.mahnung_erstellen(beleg, int(stufe), request.user)
            angelegt += 1
        except Exception as fehler:  # z. B. Stufe schon vorhanden
            messages.error(request, f"{beleg}: {fehler}")
    if angelegt:
        messages.success(request, f"{angelegt} Mahnung(en) erstellt – unten drucken oder per E-Mail senden.")
    return redirect("finanzen:mahnwesen")


def mahnung_holen(request, mahnung_id):
    return get_object_or_404(Mahnung.objects.select_related("beleg", "beleg__kunde", "beleg__mandant"),
                             pk=mahnung_id, beleg__mandant=request.mandant)


@mit_mandant
def mahnung_pdf_ansicht(request, mahnung_id):
    mahnung = mahnung_holen(request, mahnung_id)
    antwort = HttpResponse(mahnung_pdf(mahnung), content_type="application/pdf")
    antwort["Content-Disposition"] = f'inline; filename="Mahnung-{mahnung.beleg.nummer}-{mahnung.stufe}.pdf"'
    return antwort


@mit_mandant
def mahnung_mail(request, mahnung_id):
    from apps.verkauf.mail import adressen
    mahnung = mahnung_holen(request, mahnung_id)
    beleg = mahnung.beleg
    ap = beleg.ansprechpartner
    an = ", ".join(dict.fromkeys(
        a for a in [(ap.email if ap and ap.email else beleg.kunde.email)] + adressen(beleg.kunde.email_weitere) if a))
    betreff = f"{mahnung.bezeichnung} zu Rechnung {beleg.nummer}"
    text = text_fuer(mahnung)
    if request.method == "POST":
        an, betreff, text = request.POST.get("an", ""), request.POST.get("betreff", ""), request.POST.get("text", "")
        try:
            senden(beleg, an, betreff, text, mahnung_pdf(mahnung), f"Mahnung-{beleg.nummer}-{mahnung.stufe}.pdf", request.user)
            messages.success(request, f"{mahnung} versendet.")
            return redirect("finanzen:mahnwesen")
        except MailFehler as fehler:
            messages.error(request, str(fehler))
    return render(request, "finanzen/mahnung_mail.html", {"mahnung": mahnung, "an": an, "betreff": betreff, "text": text})
