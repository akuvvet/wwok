import os
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from urllib.parse import urlencode

from django.contrib import messages
from django.db import transaction
from django.db.models import Exists, OuterRef, Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.stammdaten.models import Artikel, Lieferant, Preislistenartikel, kennungen_filter, lieferanten_auswahl
from apps.kern.rechte import admin_bei_aenderung, nur_admin
from apps.kern.seiten import blaettern
from apps.verkauf.mail import MailFehler, ausfuellen, senden_an
from apps.verkauf.models import Beleg, Mailart
from apps.verkauf.views import mit_mandant
from apps.werkstatt.models import Reparatur

from . import erechnung, sepa, services, vergleich
from .models import (RUECKSENDUNG_OFFEN, BestellPosition, BestellStatus, Bestellung, Bestpreisplan, Lieferantengutschrift,
                     LieferantengutschriftVerrechnung, LieferantengutschriftZahlung, Lieferantenrechnung, Rma, RmaStatus,
                     RechnungStatus, Ruecksendung, RuecksendungArt, Wareneingang, WareneingangPosition)
from .pdf import bestellung_pdf


def zahl(text, standard=None):
    text = (text or "").strip()
    if "," in text:
        text = text.replace(".", "").replace(",", ".")
    if not text:
        return standard
    try:
        wert = Decimal(text)
    except InvalidOperation:
        raise ValueError(f"Ungültige Zahl: {text}")
    if not wert.is_finite():
        raise ValueError(f"Ungültige Zahl: {text}")
    return wert


def ust_normal(text):
    """USt-ID zum Vergleichen: ohne Leerzeichen und Punkte, groß."""
    return "".join(c for c in (text or "") if c.isalnum()).upper()


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
def lieferanten(request):
    """Wie „Kunde“ im Verkauf: Suchfeld (ein Treffer öffnet den Lieferanten), ohne Suche alle offenen Einkaufsbelege."""
    from . import lieferantenkonto
    q = request.GET.get("q", "").strip()
    seite = parameter = None
    offene = None
    if q:
        treffer = lieferantenkonto.lieferanten_suchen(q)
        if q != "*" and treffer.count() == 1:
            return redirect("einkauf:lieferant", lieferant_id=treffer.first().pk)
        seite, parameter = blaettern(request, treffer)
    else:
        services.leere_wareneingaenge_entfernen(request.mandant, benutzer=request.user)  # ohne Position wird nicht aufbewahrt
        offene = lieferantenkonto.belege(request.mandant)
    return render(request, "einkauf/lieferanten.html", {
        "lieferanten": seite, "seite": seite, "parameter": parameter, "q": q, "offene": offene, "bereich": "einkauf"})


@mit_mandant
def lieferant_detail(request, lieferant_id):
    """Lieferantenkonto wie die Kundenansicht: Kopf mit Stammdaten, darunter die Einkaufsbelege (Standard: nur offene)."""
    from datetime import date

    from apps.stammdaten.models import LieferantMandant

    from . import lieferantenkonto
    lieferant = get_object_or_404(Lieferant, pk=lieferant_id)
    services.leere_bestellungen_entfernen(request.mandant, lieferant)  # Bestellungen ohne Position werden nicht gespeichert
    services.leere_wareneingaenge_entfernen(request.mandant, lieferant, benutzer=request.user)  # Wareneingänge ebenso
    services.vorlaeufige_bestellungen_verwerfen(request.mandant, lieferant)  # nicht gespeichert und ohne Lebenszeichen: verworfen
    art =request.GET.get("art") or "offen"  # beim Öffnen: nur offene Belege; „alle“ schickt art=alle (der Live-Filter lässt leere Werte weg)
    nur_offen = art == "offen"
    belegart = art if art in dict(lieferantenkonto.ARTEN) else ""
    ab = request.GET.get("ab", "")
    try:
        ab_datum = date.fromisoformat(ab) if ab else None
    except ValueError:
        ab, ab_datum = "", None
    zeilen = lieferantenkonto.belege(request.mandant, lieferant, nur_offen=nur_offen, art=belegart, ab=ab_datum)
    seite, parameter = blaettern(request, zeilen, 25)
    offen_rechnungen = sum((z["betrag"] for z in lieferantenkonto.belege(request.mandant, lieferant, art="LR")), Decimal("0"))
    offen_gutschriften = sum((z["betrag"] for z in lieferantenkonto.belege(request.mandant, lieferant, art="LG")), Decimal("0"))
    md = LieferantMandant.objects.filter(lieferant=lieferant, mandant=request.mandant).first()
    return render(request, "einkauf/lieferant.html", {
        "lieferant": lieferant, "seite": seite, "parameter": parameter, "filter": art, "ab": ab,
        "arten": lieferantenkonto.ARTEN, "offen_rechnungen": offen_rechnungen, "offen_gutschriften": offen_gutschriften,
        "unsere_kundennummer": md.unsere_kundennummer if md else "",
        # Ansprechpartner von Lieferanten gelten je Mandant (ohne Mandant: für alle); Hauptkontakt zuerst
        "ansprechpartner": list(services.lieferant_ansprechpartner(request.mandant, lieferant)),
        "offene_bestellungen": Bestellung.objects.filter(mandant=request.mandant, lieferant=lieferant,
                                                         status__in=[BestellStatus.BESTELLT, BestellStatus.TEIL]),
        # „+ WE-Rücksendung“ steht wie „+ Bestellung“ beim Lieferanten, nicht mehr in der Seitenleiste (Wunsch des Nutzers 19.11.2026)
        "gebuchte_wareneingaenge": services.ruecksendbare_wareneingaenge(request.mandant, lieferant)})


@mit_mandant
def lieferant_kontoauszug(request, lieferant_id):
    """Kontoauszug des Lieferanten wie beim Kunden: Rechnungen, Zahlungen, Gutschriften, Verrechnungen, laufender Saldo."""
    lieferant = get_object_or_404(Lieferant, pk=lieferant_id)
    zeilen, saldo, von, bis, text, saldotext, pdf = lieferant_kontoauszug_daten(request, lieferant)
    if request.GET.get("format") == "pdf":
        antwort = HttpResponse(pdf(), content_type="application/pdf")
        antwort["Content-Disposition"] = f'inline; filename="Kontoauszug-{lieferant.nummer}.pdf"'
        return antwort
    from apps.finanzen.views import zeitraum_query
    return render(request, "einkauf/kontoauszug.html", {"lieferant": lieferant, "zeilen": zeilen, "saldo": saldo, "bereich": "einkauf",
                                                        "von": von, "bis": bis, "zeitraum": text, "query": zeitraum_query(von, bis)})


def lieferant_kontoauszug_daten(request, lieferant):
    """Kontoauszug des Lieferanten im gewählten Zeitraum: (zeilen, saldo, von, bis, zeitraum_text, saldo_text, PDF)."""
    from apps.finanzen.pdf import kontoauszug_pdf
    from apps.finanzen.services import zeitraum, zeitraum_aus

    from . import lieferantenkonto
    von, bis, text = zeitraum_aus(request.GET)
    alle, _ = lieferantenkonto.kontoauszug(request.mandant, lieferant)
    zeilen, saldo = zeitraum(alle, von, bis)
    saldotext = "Guthaben beim Lieferanten" if saldo < 0 else "Offener Saldo (wir schulden dem Lieferanten)"
    pdf = lambda: kontoauszug_pdf(request.mandant, lieferant, "Lieferanten", zeilen, saldo, saldotext, zeitraum_text=text)  # noqa: E731
    return zeilen, saldo, von, bis, text, saldotext, pdf


def avis_daten(request, lieferant):
    """Auswahl (?r=…, Zahltag ?datum=…) und Aufstellung für das Zahlungsavis."""
    kandidaten = list(services.avis_kandidaten(request.mandant, lieferant))
    gewaehlt = {int(x) for x in request.GET.getlist("r") if x.isdigit()} if "r" in request.GET else services.avis_vorauswahl(kandidaten)
    try:
        datum = date.fromisoformat(request.GET.get("datum", ""))
    except ValueError:
        datum = timezone.localdate()
    auswahl = [r for r in kandidaten if r.pk in gewaehlt]
    return kandidaten, auswahl, datum, services.zahlungsavis(auswahl, datum)


def avis_query(auswahl, datum) -> str:
    return urlencode([("r", r.pk) for r in auswahl] + [("datum", datum.isoformat())])


@mit_mandant
def zahlungsavis(request, lieferant_id):
    """Zahlungsavis an den Lieferanten: Rechnungen wählen, als PDF drucken oder per E-Mail senden (Wunsch des Nutzers 20.11.2026)."""
    from apps.finanzen.pdf import zahlungsavis_pdf
    lieferant = get_object_or_404(Lieferant, pk=lieferant_id)
    kandidaten, auswahl, datum, avis = avis_daten(request, lieferant)
    aktion = request.GET.get("aktion", "")
    if aktion in ("pdf", "mail") and not auswahl:
        messages.error(request, "Bitte mindestens eine Rechnung auswählen.")
    elif aktion == "pdf":
        antwort = HttpResponse(zahlungsavis_pdf(request.mandant, lieferant, avis), content_type="application/pdf")
        antwort["Content-Disposition"] = f'inline; filename="Zahlungsavis-{lieferant.nummer}-{datum:%Y%m%d}.pdf"'
        return antwort
    elif aktion == "mail":
        return redirect(reverse("einkauf:zahlungsavis_mail", args=[lieferant.pk]) + "?" + avis_query(auswahl, datum))
    gewaehlt = {r.pk for r in auswahl}
    zeilen = {z["rechnung"].pk: z for z in services.zahlungsavis(kandidaten, datum)["zeilen"]}
    return render(request, "einkauf/zahlungsavis.html", {
        "lieferant": lieferant, "zeilen": [dict(zeilen[r.pk], gewaehlt=r.pk in gewaehlt) for r in kandidaten],
        "datum": datum, "summe": avis["summe"], "tage": services.AVIS_TAGE, "bereich": "einkauf"})


@mit_mandant
def zahlungsavis_mail(request, lieferant_id):
    from apps.finanzen.pdf import zahlungsavis_pdf
    from apps.verkauf.mail import anrede_ap, empfaenger_vorschlag
    lieferant = get_object_or_404(Lieferant, pk=lieferant_id)
    _, auswahl, datum, avis = avis_daten(request, lieferant)
    query = avis_query(auswahl, datum)
    zurueck = reverse("einkauf:zahlungsavis", args=[lieferant.pk]) + "?" + query
    if not auswahl:
        messages.error(request, "Bitte mindestens eine Rechnung auswählen.")
        return redirect(zurueck)
    an, ap = empfaenger_vorschlag(lieferant, services.lieferant_ansprechpartner(request.mandant, lieferant))
    betreff, mailtext = ausfuellen(request.mandant, Mailart.ZAHLUNGSAVIS, {
        "anrede": anrede_ap(ap), "lieferant": lieferant.name1, "firma": request.mandant.firmenname, "datum": f"{datum:%d.%m.%Y}",
        "betrag": euro_text(avis["summe"]["zahlung"]), "anzahl": len(auswahl),
        "rechnungen": ", ".join(r.rechnungsnummer for r in auswahl)})
    dateiname = f"Zahlungsavis-{lieferant.nummer}-{datum:%Y%m%d}.pdf"
    if request.method == "POST":
        an, betreff, mailtext = request.POST.get("an", ""), request.POST.get("betreff", ""), request.POST.get("text", "")
        try:
            senden_an(request.mandant, an, betreff, mailtext, zahlungsavis_pdf(request.mandant, lieferant, avis), dateiname,
                      bezug=f"Zahlungsavis {lieferant.nummer}", benutzer=request.user)
            messages.success(request, f"Zahlungsavis an {an} gesendet.")
            sepa_avis_gesendet(request, lieferant, auswahl)
            return redirect(zurueck)
        except MailFehler as fehler:
            messages.error(request, str(fehler))
    return render(request, "finanzen/partner_mail.html", {
        "titel": f"Zahlungsavis an {lieferant.name1} senden", "an": an, "betreff": betreff, "text": mailtext, "zurueck": zurueck,
        "anhang": dateiname, "anhang_url": zurueck + "&aktion=pdf", "bereich": "einkauf"})


def euro_text(betrag) -> str:
    return f"{betrag:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


@mit_mandant
def lieferant_kontoauszug_mail(request, lieferant_id):
    """Kontoauszug als PDF per E-Mail an den Lieferanten (Wunsch des Nutzers 20.11.2026)."""
    from apps.finanzen.views import zeitraum_query
    from apps.verkauf.mail import anrede_ap, empfaenger_vorschlag
    lieferant = get_object_or_404(Lieferant, pk=lieferant_id)
    zeilen, saldo, von, bis, text, saldotext, pdf = lieferant_kontoauszug_daten(request, lieferant)
    an, ap = empfaenger_vorschlag(lieferant, services.lieferant_ansprechpartner(request.mandant, lieferant))
    betreff, mailtext = ausfuellen(request.mandant, Mailart.KONTOAUSZUG, {
        "anrede": anrede_ap(ap), "partner": lieferant.name1, "nummer": lieferant.nummer, "firma": request.mandant.firmenname,
        "zeitraum": text, "saldo": euro_text(saldo), "saldotext": saldotext})
    query = zeitraum_query(von, bis)
    zurueck = reverse("einkauf:lieferant_kontoauszug", args=[lieferant.pk]) + (f"?{query}" if query else "")
    if request.method == "POST":
        an, betreff, mailtext = request.POST.get("an", ""), request.POST.get("betreff", ""), request.POST.get("text", "")
        try:
            senden_an(request.mandant, an, betreff, mailtext, pdf(), f"Kontoauszug-{lieferant.nummer}.pdf",
                      bezug=f"Kontoauszug {lieferant.nummer}", benutzer=request.user)
            messages.success(request, f"Kontoauszug an {an} gesendet.")
            return redirect(zurueck)
        except MailFehler as fehler:
            messages.error(request, str(fehler))
    return render(request, "finanzen/partner_mail.html", {
        "titel": f"Kontoauszug an {lieferant.name1} senden", "an": an, "betreff": betreff, "text": mailtext, "zurueck": zurueck,
        "anhang": f"Kontoauszug-{lieferant.nummer}.pdf", "anhang_url": zurueck + ("&" if query else "?") + "format=pdf", "bereich": "einkauf"})


@mit_mandant
def bestellungen(request):
    services.leere_bestellungen_entfernen(request.mandant)  # Bestellungen ohne Position werden nicht gespeichert
    services.vorlaeufige_bestellungen_verwerfen(request.mandant)  # nicht gespeichert und ohne Lebenszeichen: verworfen
    status =request.GET.get("status", "offen")
    liste = Bestellung.objects.filter(mandant=request.mandant).select_related("lieferant", "aus_beleg").prefetch_related("positionen")
    if status == "offen":
        liste = liste.filter(status__in=[BestellStatus.ENTWURF, BestellStatus.BESTELLT, BestellStatus.TEIL])
    elif status == "bestellt":  # wie die Dashboard-Kachel: bestellt + teilweise geliefert
        liste = liste.filter(status__in=[BestellStatus.BESTELLT, BestellStatus.TEIL])
    elif status in BestellStatus.values:
        liste = liste.filter(status=status)
    seite, parameter = blaettern(request, liste.order_by("-datum", "-id"))
    return render(request, "einkauf/bestellungen.html", {
        "bestellungen": seite, "seite": seite, "parameter": parameter, "status": status, "lieferanten": lieferanten_auswahl()})


@mit_mandant
@require_POST
@schutz("einkauf:bestellungen")
def bestellung_neu(request):
    lieferant = get_object_or_404(Lieferant, pk=request.POST.get("lieferant"))
    # „+ Bestellung“: vorläufig, ohne Nummer – die Nummer vergibt erst „Speichern: abschließen & Nummer vergeben“ (Wunsch des Nutzers 19.11.2026)
    b = services.bestellung_anlegen(request.mandant, lieferant, benutzer=request.user, vorlaeufig=True,
                                    ansprechpartner_id=request.POST.get("ansprechpartner"))  # im Lieferantenkopf gewählt
    return redirect("einkauf:bestellung", bestellung_id=b.pk)


def bestellung_holen(request, bestellung_id):
    """Jeder Aufruf einer Bestellung (Seite, Position, Kopf, Suche) zählt bei einer vorläufigen als Aktivität (Lebenszeichen)."""
    b = get_object_or_404(Bestellung.objects.select_related("lieferant", "mandant"), pk=bestellung_id, mandant=request.mandant)
    if b.vorlaeufig:
        b.zuletzt_aktiv = timezone.now()
        Bestellung.objects.filter(pk=b.pk, vorlaeufig=True).update(zuletzt_aktiv=b.zuletzt_aktiv)
    return b


def _erst_abschliessen(request, b, aktion):
    """Mail, PDF, „Als bestellt markieren“ und Wareneingang gibt es erst nach dem Abschluss: bei einer vorläufigen Bestellung die
    Rückfrage „Erst abschließen & Nummer vergeben?“ (Bestätigen schließt ab und führt dann die Aktion aus, siehe bestellung_abschliessen).
    Rückgabe: None (weiter) oder die Rückfrage-Seite."""
    if not b.vorlaeufig:
        return None
    return render(request, "einkauf/erst_abschliessen_frage.html", {"b": b, "aktion": aktion, "aktion_text": ERST_AKTIONEN[aktion]})


ERST_AKTIONEN = {"mail": "per E-Mail bestellen", "pdf": "das PDF ansehen", "bestellen": "als bestellt markieren", "wareneingang": "einen Wareneingang erfassen"}


@mit_mandant
def bestellung_detail(request, bestellung_id):
    b = bestellung_holen(request, bestellung_id)
    positionen = list(b.positionen.select_related("artikel", "preisliste_artikel"))
    entwurf = b.status == BestellStatus.ENTWURF
    aenderbar = b.status not in (BestellStatus.STORNIERT, BestellStatus.GELIEFERT)
    offen_im_we = set(WareneingangPosition.objects.filter(bestellposition__bestellung=b, wareneingang__gebucht=False)
                      .values_list("bestellposition_id", flat=True))
    for p in positionen:
        p.bestellung = b
        p.im_wareneingang = p.pk in offen_im_we
        p.aenderbar = aenderbar and not p.geliefert and not p.im_wareneingang
        p.alternative = vergleich.alternative(p) if entwurf and not p.geliefert else None
    netto, konditionen = b.summe_netto, []
    if entwurf and positionen:
        l = b.lieferant
        if l.mindestbestellwert and netto < l.mindestbestellwert:
            konditionen.append(("Mindestbestellwert", f"noch {vergleich.eur(l.mindestbestellwert - netto)} €"))
        if l.versandkostenfrei_ab and netto < l.versandkostenfrei_ab:
            konditionen.append(("Bis versandfrei", f"noch {vergleich.eur(l.versandkostenfrei_ab - netto)} €"))
        if vergleich.versand(l, netto):
            konditionen.append(("Versand", f"{vergleich.eur(vergleich.versand(l, netto))} €"))
    bedarf_nur = request.GET.get("bedarf") == "lieferant"  # Standard: der gesamte Bedarf
    offen_fuer_positionen = b.status in (BestellStatus.ENTWURF, BestellStatus.BESTELLT, BestellStatus.TEIL)
    return render(request, "einkauf/bestellung.html", {
        "b": b, "positionen": positionen, "netto": netto, "gesamt_netto": netto + b.nebenkosten, "konditionen": konditionen, "mit_lieferung": any(p.geliefert for p in positionen),
        "wareneingaenge": b.alle_wareneingaenge().prefetch_related("positionen", "ruecksendungen"), "aus_beleg": b.aus_beleg,
        "bedarf": services.bedarf_fuer_lieferant(b, nur_lieferant=bedarf_nur) if offen_fuer_positionen else [], "bedarf_nur": bedarf_nur,
        "offen_fuer_positionen": offen_fuer_positionen,
        "ansprechpartner_liste": list(services.lieferant_ansprechpartner(request.mandant, b.lieferant))})


@mit_mandant
@require_POST
@schutz("einkauf:bestellung", "bestellung_id")
def bestellung_bedarf(request, bestellung_id):
    """Tab „Bedarf“: gewählte Artikel mit Menge als Positionen übernehmen."""
    b = bestellung_holen(request, bestellung_id)
    auswahl = []
    for artikel_id in request.POST.getlist("auswahl"):
        if not artikel_id.isdigit():
            raise ValueError("Ungültige Artikelauswahl.")
        artikel = get_object_or_404(Artikel, pk=artikel_id)
        auswahl.append((artikel, zahl(request.POST.get(f"menge_{artikel_id}"), None)))
    anzahl = services.bedarf_uebernehmen(b, auswahl)
    messages.success(request, f"{anzahl} Artikel aus dem Bedarf übernommen.")
    return redirect(reverse("einkauf:bestellung", args=[b.pk]) + "#positionen")


@mit_mandant
@require_POST
@schutz("einkauf:bestellung", "bestellung_id")
def bestellung_kopf(request, bestellung_id):
    """Tabs „Versand & Konditionen“ und „Texte & Notizen“ speichern."""
    b = bestellung_holen(request, bestellung_id)
    daten = {}
    for feld in ("liefertermin", "bezugnahme", "versandart", "bemerkung", "notiz", "ansprechpartner"):
        if feld in request.POST:
            daten[feld] = request.POST[feld]
    for feld in ("versandkosten", "verpackung", "versicherung"):
        if feld in request.POST:
            daten[feld] = zahl(request.POST[feld], Decimal("0"))
    services.bestellung_kopf_speichern(b, daten)
    messages.success(request, "Gespeichert.")
    tab = request.POST.get("tab") if request.POST.get("tab") in ("versand", "texte") else "versand"
    return redirect(reverse("einkauf:bestellung", args=[b.pk]) + "#" + tab)


def vergleich_menge(text):
    """Menge für den Vergleich: gültig für das Mengenfeld der Bestellung, sonst ValueError."""
    menge = zahl(text, Decimal("1"))
    if not Decimal("0.001") <= menge <= Decimal("9999999"):
        raise ValueError("Die Menge muss zwischen 0,001 und 9.999.999 liegen.")
    return menge.quantize(Decimal("0.001"))


def vergleich_entwurf(request, daten):
    """Die Bestellung, aus der heraus verglichen wird: ihr Entwurf hat beim eigenen Lieferanten Vorrang."""
    nummer = daten.get("bestellung", "")
    return Bestellung.objects.filter(pk=nummer, mandant=request.mandant).first() if nummer.isdigit() else None


def vergleich_produkt(request, daten):
    """(Artikel, Preislisteneintrag) aus den Angaben 'artikel' (Nummer) und 'pl' (Eintrag)."""
    pl = daten.get("pl", "")
    if pl.isdigit():
        eintrag = Preislistenartikel.objects.select_related("artikel").filter(pk=pl).first()
        if eintrag:
            return (eintrag.artikel, None) if eintrag.artikel_id else (None, eintrag)
    nummer = daten.get("artikel", "").strip()
    return (Artikel.objects.filter(nummer__iexact=nummer).first() if nummer else None), None


@mit_mandant
def preisvergleich(request):
    """Produkt suchen, Lieferanten vergleichen und selbst entscheiden. Mit ?teil=1 nur die Tabelle (für die Bestellmaske)."""
    try:
        menge = vergleich_menge(request.GET.get("menge"))
    except ValueError:
        menge = Decimal("1")
    q = request.GET.get("q", "").strip()
    position_id = request.GET.get("position", "")
    position = BestellPosition.objects.filter(pk=position_id if position_id.isdigit() else None, bestellung__mandant=request.mandant,
                                              bestellung__status=BestellStatus.ENTWURF, geliefert=0) \
        .select_related("artikel", "preisliste_artikel__artikel", "bestellung").first()
    if position:
        menge, artikel = position.menge, position.artikel or (position.preisliste_artikel.artikel if position.preisliste_artikel_id else None)
        eintrag = None if artikel else position.preisliste_artikel
    elif q:  # neue Suche ersetzt das bisher gewählte Produkt
        artikel, eintrag = None, None
    else:
        artikel, eintrag = vergleich_produkt(request, request.GET)
    produkt = artikel or eintrag
    aktuelle = vergleich_entwurf(request, request.GET)
    kontext = {"artikel": artikel, "eintrag": eintrag, "produkt": produkt, "menge": menge, "position": position, "aktuelle": aktuelle,
               "zeilen": vergleich.vergleich(request.mandant, menge, artikel, eintrag, position, aktuelle) if produkt else []}
    if request.GET.get("teil"):
        return render(request, "einkauf/vergleich_tabelle.html", kontext)
    treffer, eintraege = [], []
    if len(q) >= 2 and not produkt:
        treffer = Artikel.objects.filter(nicht_aktiv=False, typ="A")
        eintraege = Preislistenartikel.objects.filter(artikel__isnull=True).select_related("lieferant")
        for wort in q.split():
            treffer = treffer.filter(Q(nummer__icontains=wort) | Q(bezeichnung__icontains=wort) | Q(herstellernummer__icontains=wort)
                                     | Q(hersteller__name__icontains=wort) | kennungen_filter(wort))
            eintraege = eintraege.filter(Q(bezeichnung__icontains=wort) | Q(herstellernummer__icontains=wort) | Q(bestellnummer__icontains=wort)
                                         | Q(hersteller__icontains=wort) | Q(ean__icontains=wort))
        treffer, eintraege = treffer.order_by("nummer")[:20], eintraege.order_by("bezeichnung")[:20]
    return render(request, "einkauf/vergleich.html", {**kontext, "q": q, "treffer": treffer, "eintraege": eintraege})


@mit_mandant
@require_POST
@schutz("einkauf:preisvergleich")
def preisvergleich_bestellen(request):
    """Gewähltes Angebot in den Entwurf des Lieferanten legen; mit 'position' wird eine Entwurfsposition dorthin verschoben."""
    position = None
    if request.POST.get("position", "").isdigit():
        position = get_object_or_404(BestellPosition.objects.select_related("artikel", "preisliste_artikel__artikel"),
                                     pk=request.POST["position"], bestellung__mandant=request.mandant)
        artikel = position.artikel or (position.preisliste_artikel.artikel if position.preisliste_artikel_id else None)
        eintrag, menge = None if artikel else position.preisliste_artikel, position.menge
    else:
        artikel, eintrag = vergleich_produkt(request, request.POST)
        menge = vergleich_menge(request.POST.get("menge"))
    if not (artikel or eintrag):
        raise ValueError("Bitte zuerst ein Produkt wählen.")
    lieferant = request.POST.get("lieferant", "")
    ek = zahl(request.POST.get("ek"))  # in der Bestellmaske eingetragener Preis gilt für „Übernehmen“
    if ek is not None and (ek < 0 or ek >= Decimal("100000000")):
        raise ValueError("Der Preis darf nicht negativ sein.")
    ziel = vergleich.bestellen(request.mandant, int(lieferant) if lieferant.isdigit() else 0, menge, artikel, eintrag, position,
                               vergleich_entwurf(request, request.POST), ek)
    meldung = f"{'Verschoben nach' if position else 'Aufgenommen in'} Bestellung {ziel.nummer_text} ({ziel.lieferant.kurz})."
    if ziel.geloeschte_quelle:
        meldung += f" Die Bestellung {ziel.geloeschte_quelle} hatte keine Position mehr und wurde gelöscht."
    messages.success(request, meldung)
    return redirect("einkauf:bestellung", bestellung_id=ziel.pk)


@mit_mandant
@require_POST
@schutz("einkauf:bestellung", "bestellung_id")
def bestellung_position(request, bestellung_id):
    b = bestellung_holen(request, bestellung_id)
    menge, ek = zahl(request.POST.get("menge"), Decimal("1")), zahl(request.POST.get("ek"))
    if menge <= 0:
        raise ValueError("Die Menge muss größer als 0 sein.")
    text = request.POST.get("bezeichnung", "").strip()
    if request.POST.get("pl"):  # in der Suche gewählter Artikel aus der Preisliste des Lieferanten
        eintrag = get_object_or_404(Preislistenartikel.objects.select_related("lieferant", "artikel"), pk=request.POST["pl"])
        neu = services.bestellposition(b, None, menge, ek, preisliste_artikel=eintrag)
        if text:
            services.bezeichnung_setzen(neu, text)
        return redirect("einkauf:bestellung", bestellung_id=b.pk)
    code = request.POST.get("artikel", "").strip()
    artikel = Artikel.objects.filter(nummer__iexact=code).first() or services.artikel_per_code(code, b.lieferant)
    if artikel is None:
        eintrag = services.preislisten_treffer(code, b.lieferant)
        if eintrag is None:
            raise ValueError(f"Artikel '{code}' nicht gefunden.")
        neu = services.bestellposition(b, None, menge, ek, preisliste_artikel=eintrag)  # P-Position, kein eigener Artikel
    else:
        neu = services.bestellposition(b, artikel, menge, ek)
    if text:  # eingegebene Bezeichnung gilt, leer = wie Artikel
        services.bezeichnung_setzen(neu, text)
    return redirect("einkauf:bestellung", bestellung_id=b.pk)


@mit_mandant
def api_bestellartikel(request, bestellung_id):
    """Suche für die Bestellposition, dieselbe Syntax wie in der Belegmaske (apps/stammdaten/suche.py, Wunsch des Nutzers
    19.11.2026): Wörter in beliebiger Reihenfolge, „*“ = alle, „%“ wie LIKE, „>5“ = Lagerbestand über 5 (bei Preislistenartikeln:
    Bestand beim Lieferanten). Eigene Artikel und die Preisliste des Lieferanten; tabelle=1 liefert mehr Zeilen (Auswahlfenster)."""
    from apps.lager.models import reserviert_je_artikel
    from apps.stammdaten.suche import artikel_filtern, mit_bestand, preisliste_filtern
    b = bestellung_holen(request, bestellung_id)
    q = request.GET.get("q", "").strip()
    tabelle = request.GET.get("tabelle") == "1"
    alle = any(z in q for z in "*>%") or tabelle
    if len(q) < 2 and not alle:
        return JsonResponse({"treffer": []})
    grenze = 2000 if tabelle else (60 if alle else 15)
    artikel = artikel_filtern(Artikel.objects.filter(nicht_aktiv=False, typ="A"), q, b.mandant)
    eintraege = preisliste_filtern(Preislistenartikel.objects.filter(lieferant=b.lieferant, artikel__isnull=True), q)
    artikel = list(mit_bestand(artikel, b.mandant).order_by("nummer")[:grenze + 1])
    eintraege = list(eintraege.order_by("bezeichnung")[:grenze + 1])
    mehr = len(artikel) > grenze or len(eintraege) > grenze
    treffer = []
    artikel = artikel[:grenze]
    preise = services.ek_vorschlaege(b.mandant, b.lieferant, artikel)  # wenige Abfragen statt drei je Zeile
    reserviert = reserviert_je_artikel(b.mandant, [a.pk for a in artikel])
    for a in artikel:
        ek, bestellnr = preise[a.pk]
        lager = a.lagerbestand
        frei = max(lager - (reserviert.get(a.pk) or Decimal("0")), Decimal("0"))  # wie lager.models.verfuegbar: nie unter 0
        treffer.append({"nummer": a.nummer, "bezeichnung": a.bezeichnung, "ek": vergleich.eur(ek), "bestellnummer": bestellnr,
                        "pl": None, "lieferant": "", "eigener": True, "bestand": f"{lager.normalize():f}",
                        "verfuegbar": f"{frei.normalize():f}"})
    kurz = b.lieferant.kurz  # alle Treffer gehören zum Lieferanten der Bestellung
    for e in eintraege[:grenze]:
        treffer.append({"nummer": e.nummer, "bezeichnung": e.bezeichnung, "ek": vergleich.eur(e.ek), "bestellnummer": e.bestellnummer,
                        "pl": e.pk, "lieferant": kurz, "lieferant_bestand": e.bestand, "bestand": None, "verfuegbar": None,
                        "eigener": False})
    return JsonResponse({"treffer": treffer, "mehr": mehr})


@mit_mandant
def preislistenartikel(request):
    """Alle Artikel aus den Lieferantenpreislisten durchsuchen (mehrere Wörter, beliebige Reihenfolge)."""
    from django.core.paginator import Paginator
    q = request.GET.get("q", "").strip()
    lieferant = request.GET.get("lieferant", "")
    eintraege = Preislistenartikel.objects.select_related("lieferant", "artikel")
    if lieferant.isdigit():
        eintraege = eintraege.filter(lieferant_id=int(lieferant))
    if request.GET.get("verknuepft") == "1":
        eintraege = eintraege.filter(artikel__isnull=False)
    for wort in q.split():
        eintraege = eintraege.filter(Q(bezeichnung__icontains=wort) | Q(herstellernummer__icontains=wort) | Q(bestellnummer__icontains=wort)
                                     | Q(hersteller__icontains=wort) | Q(ean__icontains=wort))
    seite = Paginator(eintraege.order_by("lieferant_id", "bezeichnung"), 50).get_page(request.GET.get("seite"))
    parameter = request.GET.copy()
    parameter.pop("seite", None)
    return render(request, "einkauf/preislistenartikel.html", {
        "seite": seite, "parameter": parameter.urlencode(), "q": q, "lieferant": lieferant,
        "verknuepft": request.GET.get("verknuepft") == "1", "lieferanten": lieferanten_auswahl(gesperrte=True).filter(
            Exists(Preislistenartikel.objects.filter(lieferant_id=OuterRef("pk"))))})


@mit_mandant
@require_POST
@schutz("einkauf:bestellung", "bestellung_id")
def bestellung_position_aendern(request, bestellung_id, position_id):
    """Bezeichnung, Menge und Preis einer Position nachträglich ändern – bis zur ersten Lieferung dieser Position.
    Die Bezeichnung gilt nur für diese Bestellung; leer = wieder die Bezeichnung des Artikels."""
    b = bestellung_holen(request, bestellung_id)
    with transaction.atomic():  # Sperre: ein gleichzeitig gebuchter Wareneingang darf nicht überschrieben werden
        pos = get_object_or_404(BestellPosition.objects.select_for_update(), pk=position_id, bestellung=b)
        if pos.geliefert or b.status in (BestellStatus.STORNIERT, BestellStatus.GELIEFERT):
            raise ValueError("Diese Position lässt sich nicht mehr ändern.")
        if pos.eingaenge.filter(wareneingang__gebucht=False).exists():
            raise ValueError("Die Position steht in einem offenen Wareneingang – bitte dort ändern oder ihn erst löschen.")
        text = request.POST.get("menge", "").strip()
        menge, ek = vergleich_menge(text) if text else pos.menge, zahl(request.POST.get("ek"), pos.ek)
        if ek < 0 or ek >= Decimal("100000000"):
            raise ValueError("Der Preis darf nicht negativ sein.")
        pos.menge, pos.ek = menge, services.ek_runden(ek)
        pos.save(update_fields=["menge", "ek"])
        if request.POST.get("bezeichnung") is not None:
            services.bezeichnung_setzen(pos, request.POST["bezeichnung"])
    return redirect("einkauf:bestellung", bestellung_id=b.pk)


@mit_mandant
@require_POST
@schutz("einkauf:bestellung", "bestellung_id")
def bestellung_position_loeschen(request, bestellung_id, position_id):
    b = bestellung_holen(request, bestellung_id)
    get_object_or_404(BestellPosition, pk=position_id, bestellung=b)
    services.position_loeschen(b, position_id)
    return redirect("einkauf:bestellung", bestellung_id=b.pk)


@mit_mandant
@require_POST
@schutz("einkauf:bestellung", "bestellung_id")
def bestellung_bestellen(request, bestellung_id):
    b = bestellung_holen(request, bestellung_id)
    frage = _erst_abschliessen(request, b, "bestellen")
    if frage:
        return frage
    services.bestellen(b)
    messages.success(request, "Als bestellt markiert.")
    return redirect("einkauf:bestellung", bestellung_id=b.pk)


@mit_mandant
@require_POST
@schutz("einkauf:bestellung", "bestellung_id")
def bestellung_rest_schliessen(request, bestellung_id):
    b = bestellung_holen(request, bestellung_id)
    weg = services.rest_schliessen(b)
    messages.success(request, "Bestellung abgeschlossen, Rest gestrichen: " + ", ".join(weg))
    return redirect("einkauf:bestellung", bestellung_id=b.pk)


@mit_mandant
@require_POST
@schutz("einkauf:bestellung", "bestellung_id")
def bestellung_abschliessen(request, bestellung_id):
    """„Speichern: abschließen & Nummer vergeben“. Mit danach (nach der Rückfrage „Erst abschließen?“) wird anschließend die gewünschte
    Aktion ausgeführt: mail, pdf, bestellen oder wareneingang."""
    b = bestellung_holen(request, bestellung_id)
    if services.bestellung_abschliessen(b):
        messages.success(request, f"Bestellung {b.nummer} gespeichert – Nummer vergeben.")
    danach = request.POST.get("danach", "")
    if danach == "mail":
        return redirect("einkauf:bestellung_mail", bestellung_id=b.pk)
    if danach == "pdf":
        return redirect("einkauf:bestellung_pdf", bestellung_id=b.pk)
    if danach == "bestellen":
        services.bestellen(b)
        messages.success(request, "Als bestellt markiert.")
    elif danach == "wareneingang":
        we = services.wareneingang_anlegen(request.mandant, b.lieferant, b, request.user)
        services.wareneingang_aus_bestellung(we)
        return redirect("einkauf:wareneingang", we_id=we.pk)
    from apps.verkauf.views import sicheres_ziel
    weiter = sicheres_ziel(request, request.POST.get("weiter", ""))
    if weiter and not b.vorlaeufig:  # abgeschlossen beim Verlassen: weiter zum angeklickten Menüpunkt
        return redirect(weiter)
    return redirect("einkauf:bestellung", bestellung_id=b.pk)


@mit_mandant
@require_POST
def bestellung_verwerfen(request, bestellung_id):
    """Nicht gespeicherte Bestellung verwerfen (samt Positionen). Mit Kopfzeile X-Wwok-Stand (Fenster beim Verlassen, per fetch)
    antwortet die View mit JSON {ok, weiter}, sonst mit Meldung und Weiterleitung zum Lieferanten."""
    als_json = bool(request.headers.get("X-Wwok-Stand"))
    b = Bestellung.objects.filter(pk=bestellung_id, mandant=request.mandant).select_related("lieferant").first()
    if b is None:  # schon verworfen (z. B. von der Bereinigung)
        weiter = reverse("einkauf:bestellungen")
        if als_json:
            return JsonResponse({"ok": True, "weiter": weiter})
        messages.info(request, "Die Bestellung ist schon verworfen.")
        return redirect(weiter)
    weiter = reverse("einkauf:lieferant", args=[b.lieferant_id])
    try:
        verworfen = services.bestellung_verwerfen(b)
    except ValueError as fehler:
        if als_json:
            return JsonResponse({"ok": False, "fehler": str(fehler)}, status=409)
        messages.error(request, str(fehler))
        return redirect("einkauf:bestellung", bestellung_id=b.pk)
    if not verworfen:  # inzwischen gespeichert
        weiter = reverse("einkauf:bestellung", args=[b.pk])
        if als_json:
            return JsonResponse({"ok": False, "weiter": weiter, "fehler": "Die Bestellung ist schon gespeichert – es gibt nichts zu verwerfen."})
        messages.warning(request, "Die Bestellung ist schon gespeichert – es gibt nichts zu verwerfen.")
        return redirect(weiter)
    messages.success(request, "Bestellung verworfen.")
    return JsonResponse({"ok": True, "weiter": weiter}) if als_json else redirect(weiter)


@mit_mandant
@require_POST
def bestellung_aktiv(request, bestellung_id):
    """Puls der geöffneten Bestellseite: hält die vorläufige Bestellung am Leben (zuletzt_aktiv). ok=False: sie wurde inzwischen
    verworfen oder gespeichert; weiter zeigt, wohin die Seite wechseln soll."""
    if Bestellung.objects.filter(pk=bestellung_id, mandant=request.mandant, vorlaeufig=True).update(zuletzt_aktiv=timezone.now()):
        return JsonResponse({"ok": True})
    b = Bestellung.objects.filter(pk=bestellung_id, mandant=request.mandant).first()
    return JsonResponse({"ok": False, "weiter": reverse("einkauf:bestellung", args=[b.pk]) if b else reverse("einkauf:bestellungen")})


@mit_mandant
@require_POST
def bestellung_verlassen(request, bestellung_id):
    """Signal beim Schließen von Tab/Browser (navigator.sendBeacon): die nicht gespeicherte Bestellung gilt in VERLASSEN_SEKUNDEN als
    verlassen und wird beim nächsten Öffnen der Bestellliste oder Lieferantenseite verworfen. Neuladen (F5) setzt die Zeit zurück."""
    from datetime import timedelta

    from apps.verkauf.models import VERLASSEN_SEKUNDEN, VORLAEUFIG_MINUTEN
    zeit = timezone.now() - timedelta(minutes=VORLAEUFIG_MINUTEN) + timedelta(seconds=VERLASSEN_SEKUNDEN)
    Bestellung.objects.filter(pk=bestellung_id, mandant=request.mandant, vorlaeufig=True).update(zuletzt_aktiv=zeit)
    return HttpResponse(status=204)


@mit_mandant
@require_POST
def bestellung_stornieren(request, bestellung_id):
    b = bestellung_holen(request, bestellung_id)
    if b.alle_wareneingaenge().exists():
        if b.alle_wareneingaenge().filter(gebucht=True).exists():
            messages.error(request, "Es gibt schon Wareneingänge – nicht stornierbar.")
        else:
            messages.error(request, "Es gibt noch einen offenen Wareneingang – bitte ihn zuerst im Wareneingang löschen.")
    else:
        b.status = BestellStatus.STORNIERT
        b.save(update_fields=["status"])
    return redirect("einkauf:bestellung", bestellung_id=b.pk)


@mit_mandant
@require_POST
def bestellung_loeschen(request, bestellung_id):
    b = bestellung_holen(request, bestellung_id)
    if b.alle_wareneingaenge().exists():
        if b.alle_wareneingaenge().filter(gebucht=True).exists():
            messages.error(request, "Zur Bestellung gibt es Wareneingänge – sie kann nicht gelöscht werden.")
        else:
            messages.error(request, "Es gibt noch einen offenen Wareneingang – bitte ihn zuerst im Wareneingang löschen.")
        return redirect("einkauf:bestellung", bestellung_id=b.pk)
    if b.vorlaeufig:  # nicht gespeichert: wird verworfen, nicht gelöscht (es gibt keine Nummer)
        return bestellung_verwerfen(request, bestellung_id)
    nummer = b.nummer
    b.delete()
    messages.success(request, f"Bestellung {nummer} gelöscht.")
    return redirect("einkauf:bestellungen")


@mit_mandant
def bestellung_pdf_ansicht(request, bestellung_id):
    b = bestellung_holen(request, bestellung_id)
    frage = _erst_abschliessen(request, b, "pdf")
    if frage:
        return frage
    antwort = HttpResponse(bestellung_pdf(b), content_type="application/pdf")
    antwort["Content-Disposition"] = f'inline; filename="{b.nummer}.pdf"'
    return antwort


@mit_mandant
def bestellung_mail(request, bestellung_id):
    b = bestellung_holen(request, bestellung_id)
    frage = _erst_abschliessen(request, b, "mail")
    if frage:
        return frage
    ap = (b.ansprechpartner if b.ansprechpartner_id and b.ansprechpartner.email else None) \
        or services.lieferant_ansprechpartner(request.mandant, b.lieferant).exclude(email="").first()
    an = (ap.email if ap else b.lieferant.email) or ""
    betreff, text = ausfuellen(request.mandant, Mailart.LIEFERANT, {
        "anrede": "Sehr geehrte Damen und Herren", "nummer": b.nummer, "firma": request.mandant.firmenname,
        "lieferant": b.lieferant.name1})
    if request.method == "POST":
        an, betreff, text = request.POST.get("an", ""), request.POST.get("betreff", ""), request.POST.get("text", "")
        try:
            services.bestellen_pruefen(b)  # erst prüfen, dann senden: keine Mail für eine leere oder stornierte Bestellung
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
        beleg.vorlaeufig_sperre("Eine Lieferantenbestellung anlegen geht")
    except ValueError as fehler:
        messages.error(request, str(fehler))
        return redirect("verkauf:beleg", beleg_id=beleg.pk)
    modus = request.POST.get("modus") or None
    if modus is None and services.rueckfrage_noetig(beleg):  # Kundenbestellung schon (teilweise) bestellt: erst fragen
        return render(request, "einkauf/aus_beleg_rueckfrage.html", {"beleg": beleg, **services.bestellstand(beleg)})
    grund = request.POST.get("grund", "")
    try:
        beleg.stand_pruefen()
        wahl = None
        if request.POST.get("wahl"):  # Antwort aus der Lieferantenwahl: je Position der gewählte Lieferant
            wahl = {}
            for name, wert in request.POST.items():
                if name.startswith("lf_") and name[3:].isdigit() and wert.isdigit():
                    wahl[int(name[3:])] = int(wert)
        else:  # mehrere Positionen und mehr als ein möglicher Lieferant: erst fragen, mit Preisvergleich
            vergleich = services.lieferantenvergleich(beleg, modus=modus, grund=grund)
            if vergleich["wahl_noetig"]:
                return render(request, "einkauf/aus_beleg_lieferantenwahl.html", {
                    "beleg": beleg, "modus": modus or "", "grund": grund, **vergleich})
        liste = services.bestellungen_aus_beleg(beleg, modus=modus, grund=grund, benutzer=request.user, wahl=wahl, vorlaeufig_bei_einer=True)
    except ValueError as fehler:
        messages.error(request, str(fehler))
        return redirect("verkauf:beleg", beleg_id=beleg.pk)
    ohne = [p.bezeichnung for p in getattr(beleg, "_ohne_lieferant", [])]
    messages.success(request, f"{len(liste)} Bestellung(en) angelegt (nicht abgeschlossen)." +
                     (" Sie ist noch nicht gespeichert: „Speichern: abschließen & Nummer vergeben“ vergibt die Nummer, sonst wird sie verworfen."
                      if liste[0].vorlaeufig else "") +
                     (f" Ohne Lieferant geblieben: {', '.join(ohne)}." if ohne else ""))
    return redirect("einkauf:bestellung", bestellung_id=liste[0].pk) if len(liste) == 1 else redirect("einkauf:bestellungen")


@mit_mandant
def vorschlag(request):
    if request.method == "POST":
        try:
            auswahl = []
            for artikel_id in request.POST.getlist("auswahl"):
                if not artikel_id.isdigit():
                    raise ValueError("Ungültige Artikelauswahl.")
                lieferant_id = request.POST.get(f"lieferant_{artikel_id}", "")
                auswahl.append((int(artikel_id), zahl(request.POST.get(f"menge_{artikel_id}"), Decimal("1")),
                                int(lieferant_id) if lieferant_id.isdigit() else None))
            if not auswahl:
                raise ValueError("Bitte mindestens einen Artikel auswählen.")
            liste = services.bestellungen_aus_vorschlag(request.mandant, auswahl)
            messages.success(request, f"{len(liste)} Bestellung(en) angelegt (nicht abgeschlossen): "
                             + ", ".join(f"{b.lieferant.kurz}" for b in liste) + ".")
            return redirect("einkauf:bestellungen")
        except ValueError as fehler:
            messages.error(request, str(fehler))
    return render(request, "einkauf/vorschlag.html", {"zeilen": services.bedarf_zeilen(request.mandant)})


# ---- Wareneingang ---------------------------------------------------------------------------------

@mit_mandant
def wareneingaenge(request):
    services.leere_wareneingaenge_entfernen(request.mandant, benutzer=request.user)  # ohne Position wird nicht aufbewahrt
    seite, parameter = blaettern(request, Wareneingang.objects.filter(mandant=request.mandant)
                                 .select_related("lieferant", "bestellung").prefetch_related("rechnungen").order_by("-datum", "-id"))
    return render(request, "einkauf/wareneingaenge.html", {
        "liste": seite, "seite": seite, "parameter": parameter,
        "lieferanten": lieferanten_auswahl(),
        "offene": Bestellung.objects.filter(mandant=request.mandant, status__in=[BestellStatus.BESTELLT, BestellStatus.TEIL])
        .select_related("lieferant")})


@mit_mandant
@require_POST
@schutz("einkauf:wareneingaenge")
def wareneingang_neu(request):
    if request.POST.get("bestellung"):
        b = get_object_or_404(Bestellung, pk=request.POST["bestellung"], mandant=request.mandant)
        frage = _erst_abschliessen(request, b, "wareneingang")
        if frage:
            return frage
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
    kalkulation = we.kalkulation()
    positionen = list(we.positionen.select_related("artikel", "bestellposition__bestellung"))
    for p in positionen:
        p.kalk = p.kalk_ek if we.gebucht and p.kalk_ek is not None else kalkulation[p.pk]
    p_rueckfrage = None
    if not we.gebucht and request.GET.get("p_code"):  # Scan traf einen Preislistenartikel ohne eigenen Artikel: erst fragen
        eintrag = services.preislisten_treffer(request.GET["p_code"], we.lieferant)
        if eintrag is not None:
            menge = request.GET.get("menge", "1")
            p_rueckfrage = {"code": request.GET["p_code"], "menge": menge, "eintrag": eintrag,
                            "formular_url": artikel_formular_url(eintrag, we, we_menge=menge)}
    p_offen = [] if we.gebucht else services.offene_p_positionen(we)
    for bp in p_offen:  # „Im Formular anlegen …“: Artikelmaske mit den Lieferantendaten, nach dem Speichern übernommen
        bp.formular_url = artikel_formular_url(bp.preisliste_artikel, we, we_bp=bp.pk)
    laufende = [] if we.gebucht else services.laufende_bestellungen(we)
    return render(request, "einkauf/wareneingang.html", {
        "we": we, "positionen": positionen, "netto": we.summe_netto, "bleibt_offen": services.bleibt_offen(we),
        "rechnungen": list(we.rechnungen.filter(mandant=request.mandant).order_by("datum", "pk")),
        "p_offen": p_offen, "p_rueckfrage": p_rueckfrage, "laufende": laufende,
        "laufende_anzahl": sum(1 for g in laufende for bp in g["positionen"] if bp.rest > 0)})


@mit_mandant
@require_POST
@schutz("einkauf:wareneingang", "we_id")
def wareneingang_uebernehmen(request, we_id):
    """Tab „Bestellungen“: ganze laufende Bestellung (komplett=ID) oder gewählte Positionen mit Menge übernehmen."""
    we = we_holen(request, we_id)
    gruppen = {g["bestellung"].pk: g for g in services.laufende_bestellungen(we)}
    p_anlegen = request.POST.get("p_anlegen") == "1"
    auswahl = []
    if request.POST.get("komplett", "").isdigit():
        g = gruppen.get(int(request.POST["komplett"]))
        if g is None:
            raise ValueError("Die Bestellung ist nicht (mehr) offen.")
        auswahl = [(bp, bp.rest) for bp in g["positionen"] if bp.rest > 0 and (p_anlegen or not bp.ist_p)]
        if not auswahl:
            raise ValueError(f"Aus {g['bestellung'].nummer} ist nichts mehr zu übernehmen"
                             + (" (nur Preislistenartikel – bitte das Anlegen bestätigen)." if any(bp.ist_p and bp.rest > 0 for bp in g["positionen"]) else "."))
    else:
        nach_id = {bp.pk: bp for g in gruppen.values() for bp in g["positionen"]}
        for bp_id in dict.fromkeys(request.POST.getlist("bp")):  # doppelt geschickte Position nur einmal
            if not bp_id.isdigit() or int(bp_id) not in nach_id:
                raise ValueError("Die Position ist nicht (mehr) offen.")
            auswahl.append((nach_id[int(bp_id)], zahl(request.POST.get(f"menge_{bp_id}"), None)))
    anzahl = services.wareneingang_uebernehmen(we, auswahl, p_anlegen=p_anlegen)
    messages.success(request, f"{anzahl} Position(en) aus der Bestellung übernommen – bitte Mengen prüfen und buchen.")
    return redirect(reverse("einkauf:wareneingang", args=[we.pk]) + "#positionen")


def artikel_formular_url(eintrag, we, **bezug):
    """Neuer Artikel aus einem Preislistenartikel, vorausgefüllt, mit Rückweg in den Wareneingang."""
    return f"{reverse('stammdaten:artikel_neu')}?{urlencode({'kopie_von_pl': eintrag.pk, 'we': we.pk, **bezug})}"


@mit_mandant
@require_POST
@schutz("einkauf:wareneingang", "we_id")
def wareneingang_loeschen(request, we_id):
    """Ungebuchten Wareneingang löschen; die Mengen bleiben in der Bestellung offen."""
    we = we_holen(request, we_id)
    bestellung_id = we.bestellung_id
    nummer = services.wareneingang_loeschen(we)
    messages.success(request, f"Wareneingang {nummer} gelöscht.")
    return redirect("einkauf:bestellung", bestellung_id=bestellung_id) if bestellung_id else redirect("einkauf:wareneingaenge")


@mit_mandant
@require_POST
@schutz("einkauf:wareneingang", "we_id")
def wareneingang_aufnehmen(request, we_id, bp_id):
    """Als „nicht geliefert“ entfernte Bestellposition doch mit der offenen Menge in den Wareneingang übernehmen.
    Preislistenposition (P) ohne Artikel nur nach Rückfrage (p_anlegen=1): dann wird sie zum eigenen Artikel."""
    we = we_holen(request, we_id)
    if not we.bestellung_id:
        raise ValueError("Der Wareneingang gehört zu keiner Bestellung.")
    bp = get_object_or_404(BestellPosition, pk=bp_id, bestellung=we.bestellung)
    if bp.artikel_id is None and bp.preisliste_artikel_id and not request.POST.get("p_anlegen"):
        raise ValueError("Preislistenartikel: Der eigene Artikel wird nur nach Rückfrage angelegt.")
    services.wareneingang_aus_bestellung(we, nur=bp, p_anlegen=bool(request.POST.get("p_anlegen")))
    return redirect("einkauf:wareneingang", we_id=we.pk)


@mit_mandant
@require_POST
@schutz("einkauf:wareneingang", "we_id")
def wareneingang_p_anlegen(request, we_id):
    """Nach Rückfrage: offene Preislistenpositionen der Bestellung als eigene Artikel (A) anlegen und übernehmen."""
    we = we_holen(request, we_id)
    if not we.bestellung_id:
        raise ValueError("Der Wareneingang gehört zu keiner Bestellung.")
    anzahl = len(services.offene_p_positionen(we))
    with transaction.atomic():
        services.wareneingang_aus_bestellung(we, p_anlegen=True, nur_p=True)
    messages.success(request, f"{anzahl} Artikel aus der Preisliste angelegt und übernommen.")
    return redirect("einkauf:wareneingang", we_id=we.pk)


@mit_mandant
@require_POST
@schutz("einkauf:wareneingang", "we_id")
def wareneingang_scan(request, we_id):
    """Scan-Feld (Bluetooth-Scanner sendet Code + Enter): jeder Scan erhöht die Menge um 1."""
    we = we_holen(request, we_id)
    code = request.POST.get("code", "").strip()
    artikel = services.artikel_per_code(code, we.lieferant)
    if artikel is None:
        eintrag = services.preislisten_treffer(code, we.lieferant)
        if eintrag is None:
            raise ValueError(f"Code '{code}' keinem Artikel zugeordnet.")
        if not request.POST.get("p_anlegen"):  # nie still anlegen: zurück zur Seite mit Rückfrage
            return redirect(reverse("einkauf:wareneingang", kwargs={"we_id": we.pk})
                            + "?" + urlencode({"p_code": code, "menge": request.POST.get("menge", "1")}))
        artikel = services.artikel_aus_preisliste(eintrag)
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
    menge = zahl(request.POST.get("menge"), pos.menge)
    if request.POST.get("aktion") == "loeschen" or (menge == 0 and pos.bestellposition_id):
        # nicht geliefert: Zeile raus, die Menge bleibt in der Bestellung offen (Teillieferung)
        pos.delete()
        if pos.bestellposition_id:
            messages.info(request, f"{pos.artikel.nummer}: nicht geliefert – bleibt in der Bestellung offen.")
    else:
        pos.menge = menge
        pos.ek = services.ek_runden(zahl(request.POST.get("ek"), pos.ek))
        if pos.menge <= 0 or pos.ek < 0:
            raise ValueError("Die Menge muss größer als 0 sein und der EK darf nicht negativ sein.")
        pos.seriennummern = request.POST.get("seriennummern", "")
        if "esd_keys" in request.POST:
            pos.esd_keys = request.POST["esd_keys"]
        pos.save()
    return redirect("einkauf:wareneingang", we_id=we.pk)


@mit_mandant
@require_POST
@schutz("einkauf:wareneingang", "we_id")
def wareneingang_buchen(request, we_id):
    pk = we_holen(request, we_id).pk
    with transaction.atomic():  # Sperre: Doppelklick oder zweiter Tab dürfen weder doppelt buchen noch „gebucht“ zurücksetzen
        we = Wareneingang.objects.select_for_update().get(pk=pk)
        if we.gebucht:
            raise ValueError("Der Wareneingang ist bereits gebucht.")
        we.lieferschein = request.POST.get("lieferschein", we.lieferschein).strip()
        we.nebenkosten_extern = bool(request.POST.get("nebenkosten_extern"))
        felder = ["lieferschein", "nebenkosten_extern"]
        for feld, name in (("versandkosten", "Versandkosten"), ("versicherung", "Versicherungskosten"), ("verpackung", "Verpackungskosten")):
            wert = zahl(request.POST.get(feld), Decimal("0"))
            if not Decimal("0") <= wert < Decimal("100000000"):
                raise ValueError(f"{name} dürfen nicht negativ sein.")
            setattr(we, feld, wert.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
            felder.append(feld)
        we.save(update_fields=felder)
        if request.POST.get("aktion") == "kosten":
            messages.success(request, "Nebenkosten gespeichert, Kalkulations-EK neu berechnet.")
            return redirect(reverse("einkauf:wareneingang", args=[we.pk]) + "#versand")
        bestaetigt = bool(request.POST.get("preis_bestaetigt"))
        if not bestaetigt:  # Plausibilität: Nebenkosten bleiben gespeichert, gebucht wird erst nach Bestätigung
            rueckfragen = services.mengen_rueckfragen(we) + services.preis_rueckfragen(we)
            if rueckfragen:
                messages.warning(request, " | ".join(rueckfragen))
                return redirect(reverse("einkauf:wareneingang", kwargs={"we_id": we.pk}) + "?rueckfrage=1")
        anzahl = services.wareneingang_buchen(we, request.user, bestaetigt=True)
    we.refresh_from_db()  # gebucht: „geliefert“ der Bestellung enthält jetzt diese Lieferung
    rest = services.bleibt_offen(we)
    messages.success(request, f"{anzahl} Position(en) ins Lager gebucht."
                     + (f" Teillieferung: {len(rest)} Position(en) bleiben in Bestellung {we.bestellung.nummer} offen." if rest else ""))
    return redirect("einkauf:wareneingang", we_id=we.pk)


# ---- Lieferantenrechnungen / SEPA -----------------------------------------------------------------

@mit_mandant
def rechnungen(request, sicht=""):
    """sicht: „“ = Einkauf, „op“ = Finanzen → Offene Posten Lieferanten, „zahlung“ = Finanzen → Zahlungen Lieferanten."""
    status = request.GET.get("status", "offen")
    liste = Lieferantenrechnung.objects.filter(mandant=request.mandant).select_related("lieferant") \
        .prefetch_related("wareneingaenge__positionen", "verrechnungen")
    if status == "offen":  # nicht bezahlt: noch offen oder schon per SEPA exportiert
        liste = liste.exclude(status=RechnungStatus.BEZAHLT)
    elif status in RechnungStatus.values:  # u. a. „exportiert“: SEPA-Datei erzeugt, Zahlung noch nicht gebucht
        liste = liste.filter(status=status)
    q = request.GET.get("q", "").strip()[:100]
    for wort in q.split()[:6]:  # Suche: Rechnungsnr., Lieferant (Kurzname, Name, Nummer) oder Nummer eines verknüpften Wareneingangs
        liste = liste.filter(Q(rechnungsnummer__icontains=wort) | Q(lieferant__kurzname__icontains=wort) | Q(lieferant__name1__icontains=wort)
                             | Q(lieferant__nummer__iexact=wort) | Q(wareneingaenge__nummer__icontains=wort))
    if q:
        liste = liste.distinct()
    seite, parameter = blaettern(request, liste)  # Modellsortierung: fällig, id
    zeilen =[(r, r.abgleich(), r.zahlbetrag()) for r in seite]
    return render(request, "einkauf/rechnungen.html", {
        "abweichungen": sum(1 for _, a, _ in zeilen if a["offen"]),
        "seite": seite, "parameter": parameter,
        "zeilen": zeilen, "status": status, "q": q, "sicht": sicht, "lieferanten": lieferanten_auswahl(),
        "gutschriften": services.offene_gutschriften(request.mandant) if sicht in ("op", "zahlung") else [],
        "eingaenge": LieferantengutschriftZahlung.objects.filter(gutschrift__mandant=request.mandant).select_related("gutschrift", "gutschrift__lieferant")[:30]
        if sicht == "zahlung" else [],
        "heute": date.today().isoformat(), "summe": sum((z[2] for z in zeilen), Decimal("0")),
        "sepa_avis": sepa_avis_gruppen(request), "entwurf": rechnung_entwurf(request)})


def rechnung_entwurf(request) -> dict:
    """Eingaben einer nicht gespeicherten Rechnung (einmalig, nur im selben Mandanten)."""
    entwurf = request.session.pop(RECHNUNG_ENTWURF, None) or {}
    return entwurf if entwurf.get("mandant") == request.mandant.pk else {}


RECHNUNG_ENTWURF = "rechnung_entwurf"


def lieferant_aus_text(text):
    """Getippter Lieferant ohne Auswahl aus der Liste: genau ein Treffer über Kurzname, Nummer oder Name, sonst None."""
    text = (text or "").strip()
    if not text:
        return None
    for abfrage in (Q(kurzname__iexact=text) | Q(nummer__iexact=text) | Q(name1__iexact=text),
                    Q(kurzname__icontains=text) | Q(name1__icontains=text)):
        treffer = list(Lieferant.objects.filter(abfrage, gesperrt=False)[:2])
        if len(treffer) == 1:
            return treffer[0]
        if treffer:
            return None  # mehrdeutig: nicht raten
    return None


@mit_mandant
@require_POST
def rechnung_neu(request):
    """Lieferantenrechnung von Hand erfassen. Bei einem Fehler bleiben alle Eingaben (auch die angehakten Wareneingänge)
    stehen und die Meldung steht direkt am Formular (Wunsch des Nutzers 20.11.2026: eine Rechnung ging unbemerkt verloren)."""
    zurueck = request.POST.get("zurueck", "")
    zurueck = zurueck if zurueck.startswith("/") and not zurueck.startswith("//") else reverse("einkauf:rechnungen")
    try:
        return _rechnung_neu(request)
    except ValueError as fehler:
        request.session[RECHNUNG_ENTWURF] = {
            "fehler": str(fehler), "mandant": request.mandant.pk,
            **{f: request.POST.get(f, "")[:200] for f in ("lieferant", "lieferant_text", "rechnungsnummer", "datum", "netto", "brutto")},
            "wareneingaenge": [i for i in request.POST.getlist("wareneingang") if i.isdigit()][:50]}
        messages.error(request, f"Rechnung nicht gespeichert: {fehler}")
        return redirect(zurueck + "#rechnung-erfassen")


def _rechnung_neu(request):
    lieferant_id = request.POST.get("lieferant", "")
    lieferant = Lieferant.objects.filter(pk=lieferant_id).first() if lieferant_id.isdigit() else None
    if lieferant is None:
        lieferant = lieferant_aus_text(request.POST.get("lieferant_text"))
    if lieferant is None:
        raise ValueError("Lieferant nicht eindeutig – bitte aus der Liste wählen.")
    netto, brutto = zahl(request.POST.get("netto")), zahl(request.POST.get("brutto"))
    if brutto is None or not request.POST.get("rechnungsnummer", "").strip():
        raise ValueError("Rechnungsnummer und Bruttobetrag sind Pflicht.")
    if netto is None:
        netto = (brutto / Decimal("1.19")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    ids = {i for i in request.POST.getlist("wareneingang") if i.isdigit()}
    frei = list(services.freie_wareneingaenge(request.mandant, lieferant).filter(pk__in=ids)) if ids else []
    r = services.rechnung_erfassen(request.mandant, lieferant, request.POST["rechnungsnummer"].strip(),
                                   tag(request.POST.get("datum")), netto, brutto, wareneingaenge=frei)
    if len(frei) < len(ids):
        messages.warning(request, "Wareneingänge, die inzwischen zu einer anderen Rechnung gehören, wurden nicht verknüpft.")
    elif frei:
        messages.success(request, f"Rechnung erfasst und mit {len(frei)} Wareneingang{'' if len(frei) == 1 else 'en'} verknüpft.")
    return redirect("einkauf:rechnung", rechnung_id=r.pk)


@mit_mandant
def rechnung_wareneingaenge(request):
    """Für das Erfassen einer Lieferantenrechnung: freie gebuchte Wareneingänge des gewählten Lieferanten (JSON)."""
    lieferant_id = request.GET.get("lieferant", "")
    lieferant = Lieferant.objects.filter(pk=lieferant_id).first() if lieferant_id.isdigit() else None
    if lieferant is None:
        return JsonResponse({"wareneingaenge": []})
    return JsonResponse({"wareneingaenge": [
        {"pk": w.pk, "nummer": w.nummer, "datum": f"{w.datum:%d.%m.%Y}", "bestellung": w.bestellung.nummer if w.bestellung_id else "",
         "lieferschein": w.lieferschein, "netto": f"{w.rechnungswert:.2f}"}
        for w in services.freie_wareneingaenge(request.mandant, lieferant)[:200]]})


@mit_mandant
@require_POST
@schutz("einkauf:rechnungen")
def rechnung_erechnung(request):
    datei = request.FILES.get("datei")
    if not datei:
        raise ValueError("Bitte eine XRechnung-/ZUGFeRD-Datei (XML oder PDF) wählen.")
    daten = erechnung.lese(datei.name, datei.read())
    ust_mandant, ust_empfaenger = ust_normal(request.mandant.ust_id), ust_normal(daten["empfaenger_ust_id"])
    if ust_mandant and ust_empfaenger and ust_mandant != ust_empfaenger:
        raise ValueError(f"Die Rechnung ist an eine andere USt-ID gerichtet ({daten['empfaenger_ust_id']}"
                         f"{', ' + daten['empfaenger'] if daten['empfaenger'] else ''}) als die des Mandanten – nicht übernommen.")
    lieferant = Lieferant.objects.filter(ust_id__iexact=daten["ust_id"]).first() if daten["ust_id"] else None
    if lieferant is None and daten["lieferant"]:
        lieferant = Lieferant.objects.filter(name1__iexact=daten["lieferant"]).first()
    if lieferant is None and daten["lieferant"]:  # Namensanfang nur bei genau einem Treffer, sonst wird nicht geraten
        treffer = list(Lieferant.objects.filter(name1__icontains=daten["lieferant"][:20])[:2])
        lieferant = treffer[0] if len(treffer) == 1 else None
    if lieferant is None:
        raise ValueError(f"Lieferant '{daten['lieferant']}' nicht gefunden – bitte zuerst als Lieferant anlegen.")
    ust_lieferant = ust_normal(lieferant.ust_id)
    if daten["ust_id"] and ust_lieferant and ust_normal(daten["ust_id"]) != ust_lieferant:
        raise ValueError(f"Die USt-ID in der Rechnung ({daten['ust_id']}) passt nicht zu Lieferant {lieferant.name1} "
                         f"({lieferant.ust_id}) – nicht übernommen.")
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
        if r.status == RechnungStatus.BEZAHLT:
            messages.error(request, "Die Rechnung ist bezahlt – die Verknüpfung lässt sich nicht mehr ändern.")
            return redirect("einkauf:rechnung", rechnung_id=r.pk)
        ids = request.POST.getlist("wareneingang")
        bisher = set(r.wareneingaenge.values_list("pk", flat=True))
        frei = Wareneingang.objects.filter(pk__in=ids, mandant=request.mandant, lieferant=r.lieferant).exclude(
            rechnungen__in=Lieferantenrechnung.objects.exclude(pk=r.pk)) \
            .filter(Q(ersatz_fuer__isnull=True) | Q(pk__in=bisher))  # ein WE je Rechnung; Ersatz aus einer Rücksendung gehört zu keiner
        if frei.count() < len(set(ids)):
            messages.warning(request, "Wareneingänge, die schon zu einer anderen Rechnung gehören, wurden nicht verknüpft.")
        r.wareneingaenge.set(frei)
        messages.success(request, "Verknüpfung gespeichert.")
        return redirect("einkauf:rechnung", rechnung_id=r.pk)
    verknuepft = set(r.wareneingaenge.values_list("pk", flat=True))
    kandidaten = services.freie_wareneingaenge(request.mandant, r.lieferant, r)
    return render(request, "einkauf/rechnung.html", {
        "r": r, "abgleich": r.abgleich(), "zahlbetrag": r.zahlbetrag(), "kandidaten": kandidaten, "verknuepft": verknuepft,
        "verrechnungen": r.verrechnungen.select_related("gutschrift"), "verrechnet": r.verrechnet,
        "heute": date.today().isoformat()})


@mit_mandant
@require_POST
@schutz("einkauf:rechnung", "rechnung_id")
def rechnung_bezahlt(request, rechnung_id):
    get_object_or_404(Lieferantenrechnung, pk=rechnung_id, mandant=request.mandant)
    with transaction.atomic():  # gesperrt: eine gleichzeitige Verrechnung mit einer Gutschrift ändert den Zahlbetrag
        r = Lieferantenrechnung.objects.select_for_update().select_related("lieferant").get(pk=rechnung_id)
        if r.status == RechnungStatus.BEZAHLT:
            raise ValueError("Die Rechnung ist schon als bezahlt gebucht.")
        if hinweis := r.abweichung_hinweis():  # nur Hinweis, keine Sperre (Wunsch des Nutzers 24.11.2026)
            messages.warning(request, f"Bezahlt gebucht trotz Abweichung: {hinweis}")
        betrag = zahl(request.POST.get("betrag"), r.zahlbetrag(tag(request.POST.get("datum"))))
        if betrag <= 0:
            raise ValueError("Der bezahlte Betrag muss größer als 0 sein.")
        r.status = RechnungStatus.BEZAHLT
        r.bezahlt_am = tag(request.POST.get("datum"))
        r.bezahlter_betrag = betrag
        r.save()
    return redirect("einkauf:rechnung", rechnung_id=r.pk)


@mit_mandant
@require_POST
@schutz("einkauf:rechnung", "rechnung_id")
@nur_admin
def rechnung_zahlung_storno(request, rechnung_id):
    r = get_object_or_404(Lieferantenrechnung, pk=rechnung_id, mandant=request.mandant)
    r.zahlung_stornieren(request.POST.get("grund"), request.user)
    messages.success(request, "Zahlung storniert; die Rechnung ist wieder offen.")
    return redirect("einkauf:rechnung", rechnung_id=r.pk)


@mit_mandant
@require_POST
@schutz("einkauf:rechnung", "rechnung_id")
def rechnung_loeschen(request, rechnung_id):
    r = get_object_or_404(Lieferantenrechnung.objects.select_related("lieferant"), pk=rechnung_id, mandant=request.mandant)
    name = str(r)
    try:
        nummern = services.rechnung_loeschen(r, request.POST.get("notiz"), request.user)
    except Lieferantenrechnung.DoesNotExist:  # zweiter Klick oder anderer Tab
        messages.info(request, f"Rechnung {name} wurde bereits gelöscht.")
        return redirect("einkauf:rechnungen")
    messages.success(request, f"Rechnung {name} gelöscht." + (f" Zuordnung aufgehoben: {', '.join(nummern)}." if nummern else ""))
    return redirect("einkauf:rechnungen")


@mit_mandant
@require_POST
@schutz("einkauf:rechnung", "rechnung_id")
def rechnung_notiz(request, rechnung_id):
    r = get_object_or_404(Lieferantenrechnung, pk=rechnung_id, mandant=request.mandant)
    r.bemerkung = request.POST.get("bemerkung", "").strip()[:200]
    r.save(update_fields=["bemerkung"])
    messages.success(request, "Notiz gespeichert.")
    return redirect("einkauf:rechnung", rechnung_id=r.pk)


@mit_mandant
@require_POST
@schutz("einkauf:rechnung", "rechnung_id")
def rechnung_abweichung_bestaetigen(request, rechnung_id):
    r = get_object_or_404(Lieferantenrechnung, pk=rechnung_id, mandant=request.mandant)
    r.abweichung_bestaetigen(request.POST.get("grund"), request.user)
    messages.success(request, "Abweichung bestätigt und protokolliert.")
    return redirect("einkauf:rechnung", rechnung_id=r.pk)


@mit_mandant
@require_POST
@schutz("einkauf:rechnung", "rechnung_id")
def rechnung_korrigieren(request, rechnung_id):
    r = get_object_or_404(Lieferantenrechnung, pk=rechnung_id, mandant=request.mandant)
    r.betraege_korrigieren(zahl(request.POST.get("netto")), zahl(request.POST.get("steuer")), zahl(request.POST.get("brutto")),
                           request.user)
    messages.success(request, "Beträge korrigiert und protokolliert.")
    return redirect("einkauf:rechnung", rechnung_id=r.pk)


@mit_mandant
@require_POST
@schutz("einkauf:rechnungen")
def sepa_export(request):
    ausfuehrung = tag(request.POST.get("ausfuehrung"))
    with transaction.atomic():  # keine Überweisung für eine Rechnung, die gerade gelöscht wird
        auswahl = list(Lieferantenrechnung.objects.select_for_update().filter(pk__in=request.POST.getlist("auswahl"), mandant=request.mandant)
                       .exclude(status=RechnungStatus.BEZAHLT).select_related("lieferant"))
        abweichend = [h for r in auswahl if (h := r.abweichung_hinweis())]  # nur Hinweis, keine Sperre (24.11.2026)
        if abweichend:
            messages.warning(request, "Exportiert trotz Abweichung zum Wareneingang: " + " ".join(abweichend))
        if any(r.status == RechnungStatus.EXPORTIERT for r in auswahl):
            messages.warning(request, "Achtung: Die Auswahl enthält Rechnungen, die schon einmal exportiert wurden – nicht doppelt bei der Bank einreichen.")
        daten = sepa.erzeuge(request.mandant, auswahl, ausfuehrung)
        Lieferantenrechnung.objects.filter(pk__in=[r.pk for r in auswahl]).update(status=RechnungStatus.EXPORTIERT)
    # Merken für den Hinweis „Zahlungsavis senden“ auf der Rechnungsliste (Wunsch des Nutzers 20.11.2026)
    request.session[sepa_avis_schluessel(request.mandant)] = {
        "datum": ausfuehrung.isoformat(), "ids": [r.pk for r in auswahl], "gesendet": []}
    antwort = HttpResponse(daten, content_type="application/xml")
    antwort["Content-Disposition"] = f'attachment; filename="SEPA_{request.mandant.kurzname}_{ausfuehrung:%Y%m%d}.xml"'
    return antwort


def sepa_avis_schluessel(mandant) -> str:
    return f"sepa_avis_{mandant.pk}"


def sepa_avis_gruppen(request):
    """Letzter SEPA-Export dieser Sitzung je Lieferant: Rechnungen, Zahlbetrag am Ausführungstag, Links zum Zahlungsavis."""
    gemerkt = request.session.get(sepa_avis_schluessel(request.mandant))
    if not gemerkt:
        return None
    try:
        datum = date.fromisoformat(gemerkt["datum"])
    except (KeyError, TypeError, ValueError):
        return None
    rechnungen = Lieferantenrechnung.objects.filter(mandant=request.mandant, pk__in=gemerkt.get("ids", [])) \
        .exclude(status=RechnungStatus.OFFEN).select_related("lieferant").order_by("lieferant__name1", "pk")
    gruppen = {}
    for r in rechnungen:
        gruppen.setdefault(r.lieferant_id, {"lieferant": r.lieferant, "rechnungen": []})["rechnungen"].append(r)
    if not gruppen:
        return None
    for lieferant_id, g in gruppen.items():
        g["summe"] = services.zahlungsavis(g["rechnungen"], datum)["summe"]["zahlung"]
        g["query"] = avis_query(g["rechnungen"], datum)
        g["gesendet"] = lieferant_id in gemerkt.get("gesendet", [])
    return {"datum": datum, "gruppen": list(gruppen.values())}


def sepa_avis_gesendet(request, lieferant, auswahl):
    """Nach dem Mailversand den Lieferanten im Hinweis als erledigt markieren, wenn das Avis zum letzten Export gehört."""
    schluessel = sepa_avis_schluessel(request.mandant)
    gemerkt = request.session.get(schluessel)
    if gemerkt and {r.pk for r in auswahl} & set(gemerkt.get("ids", [])) and lieferant.pk not in gemerkt.get("gesendet", []):
        gemerkt.setdefault("gesendet", []).append(lieferant.pk)
        request.session[schluessel] = gemerkt


@mit_mandant
@require_POST
def sepa_avis_ausblenden(request):
    request.session.pop(sepa_avis_schluessel(request.mandant), None)
    weiter = request.POST.get("weiter", "")
    return redirect(weiter if weiter.startswith("/") and not weiter.startswith("//") else "einkauf:rechnungen")


# ---- RMA --------------------------------------------------------------------------------------------

@mit_mandant
def rma_liste(request):
    # Vorbelegung aus der Rückverfolgung (Seriennummer bzw. Wareneingang am Artikel): Lieferant, Artikel, Seriennummer
    vor = {name: request.GET.get(name, "").strip()[:100] for name in ("lieferant", "artikel", "seriennummer")}
    vor_artikel = Artikel.objects.filter(nummer__iexact=vor["artikel"]).first() if vor["artikel"] else None
    vor["bezeichnung"] = vor_artikel.bezeichnung if vor_artikel else ""
    vorbelegt = any(vor.values())
    lieferanten = list(lieferanten_auswahl())
    vor_lieferant = Lieferant.objects.filter(pk=vor["lieferant"]).first() if vor["lieferant"].isdigit() else None
    if vor_lieferant and vor_lieferant not in lieferanten:  # inzwischen gesperrt: die Ware kam trotzdem von ihm
        lieferanten.insert(0, vor_lieferant)
    vor["lieferant"] = str(vor_lieferant.pk) if vor_lieferant else ""
    return render(request, "einkauf/rma_liste.html", {
        "liste": Rma.objects.filter(mandant=request.mandant).select_related("lieferant"),
        "lieferanten": lieferanten, "vor": vor, "vorbelegt": vorbelegt,
        # Herkunft unbekannt: nicht still den ersten Lieferanten der Liste vorwählen
        "lieferant_waehlen": vorbelegt and vor_lieferant is None})


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
        try:
            rma = services.rma_anlegen(request.mandant, lieferant, rep.geraet, rep.fehler, rep.artikel, rep.seriennummer, rep)
        except ValueError as fehler:  # z. B. Doppelklick: es gibt schon eine offene RMA zum Werkstattauftrag
            messages.error(request, str(fehler))
            return redirect("einkauf:rma_liste")
        messages.success(request, f"{rma.nummer} angelegt; Werkstattauftrag steht auf „in Lieferanten-RMA“.")
        return redirect("einkauf:rma", rma_id=rma.pk)
    return render(request, "einkauf/rma_aus_reparatur.html", {"rep": rep, "lieferanten": lieferanten_auswahl()})


@mit_mandant
def rma_detail(request, rma_id):
    rma = get_object_or_404(Rma.objects.select_related("lieferant", "reparatur"), pk=rma_id, mandant=request.mandant)
    if request.method == "POST":
        try:
            status = request.POST.get("status", rma.status)
            if status not in RmaStatus.values:
                raise ValueError("Ungültiger Status.")
            gutschrift = zahl(request.POST.get("gutschrift_betrag"))
            if gutschrift is not None and not Decimal("0") <= gutschrift < Decimal("10000000000"):
                raise ValueError("Der Gutschriftbetrag darf nicht negativ sein.")
            rma.status = status
            rma.lieferanten_rma_nr = request.POST.get("lieferanten_rma_nr", "").strip()[:60]
            rma.gutschrift_betrag = services.ek_runden(gutschrift) if gutschrift is not None else None
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


# ---- Preislisten ------------------------------------------------------------------------------------

@mit_mandant
def preislisten_liste(request):
    from .models import Preislistenprofil
    return render(request, "einkauf/preislisten.html", {
        "profile": Preislistenprofil.objects.select_related("lieferant").prefetch_related("importe")})


@mit_mandant
@admin_bei_aenderung
def preisliste(request, profil_id):
    from . import ablage, preislisten
    from .models import Preislistenprofil
    profil = get_object_or_404(Preislistenprofil.objects.select_related("lieferant"), pk=profil_id)
    kontext = {"profil": profil, "importe": profil.importe.all()[:10], "vorschau": None, "token": ""}
    if request.method == "GET" and request.GET.get("token"):
        from .views_assistent import tmp_lesen
        try:
            inhalt, name = tmp_lesen(request.GET["token"])
            kontext.update(vorschau=preislisten.vorschau(profil, inhalt), token=request.GET["token"], dateiname=name)
        except preislisten.PreislistenFehler as fehler:
            messages.error(request, str(fehler))
    if request.method == "POST":
        aktion = request.POST.get("aktion")
        try:
            if aktion == "vorschau":
                datei = request.FILES.get("datei")
                if profil.quelle == "upload" and not datei:
                    raise ValueError("Bitte eine Datei wählen.")
                if datei:
                    name, inhalt = datei.name, datei.read()
                else:
                    name, inhalt = preislisten.abrufen(profil)
                token = ablage.speichern(inhalt, name)
                kontext.update(vorschau=preislisten.vorschau(profil, inhalt), token=token, dateiname=name)
            elif aktion == "importieren":
                token = request.POST.get("token", "")
                datei = ablage.lesen(token)
                if datei is None:
                    raise ValueError("Die Vorschau ist abgelaufen – bitte erneut hochladen.")
                protokoll = preislisten.importieren(profil, datei[0], datei[1], request.user)
                ablage.loeschen(token)
                messages.success(request, f"Import abgeschlossen: {protokoll.meldung}")
                return redirect("einkauf:preislistenimport", import_id=protokoll.pk)
        except (ValueError, preislisten.PreislistenFehler) as fehler:
            messages.error(request, str(fehler))
    return render(request, "einkauf/preisliste.html", kontext)


@mit_mandant
def preislistenimport(request, import_id):
    from .models import Preislistenimport
    protokoll = get_object_or_404(Preislistenimport.objects.select_related("profil", "profil__lieferant"), pk=import_id)
    return render(request, "einkauf/preislistenimport.html", {"p": protokoll})


@mit_mandant
@admin_bei_aenderung
def preispruefliste(request):
    """Prüfliste: zurückgehaltene Importe (Quarantäne) und einzelne Preise mit großer Abweichung. Freigeben/Übernehmen per Klick."""
    from . import preislisten
    from .models import Preislistenimport, Preispruefung
    if request.method == "POST":
        aktion, pk = request.POST.get("aktion", ""), request.POST.get("id", "")
        try:
            if not pk.isdigit():
                raise ValueError("Eintrag nicht gefunden.")
            if aktion in ("import_freigeben", "import_verwerfen"):
                imp = get_object_or_404(Preislistenimport.objects.select_related("profil", "profil__lieferant"), pk=pk)
                if aktion == "import_freigeben":
                    neu = preislisten.freigeben(imp, request.user)
                    messages.success(request, f"Import freigegeben: {neu.meldung}")
                else:
                    preislisten.verwerfen(imp)
                    messages.success(request, "Import verworfen, keine Preise übernommen.")
            elif aktion in ("preis_uebernehmen", "preis_verwerfen"):
                pr = get_object_or_404(Preispruefung.objects.select_related("lieferant"), pk=pk)
                if aktion == "preis_uebernehmen":
                    preislisten.preis_uebernehmen(pr)
                    messages.success(request, f"Preis für {pr.artikel.nummer} übernommen.")
                else:
                    preislisten.preis_verwerfen(pr)
                    messages.success(request, "Preis verworfen, der bisherige bleibt.")
        except (ValueError, preislisten.PreislistenFehler) as fehler:
            messages.error(request, str(fehler))
        return redirect("einkauf:preispruefliste")
    return render(request, "einkauf/preispruefliste.html", {
        "importe": Preislistenimport.objects.filter(status="zur_pruefung").select_related("profil", "profil__lieferant"),
        "preise": Preispruefung.objects.filter(status="offen").select_related("artikel", "lieferant", "importlauf")[:500]})


@mit_mandant
@admin_bei_aenderung
def bestpreise_ansicht(request):
    """Günstigsten Lieferanten-EK je Herstellernummer in die A-Artikel übernehmen: erst Vorschau, dann anwenden."""
    from . import bestpreis
    if request.method == "POST":
        ergebnis = bestpreis.bestpreise(anwenden=True)
        messages.success(request, f"{len(ergebnis['aenderungen'])} Artikel mit dem günstigsten Lieferanten-EK aktualisiert.")
        return redirect("einkauf:bestpreise")
    ergebnis = bestpreis.bestpreise()
    return render(request, "einkauf/bestpreise.html", {"e": ergebnis, "beispiele": ergebnis["aenderungen"][:100],
                                                       "plan": Bestpreisplan.holen()})


# ---- WE-Rücksendung -----------------------------------------------------------------------------------

@mit_mandant
def ruecksendungen(request):
    """Liste der WE-Rücksendungen (Standard: offene) und Start aus einem gebuchten Wareneingang."""
    filter_ = request.GET.get("filter") or "offen"
    qs = Ruecksendung.objects.filter(mandant=request.mandant).select_related("lieferant", "wareneingang").prefetch_related("positionen")
    if filter_ == "offen":
        qs = qs.filter(status__in=RUECKSENDUNG_OFFEN)
    seite, parameter = blaettern(request, qs)
    gebuchte = Wareneingang.objects.filter(mandant=request.mandant, gebucht=True).select_related("lieferant").order_by("-datum", "-id")[:300]
    return render(request, "einkauf/ruecksendungen.html", {"liste": seite, "seite": seite, "parameter": parameter,
                                                            "filter": filter_, "gebuchte": gebuchte})


@mit_mandant
def ruecksendung_start(request):
    """„+ WE-Rücksendung“ auf der Lieferantenseite: gewählten gebuchten Wareneingang öffnen (Positionen und Grund)."""
    we_id = request.GET.get("we", "")
    if not we_id.isdigit():
        messages.error(request, "Bitte den Wareneingang wählen, aus dem Ware zurückgeht.")
        lieferant_id = request.GET.get("lieferant", "")
        return redirect("einkauf:lieferant", lieferant_id=int(lieferant_id)) if lieferant_id.isdigit() else redirect("einkauf:lieferanten")
    we = we_holen(request, int(we_id))
    return redirect("einkauf:ruecksendung_neu", we_id=we.pk)


@mit_mandant
def ruecksendung_neu(request, we_id):
    """Rücksendung aus einem gebuchten Wareneingang: je Position Menge (höchstens noch Zurücksendbares) und bei
    Seriennummern die Nummern wählen, dazu „zur Gutschrift“ oder „zum Austausch“ und der Grund."""
    we = we_holen(request, we_id)
    if not we.gebucht:
        messages.error(request, "Zurücksenden geht nur aus einem gebuchten Wareneingang.")
        return redirect("einkauf:wareneingang", we_id=we.pk)
    positionen = list(we.positionen.select_related("artikel"))
    for p in positionen:
        p.rest = services.ruecksendbar(p)
        p.sn_frei = services.seriennummern_im_lager(p)
        p.mit_sn = bool(services.zeilen(p.seriennummern))
        p.eingabe = request.POST.get(f"menge_{p.pk}", "")
        p.sn_gewaehlt = request.POST.getlist(f"sn_{p.pk}")
    if request.method == "POST":
        try:
            auswahl = []
            for p in positionen:
                menge = zahl(request.POST.get(f"menge_{p.pk}"), None)
                sn = request.POST.getlist(f"sn_{p.pk}")
                if sn and not menge:  # nur Seriennummern angehakt: die Menge ergibt sich daraus
                    menge = Decimal(len(sn))
                if menge:
                    auswahl.append((p, menge, sn))
            r = services.ruecksendung_anlegen(we, auswahl, request.POST.get("art", ""), request.POST.get("grund", ""), request.user)
        except ValueError as fehler:
            messages.error(request, str(fehler))
        else:
            messages.success(request, f"Rücksendung {r.nummer} angelegt: Die Ware ist aus dem Lager gebucht. "
                                      "Die Bestellung beim Lieferanten bleibt unverändert.")
            return redirect("einkauf:ruecksendung", ruecksendung_id=r.pk)
    return render(request, "einkauf/ruecksendung_neu.html", {
        "we": we, "positionen": positionen, "arten": RuecksendungArt.choices,
        "art": request.POST.get("art", ""), "grund": request.POST.get("grund", ""),
        "bisher": we.ruecksendungen.order_by("-id"),
        "zuruecksendbar": any(p.rest > 0 and not p.artikel.esd_key for p in positionen)})


@mit_mandant
def ruecksendung_detail(request, ruecksendung_id):
    r = get_object_or_404(Ruecksendung.objects.select_related("lieferant", "wareneingang", "ersatz_wareneingang"),
                          pk=ruecksendung_id, mandant=request.mandant)
    if request.method == "POST":
        aktion = request.POST.get("aktion")
        try:
            if aktion == "versendet":
                services.ruecksendung_versendet(r, request.user)
                messages.success(request, f"{r.nummer}: als versendet vermerkt.")
            elif aktion == "gutschrift":
                datum = tag(request.POST.get("datum")) if request.POST.get("datum") else None  # Pflicht, kein stilles „heute“
                r = services.ruecksendung_gutschrift(
                    r, request.POST.get("nummer"), datum, zahl(request.POST.get("netto"), None), zahl(request.POST.get("steuer"), None),
                    zahl(request.POST.get("brutto"), None), request.POST.get("notiz", ""), request.user)
                messages.success(request, f"{r.nummer}: Gutschrift erhalten und erfasst – jetzt mit Rechnungen verrechnen oder die Zahlung buchen.")
                return redirect("einkauf:gutschrift", gutschrift_id=r.lieferantengutschrift.pk)
            elif aktion == "ersatz":
                we = services.ruecksendung_ersatz(r, request.user)
                messages.success(request, f"{r.nummer}: Ersatz-Wareneingang {we.nummer} angelegt – bitte prüfen und buchen "
                                          "(neue Seriennummern dort erfassen).")
                return redirect("einkauf:wareneingang", we_id=we.pk)
            elif aktion == "abgelehnt":
                services.ruecksendung_abgelehnt(r, request.POST.get("grund", ""), request.user)
                messages.warning(request, f"{r.nummer}: abgelehnt – die Ware ist wieder im Lager.")
            else:
                raise ValueError("Unbekannte Aktion.")
        except ValueError as fehler:
            messages.error(request, str(fehler))
        return redirect("einkauf:ruecksendung", ruecksendung_id=r.pk)
    netto = r.summe_netto
    ust = (netto * Decimal("0.19")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)  # Vorschlag, änderbar
    vorgabe = {name: request.POST.get(name) if request.method == "POST" else wert for name, wert in
               (("nummer", ""), ("datum", date.today().isoformat()), ("netto", f"{netto:.2f}"), ("steuer", f"{ust:.2f}"),
                ("brutto", f"{netto + ust:.2f}"), ("notiz", ""))}
    return render(request, "einkauf/ruecksendung.html", {
        "r": r, "positionen": r.positionen.select_related("artikel", "we_position"), "vorgabe": vorgabe, "heute": date.today().isoformat(),
        "gutschrift": Lieferantengutschrift.objects.filter(ruecksendung=r).first()})


# ---- Gutschrift des Lieferanten (Entscheidung Nutzer 19.11.2026) -------------------------------------

def _gutschrift(request, gutschrift_id):
    return get_object_or_404(Lieferantengutschrift.objects.select_related("lieferant", "ruecksendung"), pk=gutschrift_id, mandant=request.mandant)


@mit_mandant
def gutschriften(request):
    """Einkauf → Lieferanten-Gutschriften: Liste (offen = Restguthaben > 0) und Erfassen ohne Rücksendung
    (z. B. Bonus, Preisnachlass, Gutschrift zu einer Rechnung; Wunsch des Nutzers 19.11.2026)."""
    status = request.GET.get("status", "offen")
    liste = Lieferantengutschrift.objects.filter(mandant=request.mandant).select_related("lieferant", "ruecksendung") \
        .prefetch_related("verrechnungen", "zahlungen").order_by("-datum", "-id")
    lieferant_id = request.GET.get("lieferant", "")
    if lieferant_id.isdigit():
        liste = liste.filter(lieferant_id=int(lieferant_id))
    zeilen = [g for g in liste if status != "offen" or g.restguthaben > 0][:500]
    return render(request, "einkauf/gutschriften.html", {
        "zeilen": zeilen, "status": status, "lieferanten": lieferanten_auswahl(), "heute": date.today().isoformat(),
        "summe": sum((g.restguthaben for g in zeilen), Decimal("0"))})


@mit_mandant
@require_POST
@schutz("einkauf:gutschriften")
def gutschrift_neu(request):
    lieferant_id = request.POST.get("lieferant", "")
    lieferant = Lieferant.objects.filter(pk=lieferant_id).first() if lieferant_id.isdigit() else None
    if lieferant is None:
        raise ValueError("Bitte einen Lieferanten aus der Liste wählen.")
    datum = tag(request.POST.get("datum")) if request.POST.get("datum") else None  # Pflicht, kein stilles „heute“
    g = services.gutschrift_erfassen(request.mandant, lieferant, request.POST.get("nummer"), datum,
                                     zahl(request.POST.get("netto"), None), zahl(request.POST.get("steuer"), None),
                                     zahl(request.POST.get("brutto"), None), request.POST.get("notiz", ""), benutzer=request.user)
    messages.success(request, f"Gutschrift {g.nummer} von {lieferant.name1} erfasst – jetzt mit Rechnungen verrechnen oder die Zahlung buchen.")
    return redirect("einkauf:gutschrift", gutschrift_id=g.pk)


@mit_mandant
def gutschrift_detail(request, gutschrift_id):
    g = _gutschrift(request, gutschrift_id)
    rechnungen = services.verrechenbare_rechnungen(g)
    rest = g.restguthaben
    return render(request, "einkauf/gutschrift.html", {
        "g": g, "rest": rest, "verrechnet": g.verrechnet, "eingegangen": g.eingegangen,
        "verrechnungen": g.verrechnungen.select_related("rechnung"), "zahlungen": g.zahlungen.select_related("bankumsatz"),
        "rechnungen": [(r, min(rest, r.offener_betrag)) for r in rechnungen], "heute": date.today().isoformat()})


@mit_mandant
@require_POST
@schutz("einkauf:gutschrift", "gutschrift_id")
def gutschrift_verrechnen(request, gutschrift_id):
    g = _gutschrift(request, gutschrift_id)
    rechnung_id = request.POST.get("rechnung", "")
    rechnung = Lieferantenrechnung.objects.filter(pk=rechnung_id, mandant=request.mandant, lieferant=g.lieferant).first() if rechnung_id.isdigit() else None
    if rechnung is None:
        raise ValueError("Bitte eine offene Rechnung dieses Lieferanten wählen.")
    betrag = zahl(request.POST.get("betrag"), None)
    v = services.gutschrift_verrechnen(g, rechnung, betrag, tag(request.POST.get("datum")), request.user)
    messages.success(request, f"{v.betrag:.2f} € mit Rechnung {rechnung.rechnungsnummer} verrechnet.".replace(".", ","))
    return redirect("einkauf:gutschrift", gutschrift_id=g.pk)


@mit_mandant
@require_POST
@schutz("einkauf:gutschrift", "gutschrift_id")
@nur_admin
def gutschrift_verrechnung_aufheben(request, gutschrift_id, verrechnung_id):
    g = _gutschrift(request, gutschrift_id)
    v = get_object_or_404(LieferantengutschriftVerrechnung, pk=verrechnung_id, gutschrift=g)
    services.verrechnung_aufheben(v, request.POST.get("grund"), request.user)
    messages.success(request, "Verrechnung rückgängig gemacht.")
    return redirect("einkauf:gutschrift", gutschrift_id=g.pk)


@mit_mandant
@require_POST
@schutz("einkauf:gutschrift", "gutschrift_id")
def gutschrift_zahlung(request, gutschrift_id):
    g = _gutschrift(request, gutschrift_id)
    z = services.gutschrift_zahlung_buchen(g, zahl(request.POST.get("betrag"), g.restguthaben), tag(request.POST.get("datum")), request.user)
    messages.success(request, f"Zahlung des Lieferanten über {z.betrag:.2f} € gebucht.".replace(".", ","))
    return redirect("einkauf:gutschrift", gutschrift_id=g.pk)


@mit_mandant
@require_POST
@schutz("einkauf:gutschrift", "gutschrift_id")
@nur_admin
def gutschrift_zahlung_storno(request, gutschrift_id, zahlung_id):
    g = _gutschrift(request, gutschrift_id)
    z = get_object_or_404(LieferantengutschriftZahlung, pk=zahlung_id, gutschrift=g)
    services.gutschrift_zahlung_stornieren(z, request.POST.get("grund"), request.user)
    messages.success(request, f"Zahlungseingang über {z.betrag} € zur Gutschrift {g.nummer} storniert – das Restguthaben ist wieder offen.")
    if request.POST.get("zurueck") == "zahlungen":  # Storno aus Finanzen → Zahlungen → Lieferanten
        return redirect("finanzen:zahlungen_lieferanten")
    return redirect("einkauf:gutschrift", gutschrift_id=g.pk)
