import uuid
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.db import transaction
from django.db.models import Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.stammdaten.models import Kunde
from apps.kern.rechte import nur_admin
from apps.kern.seiten import blaettern
from apps.verkauf.mail import SAMMELVERSAND_MAX, MailFehler
from apps.verkauf.models import Beleg, Belegart
from apps.verkauf.views import mit_mandant

from . import bank as bankmodul
from . import datev as datevmodul
from . import anzahlung as anzahlungmodul
from . import mahnversand
from . import services
from .mahntext import mail_fuer
from . import lastschrift as lastschriftmodul
from .models import Anzahlungsverrechnung, Gutschriftverrechnung, Lastschrift, Mahnung, Zahlart
from .pdf import mahnung_pdf


def betrag(text):
    """Betrag mit Komma oder Punkt; mehrdeutige Schreibweisen wie 1.234 und mehr als zwei Nachkommastellen werden abgelehnt."""
    t = (text or "").strip()
    if "," not in t and t.count(".") == 1 and len(t.split(".")[1]) == 3:
        raise ValueError(f"Betrag '{text}' ist mehrdeutig – bitte mit Komma schreiben, z. B. 1234,00.")
    try:
        wert = Decimal(t.replace(".", "").replace(",", ".") if "," in t else t)
    except InvalidOperation:
        raise ValueError(f"Ungültiger Betrag: {text}")
    if not wert.is_finite() or abs(wert) >= Decimal("1000000000"):
        raise ValueError(f"Ungültiger Betrag: {text}")
    if wert != wert.quantize(Decimal("0.01")):
        raise ValueError(f"Der Betrag darf höchstens zwei Nachkommastellen haben: {text}")
    return wert


def datum(text):
    try:
        return date.fromisoformat(text) if text else date.today()
    except ValueError:
        raise ValueError(f"Ungültiges Datum: {text}")


OP_SORTIERUNG = {
    "faellig": lambda z: (z["faellig"] or date.max, z["beleg"].pk),
    "ueberfaellig": lambda z: (z["ueberfaellig"], z["beleg"].pk),
    "offen": lambda z: (z["offen"], z["beleg"].pk),
}


def op_text(z):
    k = z["beleg"].kunde
    return " ".join([z["beleg"].nummer or "", k.name1 or "", k.name2 or "", k.nummer or ""]).lower()


@mit_mandant
def offene_posten(request):
    kunde_id = request.GET.get("kunde", "")
    kunde = Kunde.objects.filter(pk=kunde_id if kunde_id.isdigit() else None).first()
    q = request.GET.get("q", "").strip()
    sort = request.GET.get("sort", "")
    schluessel = sort.lstrip("-")
    if schluessel not in OP_SORTIERUNG:
        sort = schluessel = ""
    absteigend = sort.startswith("-")
    zeilen = sorted(services.offene_rechnungen(request.mandant, kunde) + services.offene_gutschriften(request.mandant, kunde),
                    key=lambda z: (z["beleg"].belegdatum, z["beleg"].pk))
    if schluessel:
        zeilen.sort(key=OP_SORTIERUNG[schluessel], reverse=absteigend)
    if request.GET.get("nur") == "ueberfaellig":
        zeilen = [z for z in zeilen if z["ueberfaellig"] > 0]
    for wort in q.lower().split():
        zeilen = [z for z in zeilen if wort in op_text(z)]
    summe = sum((z["offen"] for z in zeilen), Decimal("0"))
    ueberfaellig = sum((z["offen"] for z in zeilen if z["ueberfaellig"] > 0), Decimal("0"))
    seite, parameter = blaettern(request, zeilen)
    Beleg.versand_markieren(z["beleg"] for z in seite)  # Versand-Symbol in der Liste
    # Spaltenköpfe: erster Klick aufsteigend, zweiter absteigend; übrige Filter bleiben erhalten
    basis = request.GET.copy()
    basis.pop("seite", None)
    basis.pop("sort", None)
    basis = basis.urlencode()
    kopf = {}
    for name in OP_SORTIERUNG:
        neu = "-" + name if sort == name else name
        kopf[name] = {"url": (f"?{basis}&" if basis else "?") + f"sort={neu}",
                      "pfeil": ("▼" if absteigend else "▲") if schluessel == name else ""}
    proformas = anzahlungmodul.offene_proformas(request.mandant, kunde)
    for wort in q.lower().split():
        proformas = [z for z in proformas if wort in op_text(z)]
    return render(request, "finanzen/offene_posten.html", {
        "proformas": proformas, "proformas_summe": sum((z["offen"] for z in proformas), Decimal("0")),
        "zeilen": seite, "seite": seite, "parameter": parameter, "summe": summe, "ueberfaellig": ueberfaellig,
        "kunde": kunde, "nur": request.GET.get("nur", ""), "q": q, "sort": sort, "kopf": kopf})


def vorlaeufig_gesperrt(request, *belege):
    """Zahlungen und Verrechnungen gibt es erst nach „Speichern: abschließen & Nummer vergeben“: Meldung und Rückkehr zum Beleg."""
    for b in belege:
        try:
            b.vorlaeufig_sperre("Zahlungen und Verrechnungen gehen")
        except ValueError as fehler:
            messages.error(request, str(fehler))
            return redirect("verkauf:beleg", beleg_id=b.pk)
    return None


@mit_mandant
def beleg_op(request, beleg_id):
    beleg = get_object_or_404(Beleg, pk=beleg_id, mandant=request.mandant)
    if (gesperrt := vorlaeufig_gesperrt(request, beleg)) is not None:
        return gesperrt
    offen = services.offen(beleg)
    gutschriften = []
    if beleg.art == Belegart.RECHNUNG and beleg.abgeschlossen:
        for z in services.offene_gutschriften(request.mandant, beleg.kunde):  # Gutschriften zu dieser Rechnung zuerst
            rest = -z["offen"]
            gutschriften.append({"beleg": z["beleg"], "rest": rest, "vorschlag": min(rest, offen) if offen > 0 else rest})
        gutschriften.sort(key=lambda g: g["beleg"].vorgaenger_id != beleg.pk)
    guthaben = anzahlungmodul.guthaben(beleg) if beleg.art in anzahlungmodul.GUTHABENARTEN else Decimal("0")
    quelle = beleg.art in anzahlungmodul.QUELLARTEN or guthaben > 0
    andere = []
    if guthaben > 0:  # Guthaben mit anderen offenen Rechnungen desselben Kunden verrechnen
        for z in services.offene_rechnungen(request.mandant, beleg.kunde):
            if z["offen"] > 0:
                andere.append({"beleg": z["beleg"], "offen": z["offen"], "vorschlag": min(z["offen"], guthaben)})
    ist_gs = beleg.art == Belegart.GUTSCHRIFT
    rechnungen_fuer_gs = []
    if ist_gs and beleg.abgeschlossen:  # freien Betrag der Gutschrift mit offenen Rechnungen des Kunden verrechnen (auch teilweise)
        frei = services.gutschrift_rest(beleg)
        if frei >= services.CENT:
            for z in services.offene_rechnungen(request.mandant, beleg.kunde):
                if z["offen"] >= services.CENT:
                    rechnungen_fuer_gs.append({"beleg": z["beleg"], "offen": z["offen"], "vorschlag": min(z["offen"], frei)})
            rechnungen_fuer_gs.sort(key=lambda r: r["beleg"].pk != beleg.vorgaenger_id)  # die eigene Rechnung zuerst
    return render(request, "finanzen/beleg_op.html", {
        "weg": services.zahlungsweg(beleg), "gs_rest": services.gutschrift_rest(beleg) if ist_gs else None,
        "rechnungen_fuer_gs": rechnungen_fuer_gs,
        # Kopf der Gutschrift: nur diese Gutschrift (der Zahlungsweg umfasst alle Gutschriften ihrer Rechnung)
        "gs_verrechnet": (beleg.verrechnungen.aggregate(s=Sum("betrag"))["s"] or Decimal("0")) if ist_gs else None,
        "gs_ausgezahlt": -services.bezahlt(beleg) if ist_gs else None,
        "quelle": quelle, "guthaben": guthaben, "andere_rechnungen": andere,
        # Zahlung buchen: freie Guthaben des Kunden zum Verrechnen vorschlagen (nur für offene Rechnungen)
        "kunden_guthaben": [{**g, "vorschlag": min(g["guthaben"], offen)} for g in anzahlungmodul.kunden_guthaben(
            request.mandant, beleg.kunde, ausser=beleg)] if beleg.art == Belegart.RECHNUNG and beleg.abgeschlossen and offen > 0 else [],
        "abgegebene": beleg.anzahlung_verrechnungen.select_related("rechnung", "storniert_durch") if beleg.art == Belegart.RECHNUNG else [],
        "pf_status": anzahlungmodul.proforma_status(beleg) if beleg.art == Belegart.PROFORMA else None,
        "anzahlungen": (beleg.anzahlungen if beleg.art == Belegart.RECHNUNG else beleg.anzahlung_verrechnungen)
        .select_related("quelle", "rechnung", "storniert_durch"),
        "angerechnet": services.angerechnet(beleg),
        "beleg": beleg, "zahlungen": beleg.zahlungen.all(), "bezahlt": services.bezahlt(beleg),
        "gutschrift": services.gutgeschrieben(beleg), "offen": offen, "gutschriften": gutschriften,
        "verrechnungen": beleg.gutschrift_verrechnungen.select_related("gutschrift", "storniert_durch"),
        "skonto": services.skonto(beleg), "faellig": services.faellig_am(beleg), "mahnungen": beleg.mahnungen.all(), "zahlarten": Zahlart.choices,
        "heute": date.today().isoformat()})


@mit_mandant
@require_POST
def zahlung_buchen(request, beleg_id):
    beleg = get_object_or_404(Beleg, pk=beleg_id, mandant=request.mandant)
    if (gesperrt := vorlaeufig_gesperrt(request, beleg)) is not None:
        return gesperrt
    try:
        # offen_bisher: offener Betrag, den die Maske gezeigt hat; weicht er ab, wurde schon gebucht (Doppelklick)
        bisher = request.POST.get("offen_bisher")
        if "skonto" in request.POST and beleg.art == Belegart.RECHNUNG:  # Maske mit Feld Skonto: Rest von Hand gewährt oder offen
            skonto_feld = request.POST.get("skonto", "").strip()
            z, gewaehrt = services.zahlung_mit_skonto(
                beleg, betrag(request.POST.get("betrag")), betrag(skonto_feld) if skonto_feld else Decimal("0"),
                datum(request.POST.get("datum")), request.POST.get("zahlart", Zahlart.UEBERWEISUNG),
                request.POST.get("bemerkung", "").strip(), request.user, offen_bisher=betrag(bisher) if bisher else None)
            rest = services.offen(beleg)
            messages.success(request, "Zahlung gebucht." + (f" {gewaehrt.betrag:.2f} € Skonto gewährt.".replace(".", ",", 1) if gewaehrt else "")
                             + (" Die Rechnung ist ausgeglichen." if abs(rest) < services.CENT else
                                f" Noch offen: {rest:.2f} €.".replace(".", ",", 1) if rest > 0 else ""))
            return redirect("finanzen:beleg", beleg_id=beleg.pk)
        z = services.zahlung_buchen(beleg, betrag(request.POST.get("betrag")), datum(request.POST.get("datum")),
                                    request.POST.get("zahlart", Zahlart.UEBERWEISUNG),
                                    request.POST.get("bemerkung", "").strip(), request.user,
                                    offen_bisher=betrag(bisher) if bisher else None)
        messages.success(request, "Zahlung gebucht." + (f" {z.skonto.betrag:.2f} € Skonto gewährt, die Rechnung ist ausgeglichen.".replace(".", ",", 1)
                                                       if z.skonto else ""))
    except ValueError as fehler:
        messages.error(request, str(fehler))
    return redirect("finanzen:beleg", beleg_id=beleg.pk)


@mit_mandant
@require_POST
def gutschrift_auszahlen(request, beleg_id):
    """Freien Betrag einer Gutschrift direkt an den Kunden auszahlen (Entscheidung des Nutzers 19.11.2026): negative Zahlung
    auf der Gutschrift, höchstens bis zum freien Betrag. frei_bisher schützt vor Doppelklick und veralteter Seite."""
    from django.db import transaction
    gutschrift = get_object_or_404(Beleg, pk=beleg_id, mandant=request.mandant, art=Belegart.GUTSCHRIFT)
    if (gesperrt := vorlaeufig_gesperrt(request, gutschrift)) is not None:
        return gesperrt
    try:
        wert = betrag(request.POST.get("betrag"))
        if wert is None or wert <= 0:
            raise ValueError("Bitte den auszuzahlenden Betrag (größer als 0) eintragen.")
        zahlart = request.POST.get("zahlart", Zahlart.UEBERWEISUNG)
        if zahlart not in Zahlart.values or zahlart == Zahlart.SKONTO:  # Skonto ist kein Geldfluss
            raise ValueError("Bitte eine Zahlart für die Auszahlung wählen (Überweisung, Bar …).")
        with transaction.atomic():
            Beleg.objects.select_for_update().get(pk=gutschrift.pk)
            bisher = request.POST.get("frei_bisher")
            if bisher and betrag(bisher) != services.gutschrift_rest(gutschrift):
                raise ValueError("Der freie Betrag hat sich inzwischen geändert (evtl. schon ausgezahlt oder verrechnet). Bitte prüfen.")
            services.zahlung_buchen(gutschrift, -wert, datum(request.POST.get("datum")), zahlart,
                                    request.POST.get("bemerkung", "").strip() or "Auszahlung an den Kunden", request.user)
        messages.success(request, f"{wert:.2f} € aus {gutschrift} an den Kunden ausgezahlt.".replace(".", ",", 1))
    except ValueError as fehler:
        messages.error(request, str(fehler))
    return redirect("finanzen:beleg", beleg_id=gutschrift.pk)


@mit_mandant
@require_POST
@nur_admin
def zahlung_storno(request, zahlung_id):
    from .models import Zahlung
    zahlung = get_object_or_404(Zahlung.objects.select_related("beleg"), pk=zahlung_id, mandant=request.mandant)
    try:
        services.zahlung_stornieren(zahlung, request.POST.get("grund"), request.user)
        messages.success(request, "Zahlung storniert (Gegenbuchung erstellt).")
    except ValueError as fehler:
        messages.error(request, str(fehler))
    return redirect("finanzen:beleg", beleg_id=zahlung.beleg_id)


@mit_mandant
@require_POST
def gutschrift_verrechnen(request, beleg_id):
    rechnung = get_object_or_404(Beleg, pk=beleg_id, mandant=request.mandant)
    nummer = request.POST.get("gutschrift", "")
    gutschrift = get_object_or_404(Beleg, pk=nummer if nummer.isdigit() else None, mandant=request.mandant)
    if (gesperrt := vorlaeufig_gesperrt(request, rechnung, gutschrift)) is not None:
        return gesperrt
    try:
        services.gutschrift_verrechnen(gutschrift, rechnung, betrag(request.POST.get("betrag")), request.user,
                                       betrag(request.POST.get("offen_bisher")))
        messages.success(request, f"{gutschrift} verrechnet.")
    except ValueError as fehler:
        messages.error(request, str(fehler))
    if request.POST.get("zurueck") == "gutschrift":  # von der Zahlungsseite der Gutschrift aus verrechnet
        return redirect("finanzen:beleg", beleg_id=gutschrift.pk)
    return redirect("finanzen:beleg", beleg_id=rechnung.pk)


@mit_mandant
@require_POST
@nur_admin
def verrechnung_aufheben(request, verrechnung_id):
    verrechnung = get_object_or_404(Gutschriftverrechnung, pk=verrechnung_id, mandant=request.mandant)
    try:
        services.verrechnung_aufheben(verrechnung, request.POST.get("grund"), request.user)
        messages.success(request, "Verrechnung aufgehoben (Gegenbuchung erstellt).")
    except ValueError as fehler:
        messages.error(request, str(fehler))
    return redirect("finanzen:beleg", beleg_id=verrechnung.rechnung_id)


@mit_mandant
@require_POST
def anzahlung_verrechnen(request, beleg_id):
    quelle = get_object_or_404(Beleg, pk=beleg_id, mandant=request.mandant)
    nummer = request.POST.get("rechnung", "")
    rechnung = get_object_or_404(Beleg, pk=nummer if nummer.isdigit() else None, mandant=request.mandant)
    if (gesperrt := vorlaeufig_gesperrt(request, quelle, rechnung)) is not None:
        return gesperrt
    try:
        bisher = request.POST.get("guthaben_bisher")
        anzahlungmodul.anzahlung_verrechnen(quelle, rechnung, betrag(request.POST.get("betrag")), request.user,
                                            betrag(bisher) if bisher else None)
        messages.success(request, f"Guthaben aus {quelle} mit {rechnung} verrechnet.")
    except ValueError as fehler:
        messages.error(request, str(fehler))
    zurueck = request.POST.get("zurueck", "")  # von der Zahlungsseite der Zielrechnung aus: dorthin zurück
    return redirect("finanzen:beleg", beleg_id=rechnung.pk if zurueck == "rechnung" else quelle.pk)


@mit_mandant
@require_POST
@nur_admin
def anzahlung_aufheben(request, verrechnung_id):
    verrechnung = get_object_or_404(Anzahlungsverrechnung, pk=verrechnung_id, mandant=request.mandant)
    try:
        anzahlungmodul.anzahlung_aufheben(verrechnung, request.POST.get("grund"), request.user)
        messages.success(request, "Verrechnung aufgehoben (Gegenbuchung erstellt).")
    except ValueError as fehler:
        messages.error(request, str(fehler))
    return redirect("finanzen:beleg", beleg_id=request.POST.get("zurueck") if (request.POST.get("zurueck") or "").isdigit()
                    else verrechnung.rechnung_id)


@mit_mandant
def kontoauszug(request, kunde_id):
    kunde = get_object_or_404(Kunde, pk=kunde_id)
    zeilen, saldo = services.kontoauszug(request.mandant, kunde)
    return render(request, "finanzen/kontoauszug.html", {"kunde": kunde, "zeilen": zeilen, "saldo": saldo})


@mit_mandant
def mahnwesen(request):
    bisher = Mahnung.objects.filter(beleg__mandant=request.mandant).select_related("beleg", "beleg__kunde")
    nur_offen = request.GET.get("nur") == "offen"  # weder gesendet noch gedruckt
    if nur_offen:
        bisher = bisher.filter(gesendet_am__isnull=True, gedruckt_am__isnull=True)
    return render(request, "finanzen/mahnwesen.html", {
        "vorschlag": services.mahnvorschlag(request.mandant), "bisher": bisher[:50], "nur_offen": nur_offen})


@mit_mandant
@require_POST
def mahnungen_erstellen(request):
    angelegt = 0
    for eintrag in request.POST.getlist("auswahl"):  # "<beleg_id>:<stufe>"
        try:
            beleg_id, stufe = eintrag.split(":")
            beleg_id, stufe = int(beleg_id), int(stufe)
        except ValueError:
            messages.error(request, "Ungültige Auswahl für die Mahnung.")
            continue
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
    mahnung = mahnung_holen(request, mahnung_id)
    an = mahnversand.empfaenger(mahnung)
    betreff, text = mail_fuer(mahnung)
    if request.method == "POST":
        an, betreff, text = request.POST.get("an", ""), request.POST.get("betreff", ""), request.POST.get("text", "")
        try:
            mahnversand.mahnung_senden(mahnung, an, betreff, text, request.user)
            messages.success(request, f"{mahnung} versendet.")
            return redirect("finanzen:mahnwesen")
        except MailFehler as fehler:
            messages.error(request, str(fehler))
    return render(request, "finanzen/mahnung_mail.html", {"mahnung": mahnung, "an": an, "betreff": betreff, "text": text})


@mit_mandant
@require_POST
def mahnungen_sammel(request):
    """Mehrere Mahnungen auf einmal: ein gemeinsames PDF zum Drucken oder Sammelversand nach bestätigter Übersicht."""
    ids = [int(i) for i in request.POST.getlist("mahnung") if i.isdigit()]
    mahnungen = list(Mahnung.objects.filter(pk__in=ids, beleg__mandant=request.mandant)
                     .select_related("beleg", "beleg__kunde", "beleg__mandant", "beleg__ansprechpartner").order_by("beleg__kunde__name1", "id"))
    if not mahnungen:
        messages.error(request, "Keine Mahnung ausgewählt.")
        return redirect("finanzen:mahnwesen")
    if request.POST.get("aktion") == "drucken":
        antwort = HttpResponse(mahnversand.sammel_pdf(mahnungen), content_type="application/pdf")
        antwort["Content-Disposition"] = f'inline; filename="Mahnungen-{timezone.localdate():%Y-%m-%d}.pdf"'
        return antwort
    if not request.POST.get("bestaetigt"):  # erst die Übersicht mit Empfängern zeigen
        zeilen = [{"mahnung": m, "an": mahnversand.empfaenger(m), "betreff": mail_fuer(m)[0]} for m in mahnungen]
        frei = SAMMELVERSAND_MAX  # vorausgewählt werden die ersten 15 noch nicht gesendeten mit Adresse
        for z in zeilen:
            z["gewaehlt"] = bool(z["an"]) and not z["mahnung"].gesendet_am and frei > 0
            frei -= z["gewaehlt"]
        return render(request, "finanzen/mahnung_sammelversand.html", {
            "zeilen": zeilen, "sendbar": sum(1 for z in zeilen if z["an"]), "ohne": sum(1 for z in zeilen if not z["an"]),
            "max": SAMMELVERSAND_MAX})
    if sum(1 for m in mahnungen if mahnversand.empfaenger(m)) > SAMMELVERSAND_MAX:
        messages.error(request, f"Höchstens {SAMMELVERSAND_MAX} Mahnungen je Versand – nichts gesendet. Bitte in mehreren Durchgängen senden.")
        return redirect("finanzen:mahnwesen")
    gesendet, ohne_adresse = 0, []
    for m in mahnungen:
        if not mahnversand.empfaenger(m):
            ohne_adresse.append(f"{m.beleg.nummer} ({m.beleg.kunde.name1})")
            continue
        try:
            mahnversand.mahnung_senden(m, benutzer=request.user)
            gesendet += 1
        except MailFehler as fehler:
            messages.error(request, f"{m.beleg.nummer} ({m.beleg.kunde.name1}): {fehler}")
    if gesendet:
        messages.success(request, f"{gesendet} Mahnung(en) per E-Mail gesendet.")
    if ohne_adresse:
        messages.warning(request, "Ohne E-Mail-Adresse, nicht gesendet – bitte drucken: " + ", ".join(ohne_adresse))
    return redirect("finanzen:mahnwesen")


# ---- Lieferanten: dieselbe Liste wie Einkauf → Lieferantenrechnungen, nach Aufgabe getrennt -----------

def op_lieferanten(request):
    """Offene Posten Lieferanten: unbezahlte Rechnungen, Rechnung erfassen, E-Rechnung einlesen."""
    from apps.einkauf.views import rechnungen
    return rechnungen(request, sicht="op")


def zahlungen_lieferanten(request):
    """Zahlungen Lieferanten: Rechnungen auswählen, SEPA-Datei erzeugen, exportierte bis zur Buchung verfolgen."""
    from apps.einkauf.views import rechnungen
    return rechnungen(request, sicht="zahlung")


# ---- Zahlungsabgleich (Bankauszug) -------------------------------------------------------------

@mit_mandant
def bank(request):
    from .models import Bankumsatz
    if request.method == "POST":
        datei = request.FILES.get("datei")
        if not datei:
            messages.error(request, "Bitte eine CSV- oder CAMT-XML-Datei wählen.")
        else:
            try:
                erg = bankmodul.importiere(request.mandant, datei.name, datei.read(), request.user)
                messages.success(request, f"{erg['gelesen']} Umsätze gelesen: {erg['neu']} neu, {erg['doppelt']} schon vorhanden, "
                                          f"{erg['automatisch']} automatisch zugeordnet.")
            except bankmodul.ImportFehler as fehler:
                messages.error(request, f"Import fehlgeschlagen: {fehler}")
        return redirect("finanzen:bank")
    status = request.GET.get("status", "offen")
    umsaetze = Bankumsatz.objects.filter(mandant=request.mandant)
    if status in ("offen", "zugeordnet", "ignoriert"):
        umsaetze = umsaetze.filter(status=status)
    seite, parameter = blaettern(request, umsaetze.select_related("vorschlag", "vorschlag__kunde", "zahlung").prefetch_related("zahlungen__beleg"))
    return render(request, "finanzen/bank.html", {
        "umsaetze": seite, "seite": seite, "parameter": parameter, "status": status})


@mit_mandant
@require_POST
def bank_zuordnen(request, umsatz_id):
    from .models import Bankumsatz
    umsatz = get_object_or_404(Bankumsatz, pk=umsatz_id, mandant=request.mandant)
    if request.POST.get("aktion") in ("ignorieren", "oeffnen"):
        # Unter Zeilensperre und nur aus dem passenden Status: ein inzwischen gebuchter Umsatz (anderer Tab, „Nachbuchen“)
        # darf nicht über „Ignorieren → Wieder öffnen“ erneut offen werden – sonst würde derselbe Eingang doppelt gebucht.
        with transaction.atomic():
            umsatz = Bankumsatz.objects.select_for_update().get(pk=umsatz.pk)
            gebucht = umsatz.status == Bankumsatz.ZUGEORDNET or umsatz.zahlungen.filter(storno_zu__isnull=True, storniert_durch__isnull=True).exists()
            if gebucht:
                messages.error(request, "Dieser Umsatz ist bereits gebucht und bleibt zugeordnet. "
                                        "Zum Korrigieren die Zahlung an der Rechnung stornieren.")
            elif request.POST["aktion"] == "ignorieren" and umsatz.status == Bankumsatz.OFFEN:
                umsatz.status = Bankumsatz.IGNORIERT
                umsatz.save(update_fields=["status"])
            elif request.POST["aktion"] == "oeffnen" and umsatz.status == Bankumsatz.IGNORIERT:
                umsatz.status = Bankumsatz.OFFEN
                umsatz.save(update_fields=["status"])
        return redirect("finanzen:bank")
    eingabe = request.POST.get("rechnung", "").strip().upper()
    nummern = [n for n in eingabe.replace(";", ",").replace(" ", ",").split(",") if n]  # mehrere Nummern: Betrag wird verteilt
    belege, fehlend = [], []
    for nummer in nummern:
        beleg = Beleg.objects.filter(mandant=request.mandant, art__in=(Belegart.RECHNUNG, Belegart.PROFORMA), nummer=nummer,
                                     abgeschlossen=True).first()
        (belege if beleg else fehlend).append(beleg or nummer)
    if not nummern and umsatz.vorschlag:
        belege = [umsatz.vorschlag]
    if fehlend or not belege:
        messages.error(request, f"Rechnung oder Proforma '{', '.join(fehlend) or eingabe}' nicht gefunden.")
    else:
        try:
            teile = bankmodul.zuordnen(umsatz, belege, request.user)
            messages.success(request, "Gebucht: " + ", ".join(f"{t:.2f} € auf {b.nummer}".replace(".", ",") for b, t in teile) + ".")
        except ValueError as fehler:
            messages.error(request, str(fehler))
    return redirect("finanzen:bank")


@mit_mandant
@require_POST
def bank_nachbuchen(request):
    """Offene Eingänge mit Rechnungsnummer erneut buchen (Teilzahlungen und Aufteilung, auch aus älteren Importen)."""
    try:
        anzahl = bankmodul.vorschlaege_nachbuchen(request.mandant, request.user)
        messages.success(request, f"{anzahl} Eingang/Eingänge nachgebucht." if anzahl else "Nichts nachzubuchen: offene Eingänge ohne passende Rechnungsnummer oder mit Überzahlung bleiben zur Prüfung.")
    except ValueError as fehler:
        messages.error(request, str(fehler))
    return redirect("finanzen:bank")


# ---- Kassenbuch ------------------------------------------------------------------------------

@mit_mandant
def kassenbuch(request):
    from .models import Kassenbuch
    tokens = request.session.get("kassen_tokens", [])
    if request.method == "POST":
        try:
            token = request.POST.get("token")
            if token is not None:  # Formular-Token nur einmal einlösbar: Doppelklick bucht nicht doppelt
                if token not in tokens:
                    raise ValueError("Dieser Eintrag wurde bereits gebucht oder die Seite ist veraltet. Bitte das Kassenbuch prüfen.")
                tokens.remove(token)
                request.session["kassen_tokens"] = tokens
            summe = betrag(request.POST.get("betrag"))
            if summe <= 0:
                raise ValueError("Betrag muss positiv sein.")
            if request.POST.get("art") == "A" and summe > services.kassenstand(request.mandant):
                raise ValueError("Die Ausgabe übersteigt den Kassenstand – die Kasse darf nicht ins Minus laufen.")
            services.kasse_buchen(request.mandant, request.POST.get("art"), summe, request.POST.get("text", "").strip() or "Kassenbuchung",
                                  datum(request.POST.get("datum")), None, request.user)
            messages.success(request, "Gebucht.")
        except ValueError as fehler:
            messages.error(request, str(fehler))
        return redirect("finanzen:kassenbuch")
    token = uuid.uuid4().hex
    request.session["kassen_tokens"] = (tokens + [token])[-20:]
    seite, parameter = blaettern(request, Kassenbuch.objects.filter(mandant=request.mandant))
    return render(request, "finanzen/kassenbuch.html", {
        "token": token, "eintraege": seite, "seite": seite, "parameter": parameter, "stand": services.kassenstand(request.mandant),
        "heute": date.today().isoformat()})


# ---- DATEV -------------------------------------------------------------------------------------

@mit_mandant
def datev_export(request):
    heute = date.today()
    if request.method == "POST":
        try:
            von, bis = datum(request.POST.get("von")), datum(request.POST.get("bis"))
        except ValueError as fehler:
            messages.error(request, str(fehler))
            return redirect("finanzen:datev")
        antwort = HttpResponse(datevmodul.export(request.mandant, von, bis), content_type="text/csv; charset=cp1252")
        antwort["Content-Disposition"] = f'attachment; filename="EXTF_Buchungsstapel_{request.mandant.kurzname}_{von:%Y%m%d}_{bis:%Y%m%d}.csv"'
        return antwort
    return render(request, "finanzen/datev.html", {"von": heute.replace(day=1).isoformat(), "bis": heute.isoformat()})


@mit_mandant
def lastschrift(request):
    """Offene Rechnungen zum Einzug per SEPA-Basis-Lastschrift auswählen."""
    gezogen = set(Lastschrift.objects.filter(mandant=request.mandant, aufgehoben=False).values_list("beleg_id", flat=True))
    zeilen = []
    for z in services.offene_rechnungen(request.mandant):
        if z["offen"] <= 0:  # Überzahlung lässt sich nicht einziehen
            continue
        z["fehlt"] = lastschriftmodul.mandat_fehlt(z["beleg"].kunde)
        z["gezogen"] = z["beleg"].pk in gezogen
        zeilen.append(z)
    morgen = date.today() + timedelta(days=1)
    return render(request, "finanzen/lastschrift.html", {
        "zeilen": zeilen, "einzug": morgen.isoformat(),
        "historie": Lastschrift.objects.filter(mandant=request.mandant).select_related("beleg__kunde", "zahlung")[:50],
        "glaeubiger_fehlt": not request.mandant.glaeubiger_id or not request.mandant.iban})


@mit_mandant
@require_POST
def lastschrift_export(request):
    from apps.einkauf.sepa import SepaFehler
    belege = Beleg.objects.filter(pk__in=request.POST.getlist("auswahl"), mandant=request.mandant,
                                  art=Belegart.RECHNUNG, abgeschlossen=True).select_related("kunde")
    try:
        einzug = datum(request.POST.get("einzug"))
        bereits = set(Lastschrift.objects.filter(beleg__in=belege, aufgehoben=False).values_list("beleg__nummer", flat=True))
        if bereits:
            raise ValueError("Schon eingezogen: " + ", ".join(sorted(bereits)))
        zeilen = [(b, services.offen(b), lastschriftmodul.sequenz(b.kunde, request.mandant)) for b in belege]
        daten, msg_id = lastschriftmodul.erzeuge(request.mandant, zeilen, einzug)
    except (SepaFehler, ValueError) as fehler:
        messages.error(request, str(fehler))
        return redirect("finanzen:lastschrift")
    for b, betrag, seq in zeilen:
        Lastschrift.objects.create(mandant=request.mandant, beleg=b, betrag=betrag, einzugsdatum=einzug,
                                   mandatsreferenz=b.kunde.sepa_mandatsreferenz, sequenz=seq, datei=msg_id,
                                   benutzer=request.user)
    antwort = HttpResponse(daten, content_type="application/xml")
    antwort["Content-Disposition"] = f'attachment; filename="Lastschrift_{request.mandant.kurzname}_{einzug:%Y%m%d}.xml"'
    return antwort


@mit_mandant
@require_POST
@nur_admin
def lastschrift_aufheben(request, ls_id):
    ls = get_object_or_404(Lastschrift, pk=ls_id, mandant=request.mandant)
    try:
        gebucht = ls.zahlung_id is not None
        services.lastschrift_aufheben(ls, request.POST.get("grund"), request.user)
        messages.success(request, f"Lastschrift {ls.beleg.nummer} aufgehoben" + (", die gebuchte Zahlung ist storniert" if gebucht else "")
                         + " – die Rechnung kann erneut eingezogen werden.")
    except ValueError as fehler:
        messages.error(request, str(fehler))
    return redirect("finanzen:lastschrift")
