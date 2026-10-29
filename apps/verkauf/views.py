from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.kern.models import Mandant
from apps.stammdaten.models import (Ansprechpartner, Anschriftart, Artikel, Kunde, Preislistenartikel, Zahlungsbedingung)

from apps.lager.models import BestandReichtNicht
from .models import Abo, AboPosition, Versand, Versandart, Wiedervorlage, Beleg, BelegGesperrt, Belegart, Position
from .mail import MailFehler, beleg_senden
from .mail import vorschlag as mail_vorschlag
from .pdf import beleg_pdf


def dezimal(text, standard=None):
    text = (text or "").strip().replace(".", "").replace(",", ".") if "," in (text or "") else (text or "").strip()
    if not text:
        return standard
    try:
        wert = Decimal(text)
    except InvalidOperation:
        raise ValueError(f"Ungültige Zahl: {text}")
    if not wert.is_finite():
        raise ValueError(f"Ungültige Zahl: {text}")
    return wert


def datum_oder_none(text):
    from datetime import date
    try:
        return date.fromisoformat(text) if text else None
    except ValueError:
        raise ValueError(f"Ungültiges Datum: {text}")


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
    if mandant:
        Beleg.leere_entwuerfe_entfernen(mandant)
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
    offene = None
    if mandant and treffer is None:  # ohne Suche: die offenen (nicht abgeschlossenen) Belege zum Weiterbearbeiten
        offene = list(Beleg.objects.filter(mandant=mandant, abgeschlossen=False, positionen__isnull=False).distinct().select_related("kunde")
                      .order_by("-belegdatum", "-id")[:100])
    wiedervorlagen = mahnungen_faellig = None
    if mandant:
        from apps.finanzen.services import mahnvorschlag
        from django.utils import timezone
        wiedervorlagen = list(Wiedervorlage.objects.filter(mandant=mandant, erledigt=False, faellig__lte=timezone.localdate())
                              .select_related("kunde", "beleg")[:50])
        mahnungen_faellig = len(mahnvorschlag(mandant))
    return render(request, "verkauf/start.html", {
        "wiedervorlagen": wiedervorlagen, "mahnungen_faellig": mahnungen_faellig,
        "mandant": mandant, "mandanten": Mandant.objects.filter(aktiv=True), "q": q, "treffer": treffer,
        "offene": offene})


@mit_mandant
def kunde_detail(request, kunde_id):
    from datetime import date

    from django.core.paginator import Paginator

    from apps.finanzen import services as fin
    kunde = get_object_or_404(Kunde, pk=kunde_id)
    Beleg.leere_entwuerfe_entfernen(request.mandant, kunde)
    belege = Beleg.objects.filter(kunde=kunde, mandant=request.mandant, positionen__isnull=False).distinct()         .select_related("kunde", "zahlungsbedingung", "vorgaenger")  # Belege ohne Position gelten nicht als gespeichert
    art = request.GET.get("art", "")
    if art == "offen":
        belege = belege.filter(abgeschlossen=False)
    elif art in Belegart.values:
        belege = belege.filter(art=art)
    ab = request.GET.get("ab", "")
    try:
        if ab:
            belege = belege.filter(belegdatum__gte=date.fromisoformat(ab))
    except ValueError:
        ab = ""
    ohne_storno = request.GET.get("ohne_storno") == "1"
    if ohne_storno:  # Gutschriften und vollständig gutgeschriebene Rechnungen ausblenden
        storniert = [b.vorgaenger_id for b in Beleg.objects.filter(
            kunde=kunde, mandant=request.mandant, art=Belegart.GUTSCHRIFT, abgeschlossen=True).select_related("vorgaenger")
            if b.vorgaenger_id and b.summe_brutto >= b.vorgaenger.summe_brutto]
        belege = belege.exclude(art=Belegart.GUTSCHRIFT).exclude(pk__in=storniert)
    parameter = request.GET.copy()
    parameter.pop("seite", None)
    seite = Paginator(belege, 25).get_page(request.GET.get("seite"))
    zeilen = []
    for b in seite.object_list:
        rechnung = b.art == Belegart.RECHNUNG and b.abgeschlossen
        zeilen.append({
            "beleg": b, "faellig": fin.faellig_am(b) if rechnung else None,
            "bezahlt": fin.bezahlt(b) + fin.gutgeschrieben(b) if rechnung else None,
            "offen": fin.offen(b) if rechnung else None,
            "bezug": b.vorgaenger.nummer if b.vorgaenger_id and b.vorgaenger.nummer else "",
            "positionen": list(b.positionen.select_related("artikel")[:15])})
    offene_lieferscheine = sum(1 for b in Beleg.objects.filter(kunde=kunde, mandant=request.mandant, art=Belegart.LIEFERSCHEIN,
                                                                abgeschlossen=True) if b.abrechenbar)
    mandantendaten = kunde.mandantendaten.filter(mandant=request.mandant).first()
    offen = fin.kunde_offen(request.mandant, kunde)
    limit = mandantendaten.kreditlimit if mandantendaten else None
    return render(request, "verkauf/kunde.html", {
        "offen": offen, "limit_ueberschritten": bool(limit) and offen > limit, "sammelrechnung": offene_lieferscheine >= 2,
        "kunde": kunde, "zeilen": zeilen, "seite": seite, "parameter": parameter.urlencode(), "filter": art,
        "ab": ab, "ohne_storno": ohne_storno, "arten": Belegart.choices,
        "kreditlimit": mandantendaten.kreditlimit if mandantendaten else None,
        "ansprechpartner": kunde.ansprechpartner.all(),
        "wiedervorlagen": kunde.wiedervorlagen.filter(mandant=request.mandant, erledigt=False)})


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
    if art == Belegart.NACHLIEFERSCHEIN:
        messages.error(request, "Ein Nachlieferschein entsteht aus einer abgeschlossenen Rechnung: dort unter „Mehr“ → „Kopieren als Nachlieferschein“.")
        return redirect("verkauf:kunde", kunde_id=kunde.pk)
    if art == Belegart.LIEFERSCHEIN:
        messages.error(request, "Ein Lieferschein entsteht aus einem Auftrag: erst den Auftrag erfassen, "
                                "dann dort „Als Lieferschein übernehmen“.")
        return redirect("verkauf:kunde", kunde_id=kunde.pk)
    beleg = Beleg.objects.create(
        mandant=request.mandant, art=art, kunde=kunde,
        ansprechpartner=kunde.ansprechpartner.filter(hauptkontakt=True).first(),
        zahlungsbedingung=kunde.zahlungsbedingung,
        rechnungsanschrift=adresse_text(kunde, Anschriftart.RECHNUNG),
        versandanschrift=adresse_text(kunde, Anschriftart.VERSAND), bearbeiter=request.user)
    return redirect("verkauf:beleg", beleg_id=beleg.pk)


@mit_mandant
def beleg_detail(request, beleg_id):
    from apps.lager.services import druckhindernis
    from apps.lager.models import bestand, reserviert
    beleg = beleg_holen(request, beleg_id)
    positionen = list(beleg.positionen.select_related("artikel", "preisliste_artikel__lieferant"))
    for p in positionen:  # Warnung, wenn der verfügbare Bestand nicht reicht
        p.knapp = None
        if p.artikel_id and p.artikel.typ == "A" and not beleg.abgeschlossen and beleg.art in ("AB", "LS", "RE"):
            frei = bestand(beleg.mandant, p.artikel) - reserviert(beleg.mandant, p.artikel)
            if beleg.art == "AB":
                frei += p.menge  # die eigene Reservierung nicht doppelt zählen
            if frei < p.menge:
                p.knapp = frei
    from apps.finanzen.services import kunde_offen
    md = beleg.kunde.mandantendaten.filter(mandant=beleg.mandant).first()
    limit = md.kreditlimit if md else None
    offen = kunde_offen(beleg.mandant, beleg.kunde)
    kredit_warnung = bool(limit) and beleg.art != "AN" and not beleg.abgeschlossen and offen + beleg.summe_brutto > limit  # Angebote binden nicht
    return render(request, "verkauf/beleg.html", {
        "kredit_warnung": kredit_warnung, "kunde_offen": offen, "kreditlimit": limit,
        "esd_positionen": any(p.artikel_id and p.artikel.esd_key for p in positionen),
        "beleg": beleg, "positionen": positionen, "loeschbar": beleg.loeschhindernis() is None,
        "druckhindernis": druckhindernis(beleg),
        "gruppen": [p for p in positionen if p.typ == "G" and not p.gruppe_id],
        "arten": Belegart.choices,
        "zahlungsbedingungen": Zahlungsbedingung.objects.all(), "versandarten": Versandart.objects.filter(aktiv=True),
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
    neue_versandart = request.POST.get("versandart", "").strip()
    versandart_geaendert = neue_versandart != beleg.versandart and not beleg.gesperrt
    if not versandart_geaendert:
        beleg.versandart = neue_versandart
    beleg.zahlungsbedingung = Zahlungsbedingung.objects.filter(pk=request.POST.get("zahlungsbedingung") or None).first()
    if not beleg.gesperrt:
        beleg.ansprechpartner = Ansprechpartner.objects.filter(
            pk=request.POST.get("ansprechpartner") or None, kunde=beleg.kunde).first()
        beleg.rechnungsanschrift = request.POST.get("rechnungsanschrift", "")
        beleg.versandanschrift = request.POST.get("versandanschrift", "")
        beleg.bemerkung = request.POST.get("bemerkung", "")
        beleg.versandkosten = dezimal(request.POST.get("versandkosten"), Decimal("0"))
        beleg.verpackung = dezimal(request.POST.get("verpackung"), Decimal("0"))
        beleg.versicherung = dezimal(request.POST.get("versicherung"), Decimal("0"))
        if min(beleg.versandkosten, beleg.verpackung, beleg.versicherung) < 0:
            raise ValueError("Versand, Verpackung und Versicherung dürfen nicht negativ sein.")
        beleg.referenz = request.POST.get("referenz", "").strip()[:100]
        beleg.rabatt_prozent = dezimal(request.POST.get("rabatt_prozent"), Decimal("0"))
        if not Decimal("0") <= beleg.rabatt_prozent <= Decimal("100"):
            raise ValueError("Der Belegrabatt muss zwischen 0 und 100 % liegen.")
        beleg.bezugnahme = request.POST.get("bezugnahme", "").strip()[:60]
        beleg.gueltig_bis = datum_oder_none(request.POST.get("gueltig_bis"))
        beleg.liefertermin = datum_oder_none(request.POST.get("liefertermin"))
        beleg.vortext = request.POST.get("vortext", "")
        beleg.schlusstext = request.POST.get("schlusstext", "")
        if versandart_geaendert:  # Stammdaten-Standardkosten überschreiben die eingegebenen Kosten
            beleg.versandart_setzen(neue_versandart)
    beleg.save()
    if not beleg.gesperrt:
        beleg.berechne()
    messages.success(request, "Gespeichert.")
    return redirect("verkauf:beleg", beleg_id=beleg.pk)


def gruppenpreis_erhoehen(gruppe, betrag):
    """Hat die Gruppe einen von Hand festgelegten Preis, erhöht eine später hinzugefügte Unterposition diesen Preis um ihren Betrag
    (ohne festen Preis rechnet berechne() ohnehin die Summe)."""
    if gruppe.preis_fest and gruppe.menge:
        neu = (gruppe.einzelpreis + betrag / gruppe.menge).quantize(Decimal("0.01"))
        Position.objects.filter(pk=gruppe.pk).update(einzelpreis=neu)
        gruppe.einzelpreis = neu


@mit_mandant
@require_POST
@sperr_schutz
def position_neu(request, beleg_id):
    beleg = beleg_holen(request, beleg_id)
    typ = request.POST.get("typ", "A")
    nummer = "" if request.POST.get("pl") else request.POST.get("artikel", "").strip()  # Preislistenartikel kommen über 'pl'
    artikel = Artikel.objects.filter(nummer__iexact=nummer, nicht_aktiv=False).select_related("steuersatz").first() if nummer else None
    if nummer and not artikel:
        raise ValueError(f"Artikel '{nummer}' nicht gefunden.")
    menge = dezimal(request.POST.get("menge"), Decimal("1"))
    rabatt = dezimal(request.POST.get("rabatt"), Decimal("0"))
    if menge <= 0:
        raise ValueError("Die Menge muss größer als 0 sein (Rückgaben laufen über die Gutschrift).")
    if not Decimal("0") <= rabatt <= Decimal("100"):
        raise ValueError("Der Rabatt muss zwischen 0 und 100 % liegen.")
    if request.POST.get("pl"):  # Artikel aus der Lieferantenpreisliste (Typ P): ohne eigene Artikelanlage verkaufbar
        from apps.stammdaten.models import Steuersatz
        from apps.stammdaten.preise import preislistenpreis
        gruppe_pl = None
        if request.POST.get("gruppe"):  # Preislistenartikel als Unterposition einer Gruppe
            gruppe_pl = get_object_or_404(Position, pk=request.POST["gruppe"], beleg=beleg, typ="G", gruppe__isnull=True)
        eintrag = get_object_or_404(Preislistenartikel.objects.select_related("lieferant"), pk=request.POST["pl"])
        preis = dezimal(request.POST.get("preis"))
        if preis is None:
            preis = preislistenpreis(eintrag, beleg.kunde)
        if preis is None:
            raise ValueError("Für Preislistenartikel ist noch kein Aufschlag hinterlegt (Verwaltung → Globale Preisvorgabe) – bitte den Preis eintragen.")
        standard = Steuersatz.objects.filter(nummer=1).first()
        neue_teil = Position.objects.create(
            beleg=beleg, gruppe=gruppe_pl, typ="P", preisliste_artikel=eintrag, bezeichnung=(request.POST.get("bezeichnung", "").strip() or eintrag.bezeichnung)[:255],
            menge=menge, einzelpreis=preis, rabatt_prozent=rabatt, steuer_prozent=standard.prozent if standard else Decimal("19"))
        if gruppe_pl:
            gruppenpreis_erhoehen(gruppe_pl, menge * preis)
        beleg.berechne()
        return redirect("verkauf:beleg", beleg_id=beleg.pk)
    gruppe = None
    if request.POST.get("gruppe"):  # Unterposition einer frei zusammengestellten Gruppe
        gruppe = get_object_or_404(Position, pk=request.POST["gruppe"], beleg=beleg, typ="G", gruppe__isnull=True)
        if not artikel:
            raise ValueError("Unterpositionen brauchen einen Artikel.")
    bezeichnung = request.POST.get("bezeichnung", "").strip() or (artikel.bezeichnung if artikel else "")
    if not bezeichnung:
        raise ValueError("Artikel oder Bezeichnung fehlt.")
    preis = dezimal(request.POST.get("preis"))
    steuer = artikel.steuersatz.prozent if artikel else Decimal("19")

    if artikel and artikel.typ == "G" and not gruppe:  # Gruppenartikel: Komponenten werden als Unterpositionen angelegt
        komponenten = list(artikel.komponenten.select_related("komponente__steuersatz"))
        kopf = Position.objects.create(beleg=beleg, typ="G", artikel=artikel, bezeichnung=bezeichnung, menge=menge,
                                       einzelpreis=preis if preis is not None else Decimal("0"), rabatt_prozent=rabatt,
                                       steuer_prozent=steuer, preis_fest=preis is not None)
        summe = Decimal("0")
        for k in komponenten:
            einzel = vorschlag_preis(k.komponente, beleg.kunde) or Decimal("0")
            Position.objects.create(beleg=beleg, gruppe=kopf, typ=k.komponente.typ, artikel=k.komponente,
                                    bezeichnung=k.komponente.bezeichnung, menge=menge * k.menge, einzelpreis=einzel,
                                    steuer_prozent=k.komponente.steuersatz.prozent)
            summe += einzel * k.menge
        if preis is None:
            kopf.einzelpreis = summe
            kopf.save()
    elif gruppe or typ == "G" and not artikel:  # Unterposition bzw. frei benannte Gruppe
        if gruppe:
            if preis is None:
                preis = vorschlag_preis(artikel, beleg.kunde) or Decimal("0")
            Position.objects.create(beleg=beleg, gruppe=gruppe, typ=artikel.typ, artikel=artikel, bezeichnung=bezeichnung,
                                    menge=menge, einzelpreis=preis, steuer_prozent=steuer)
            gruppenpreis_erhoehen(gruppe, menge * preis)
        else:
            Position.objects.create(beleg=beleg, typ="G", bezeichnung=bezeichnung, menge=menge,
                                    einzelpreis=preis if preis is not None else Decimal("0"), rabatt_prozent=rabatt,
                                    steuer_prozent=steuer, preis_fest=preis is not None)
    else:
        if preis is None:
            preis = vorschlag_preis(artikel, beleg.kunde) if artikel else None
        if preis is None:
            raise ValueError("Preis fehlt.")
        Position.objects.create(
            beleg=beleg, typ=artikel.typ if artikel else typ, artikel=artikel, bezeichnung=bezeichnung,
            menge=menge, einzelpreis=preis, rabatt_prozent=rabatt, steuer_prozent=steuer)
    beleg.berechne()
    return redirect("verkauf:beleg", beleg_id=beleg.pk)


@mit_mandant
@require_POST
@sperr_schutz
def gruppenpreis(request, beleg_id, position_id):
    """Gesamtpreis der Gruppe festlegen; leer = Summe der Unterpositionen (bei Menge der Gruppe pro Stück)."""
    beleg = beleg_holen(request, beleg_id)
    kopf = get_object_or_404(Position, pk=position_id, beleg=beleg, typ="G", gruppe__isnull=True)
    preis = dezimal(request.POST.get("preis"))
    kopf.preis_fest = preis is not None  # leer = wieder die Summe der Unterpositionen (wird in berechne() gesetzt)
    if preis is None:
        summe = sum((u.menge * u.einzelpreis for u in kopf.unterpositionen.all()), Decimal("0"))
        preis = (summe / kopf.menge) if kopf.menge else Decimal("0")
    kopf.einzelpreis = preis.quantize(Decimal("0.01"))
    kopf.save()
    beleg.berechne()
    return redirect("verkauf:beleg", beleg_id=beleg.pk)


@mit_mandant
@require_POST
@sperr_schutz
def position_aendern(request, beleg_id, position_id):
    """Menge, Preis, Rabatt und Zusatztext einer Position ändern (Unterpositionen einer Gruppe: nur Menge und Text)."""
    beleg = beleg_holen(request, beleg_id)
    p = get_object_or_404(Position, pk=position_id, beleg=beleg)
    menge = dezimal(request.POST.get("menge"), p.menge)
    if menge <= 0:
        raise ValueError("Die Menge muss größer als 0 sein.")
    p.menge = menge
    p.zusatztext = request.POST.get("zusatztext", "")
    if not p.gruppe_id:
        neuer_preis = dezimal(request.POST.get("preis"), p.einzelpreis)
        if p.typ == "G" and neuer_preis != p.einzelpreis:
            p.preis_fest = True  # von Hand geänderter Gruppenpreis bleibt stehen
        p.einzelpreis = neuer_preis
        rabatt = dezimal(request.POST.get("rabatt"), Decimal("0"))
        if not 0 <= rabatt <= 100:
            raise ValueError("Der Rabatt muss zwischen 0 und 100 % liegen.")
        p.rabatt_prozent = rabatt
    p.save()
    beleg.berechne()
    return redirect("verkauf:beleg", beleg_id=beleg.pk)


@mit_mandant
@require_POST
@sperr_schutz
def position_loeschen(request, beleg_id, position_id):
    beleg = beleg_holen(request, beleg_id)
    p = get_object_or_404(Position, pk=position_id, beleg=beleg)
    if p.gruppe_id:  # Unterposition einer Gruppe mit festem Preis: der Preis sinkt um ihren Betrag
        gruppenpreis_erhoehen(p.gruppe, -(p.menge * p.einzelpreis))
    p.delete()
    beleg.berechne()
    return redirect("verkauf:beleg", beleg_id=beleg.pk)


@mit_mandant
@require_POST
@sperr_schutz
def beleg_abschliessen(request, beleg_id):
    beleg = beleg_holen(request, beleg_id)
    if beleg.art == Belegart.NACHLIEFERSCHEIN and not beleg.abgeschlossen:
        antwort = request.POST.get("lager")
        if antwort in ("1", "0"):
            beleg.nl_lager_abbuchen = antwort == "1"
            Beleg.objects.filter(pk=beleg.pk).update(nl_lager_abbuchen=beleg.nl_lager_abbuchen)
        elif beleg.nl_lager_abbuchen is None:  # nachfragen: Ersatz für defekte Ware (abbuchen) oder bloße Nachlieferung?
            return render(request, "verkauf/nachlieferung_frage.html", {"beleg": beleg})
    try:
        beleg.abschliessen()
    except BestandReichtNicht as fehler:
        from apps.lager.services import teilung
        beleg = beleg_holen(request, beleg_id)  # Zustand vor dem abgebrochenen Abschluss
        lieferbar, knapp = teilung(beleg)
        if lieferbar and knapp:  # Teillieferung anbieten
            return render(request, "verkauf/teillieferung.html", {
                "beleg": beleg, "lieferbar": lieferbar, "knapp": knapp, "meldung": str(fehler)})
        raise
    messages.success(request, f"{beleg} abgeschlossen.")
    return redirect("verkauf:kunde", kunde_id=beleg.kunde_id)


@mit_mandant
@require_POST
@sperr_schutz
def beleg_folgebeleg(request, beleg_id):
    beleg = beleg_holen(request, beleg_id)
    neu = beleg.folgebeleg_erzeugen()
    return redirect("verkauf:beleg", beleg_id=neu.pk)


@mit_mandant
@require_POST
@sperr_schutz
def beleg_teillieferung(request, beleg_id):
    beleg = beleg_holen(request, beleg_id)
    neu = beleg.teillieferung_abschliessen()
    messages.success(request, f"Teillieferung {neu} abgeschlossen. Die übrigen Positionen bleiben im {beleg.get_art_display()}-Entwurf offen.")
    return redirect("verkauf:kunde", kunde_id=beleg.kunde_id)


@mit_mandant
@require_POST
@sperr_schutz
def beleg_kopieren(request, beleg_id):
    """Beleg als beliebige Belegart kopieren (z. B. Angebot → Rechnung); der Ursprungsbeleg wird auf Wunsch abgeschlossen."""
    beleg = beleg_holen(request, beleg_id)
    neu = beleg.kopieren_als(request.POST.get("art", ""), urbeleg_abschliessen=request.POST.get("urbeleg_abschliessen") == "1")
    messages.success(request, f"{beleg.get_art_display()} als {neu.get_art_display()} kopiert.")
    return redirect("verkauf:beleg", beleg_id=neu.pk)


@mit_mandant
@require_POST
def beleg_loeschen(request, beleg_id):
    beleg = beleg_holen(request, beleg_id)
    grund = beleg.loeschhindernis()
    if grund:
        messages.error(request, grund)
        return redirect("verkauf:beleg", beleg_id=beleg.pk)
    kunde_id, name = beleg.kunde_id, str(beleg)
    try:
        beleg.delete()
    except Exception as fehler:  # z. B. geschützte Verknüpfungen
        messages.error(request, f"{name} kann nicht gelöscht werden: {fehler}")
        return redirect("verkauf:beleg", beleg_id=beleg.pk)
    messages.success(request, f"{name} gelöscht.")
    return redirect("verkauf:kunde", kunde_id=kunde_id)


@mit_mandant
@require_POST
def lieferscheine_abrechnen(request, kunde_id):
    kunde = get_object_or_404(Kunde, pk=kunde_id)
    ids = request.POST.getlist("auswahl")
    try:
        from .models import rechnung_aus_lieferscheinen
        lieferscheine = list(Beleg.objects.filter(pk__in=ids, mandant=request.mandant, kunde=kunde, art=Belegart.LIEFERSCHEIN))
        if len(lieferscheine) != len(set(ids)):
            raise ValueError("Ungültige Auswahl.")
        rechnung = rechnung_aus_lieferscheinen(lieferscheine)
    except ValueError as fehler:
        messages.error(request, str(fehler))
        return redirect("verkauf:kunde", kunde_id=kunde.pk)
    messages.success(request, f"Rechnung als Entwurf aus {len(lieferscheine)} Lieferschein(en) angelegt – prüfen und abschließen.")
    return redirect("verkauf:beleg", beleg_id=rechnung.pk)


@mit_mandant
@require_POST
@sperr_schutz
def beleg_gutschrift(request, beleg_id):
    beleg = beleg_holen(request, beleg_id)
    neu = beleg.gutschrift_erzeugen()
    messages.success(request, "Gutschrift als Entwurf angelegt – prüfen und abschließen.")
    return redirect("verkauf:beleg", beleg_id=neu.pk)


@mit_mandant
def beleg_xrechnung(request, beleg_id):
    """Reine XRechnung (XML, Standard für Behörden) zu einer abgeschlossenen Rechnung/Gutschrift."""
    from .zugferd import ERechnungAusgangFehler, xml_erzeugen
    beleg = beleg_holen(request, beleg_id)
    try:
        xml = xml_erzeugen(beleg, xrechnung=True)
    except ERechnungAusgangFehler as fehler:
        messages.error(request, str(fehler))
        return redirect("verkauf:beleg", beleg_id=beleg.pk)
    antwort = HttpResponse(xml, content_type="application/xml")
    antwort["Content-Disposition"] = f'attachment; filename="{beleg.nummer}_xrechnung.xml"'
    return antwort


@mit_mandant
def beleg_pdf_ansicht(request, beleg_id):
    from apps.lager.services import druckhindernis
    beleg = beleg_holen(request, beleg_id)
    hindernis = druckhindernis(beleg)
    if hindernis:
        messages.error(request, hindernis)
        return redirect("verkauf:beleg", beleg_id=beleg.pk)
    from .zugferd import beleg_pdf_mit_xml
    pdf, hinweis = beleg_pdf_mit_xml(beleg)
    if hinweis:
        messages.warning(request, hinweis)
    antwort = HttpResponse(pdf, content_type="application/pdf")
    antwort["Content-Disposition"] = f'inline; filename="{beleg.nummer or "entwurf"}.pdf"'
    return antwort


@mit_mandant
def beleg_mail(request, beleg_id):
    beleg = beleg_holen(request, beleg_id)
    if beleg.nummer and not beleg.positionen.exists():
        messages.error(request, "Ein Beleg ohne Position kann nicht versendet werden.")
        return redirect("verkauf:beleg", beleg_id=beleg.pk)
    if not beleg.nummer:
        messages.error(request, "Erst abschließen und nummerieren, dann versenden.")
        return redirect("verkauf:beleg", beleg_id=beleg.pk)
    from apps.lager.services import druckhindernis
    hindernis = druckhindernis(beleg)
    if hindernis:
        messages.error(request, hindernis)
        return redirect("verkauf:beleg", beleg_id=beleg.pk)
    an, betreff, text = mail_vorschlag(beleg)
    if request.method != "POST" and beleg.art in ("RE", "GS"):
        from .zugferd import beleg_pdf_mit_xml
        hinweis = beleg_pdf_mit_xml(beleg)[1]
        if hinweis:
            messages.warning(request, hinweis)
    if request.method == "POST":
        an, betreff, text = request.POST.get("an", ""), request.POST.get("betreff", ""), request.POST.get("text", "")
        try:
            versand = beleg_senden(beleg, an, betreff, text, benutzer=request.user)
            messages.success(request, f"{beleg} an {versand.an} versendet.")
            return redirect("verkauf:kunde", kunde_id=beleg.kunde_id)
        except MailFehler as fehler:
            messages.error(request, str(fehler))
    return render(request, "verkauf/mail.html", {
        "beleg": beleg, "an": an, "betreff": betreff, "text": text, "bisher": beleg.mails.all()[:10]})


def vorschlag_preis(artikel, kunde):
    from apps.stammdaten.preise import verkaufspreis
    return verkaufspreis(artikel, kunde)[0]


@mit_mandant
def api_artikel(request):
    """Artikelsuche für die Positionserfassung: Preis nach Preisstufe, letzter Preis des Kunden."""
    q = request.GET.get("q", "").strip()
    kunde = Kunde.objects.filter(pk=request.GET.get("kunde")).first()
    typ = request.GET.get("typ", "A")
    alle = q == "*"  # Stern = alle Artikel des gewählten Typs
    if len(q) < 2 and not alle:
        return JsonResponse({"treffer": []})
    artikel = Artikel.objects.filter(nicht_aktiv=False)
    if typ in ("L", "G"):
        artikel = artikel.filter(typ=typ)
    elif typ == "P":
        artikel = artikel.none()
    else:
        artikel = artikel.filter(typ="A")
    pl = Preislistenartikel.objects.filter(artikel__isnull=True).select_related("lieferant")  # verknüpfte stehen schon als eigener Artikel da
    if typ in ("L", "G"):
        pl = pl.none()
    for wort in ([] if alle else q.split()):  # jedes Wort muss irgendwo vorkommen, Reihenfolge egal
        artikel = artikel.filter(
            Q(nummer__icontains=wort) | Q(bezeichnung__icontains=wort) | Q(herstellernummer__icontains=wort)
            | Q(bezeichnung_erweitert__icontains=wort) | Q(hersteller__name__icontains=wort) | Q(ean__icontains=wort))
        pl = pl.filter(Q(bezeichnung__icontains=wort) | Q(herstellernummer__icontains=wort) | Q(bestellnummer__icontains=wort)
                       | Q(hersteller__icontains=wort) | Q(ean__icontains=wort))
    artikel = artikel.select_related("steuersatz").order_by("nummer")[:(60 if alle else 20)]
    preisliste = list(pl.order_by("bezeichnung")[:(60 if alle else 15)])
    treffer = []
    for a in artikel:
        letzte = None
        if kunde:
            p = Position.objects.filter(artikel=a, beleg__kunde=kunde, beleg__mandant=request.mandant) \
                .select_related("beleg").order_by("-beleg__belegdatum", "-id").first()
            if p:
                letzte = {"preis": str(p.einzelpreis), "datum": p.beleg.belegdatum.strftime("%d.%m.%Y")}
        from apps.stammdaten.preise import verkaufspreis
        preis, quelle = verkaufspreis(a, kunde)
        frei = None
        if a.typ == "A":
            from apps.lager.models import bestand, reserviert
            frei = str(bestand(request.mandant, a) - reserviert(request.mandant, a))
        elif a.typ == "G":
            from apps.lager.models import gruppen_verfuegbar
            g = gruppen_verfuegbar(request.mandant, a)
            frei = str(g) if g is not None else None
        ek = a.ek_errechnet
        treffer.append({"nummer": a.nummer, "bezeichnung": a.bezeichnung, "typ": a.typ, "verfuegbar": frei,
                        "ek": str(ek) if ek is not None else "",
                        "preis": str(preis) if preis is not None else "", "sonderpreis": quelle == "sonderpreis", "letzter": letzte})
    from apps.stammdaten.preise import preislistenpreis
    for e in preisliste:  # Lieferantenartikel (Typ P) mit Kurzname des Lieferanten
        preis = preislistenpreis(e, kunde)
        treffer.append({"nummer": e.nummer, "bezeichnung": e.bezeichnung, "typ": "P", "verfuegbar": None, "sonderpreis": False,
                        "preis": str(preis) if preis is not None else "", "letzter": None, "pl": e.pk, "ek": str(e.ek), "lieferant": e.lieferant.kurz,
                        "lieferant_bestand": e.bestand})
    return JsonResponse({"treffer": treffer})


@mit_mandant
def abos(request):
    """Wiederkehrende Rechnungen des Mandanten."""
    liste = Abo.objects.filter(mandant=request.mandant).select_related("kunde").prefetch_related("positionen")
    return render(request, "verkauf/abos.html", {"abos": liste})


@mit_mandant
@require_POST
@sperr_schutz
def abo_aus_rechnung(request, beleg_id):
    """Aus einer Rechnung einen wiederkehrenden Auftrag machen: Positionen werden übernommen, die erste Folgerechnung ist am Starttermin."""
    from datetime import date
    beleg = beleg_holen(request, beleg_id)
    if beleg.art != Belegart.RECHNUNG:
        raise ValueError("Wiederkehrend einrichten geht nur aus einer Rechnung.")
    try:
        monate = int(request.POST.get("monate", "1"))
    except ValueError:
        monate = 0
    if monate not in dict(Abo.INTERVALLE):
        raise ValueError("Bitte einen gültigen Rhythmus wählen (monatlich, vierteljährlich, halbjährlich oder jährlich).")
    try:
        start = date.fromisoformat(request.POST.get("start", ""))
    except ValueError:
        raise ValueError("Bitte ein gültiges Startdatum angeben.")
    abo = Abo.objects.create(
        mandant=beleg.mandant, kunde=beleg.kunde, bezeichnung=(request.POST.get("bezeichnung") or f"nach {beleg.nummer or 'Rechnung'}")[:100],
        monate=monate, naechster_termin=start, starttag=start.day, zahlungsbedingung=beleg.zahlungsbedingung,
        rabatt_prozent=beleg.rabatt_prozent, versandkosten=beleg.versandkosten, verpackung=beleg.verpackung,
        versicherung=beleg.versicherung, vortext=beleg.vortext, schlusstext=beleg.schlusstext)
    for p in beleg.positionen.filter(gruppe__isnull=True).order_by("pos", "id"):
        AboPosition.objects.create(abo=abo, artikel=p.artikel, bezeichnung=p.bezeichnung, menge=p.menge, einzelpreis=p.einzelpreis,
                                   rabatt_prozent=p.rabatt_prozent, steuer_prozent=p.steuer_prozent)
    messages.success(request, f"Wiederkehrende Rechnung „{abo.bezeichnung}“ angelegt: nächste Rechnung am {abo.naechster_termin:%d.%m.%Y}.")
    return redirect("verkauf:abos")


@mit_mandant
@require_POST
def abo_aktion(request, abo_id):
    abo = get_object_or_404(Abo, pk=abo_id, mandant=request.mandant)
    aktion = request.POST.get("aktion")
    if aktion == "jetzt":
        try:
            beleg = abo.rechnung_erzeugen()
        except ValueError as fehler:
            messages.error(request, str(fehler))
            return redirect("verkauf:abos")
        messages.success(request, f"Rechnungsentwurf für {abo.kunde.name1} angelegt.")
        return redirect("verkauf:beleg", beleg_id=beleg.pk)
    if aktion == "pause":
        abo.aktiv = not abo.aktiv
        abo.save(update_fields=["aktiv"])
    elif aktion == "loeschen":
        abo.delete()
        messages.success(request, "Wiederkehrende Rechnung gelöscht (bereits erzeugte Rechnungen bleiben).")
    return redirect("verkauf:abos")


@mit_mandant
def beleg_versand(request, beleg_id):
    """Versand zu einem Beleg: Sendungsnummer erfassen, Paketaufkleber drucken, Versand-Mail an den Kunden."""
    from . import versand as vs
    beleg = beleg_holen(request, beleg_id)
    if request.method == "POST":
        aktion = request.POST.get("aktion")
        try:
            gewicht_roh = dezimal(request.POST.get("gewicht"), None)
        except ValueError as fehler:
            messages.error(request, str(fehler))
            return redirect("verkauf:beleg_versand", beleg_id=beleg.pk)
        produkt = request.POST.get("produkt")
        if produkt not in dict(Versand.PRODUKTE):
            produkt = Versand.PAKET  # nichts raten: ohne Angabe DHL Paket
        if gewicht_roh is not None and gewicht_roh <= 0:
            messages.error(request, "Das Gewicht muss größer als 0 sein.")
            return redirect("verkauf:beleg_versand", beleg_id=beleg.pk)
        if aktion == "neu":
            gewicht = gewicht_roh
            Versand.objects.create(beleg=beleg, sendungsnummer=request.POST.get("sendungsnummer", "").replace(" ", "")[:40],
                                   gewicht_kg=gewicht, produkt=produkt)
            messages.success(request, "Versand gespeichert.")
        elif aktion == "dhl":
            from . import dhl
            gewicht = gewicht_roh if gewicht_roh is not None else beleg.artikelgewicht()[0] or None
            v = Versand.objects.create(beleg=beleg, gewicht_kg=gewicht, produkt=produkt)
            try:
                dhl.label_erzeugen(v)
                messages.success(request, f"DHL-Label erzeugt, Sendungsnummer {v.sendungsnummer}.")
            except dhl.DhlFehler as fehler:
                v.delete()
                messages.error(request, str(fehler))
        else:
            v = get_object_or_404(Versand, pk=request.POST.get("versand"), beleg=beleg)
            if aktion == "mail":
                an = request.POST.get("an", "")
                try:
                    vs.versandmail_senden(v, an, request.user)
                    messages.success(request, f"Versand-Mail an {an} gesendet.")
                except MailFehler as fehler:
                    messages.error(request, str(fehler))
            elif aktion == "loeschen":
                if v.dhl_erzeugt:
                    from . import dhl
                    try:
                        dhl.stornieren(v)
                    except dhl.DhlFehler as fehler:
                        messages.error(request, str(fehler))
                        return redirect("verkauf:beleg_versand", beleg_id=beleg.pk)
                v.delete()
        return redirect("verkauf:beleg_versand", beleg_id=beleg.pk)
    ap = beleg.ansprechpartner
    from . import dhl
    gewicht, ohne_gewicht = beleg.artikelgewicht()
    try:
        inland = dhl.anschrift_zerlegen(beleg.versandanschrift or beleg.rechnungsanschrift)["country"] == "DEU"
    except dhl.DhlFehler:
        inland = True  # unvollständige Anschrift: meldet DHL beim Erzeugen
    return render(request, "verkauf/versand.html", {
        "beleg": beleg, "versendungen": beleg.versendungen.all(),
        "mailadresse": (ap.email if ap and ap.email else beleg.kunde.email) or "",
        "gewicht": gewicht if gewicht > 0 else None, "ohne_gewicht": ohne_gewicht, "inland": inland,
        "produkte": Versand.PRODUKTE, "produkt": Versand.produkt_vorschlag(gewicht, inland, not ohne_gewicht)})


@mit_mandant
def versand_aufkleber(request, beleg_id, versand_id):
    from . import versand as vs
    beleg = beleg_holen(request, beleg_id)
    v = get_object_or_404(Versand, pk=versand_id, beleg=beleg)
    pdf = bytes(v.label) if v.label else vs.aufkleber_pdf(v)  # amtliches DHL-Label, sonst interner Aufkleber
    antwort = HttpResponse(pdf, content_type="application/pdf")
    antwort["Content-Disposition"] = f'inline; filename="paketaufkleber_{beleg.nummer or beleg.pk}.pdf"'
    return antwort

@mit_mandant
def belege(request):
    """Alle Belege des Mandanten, kundenübergreifend: Suche, Belegart, Status, Zeitraum."""
    from datetime import date

    from django.core.paginator import Paginator
    liste = Beleg.objects.filter(mandant=request.mandant, positionen__isnull=False).distinct().select_related("kunde")
    q = request.GET.get("q", "").strip()
    for wort in q.split():
        liste = liste.filter(Q(nummer__icontains=wort) | Q(kunde__name1__icontains=wort) | Q(kunde__nummer__icontains=wort)
                             | Q(referenz__icontains=wort))
    art = request.GET.get("art", "")
    if art in Belegart.values:
        liste = liste.filter(art=art)
    status = request.GET.get("status", "")
    if status == "offen":
        liste = liste.filter(abgeschlossen=False)
    elif status == "abgeschlossen":
        liste = liste.filter(abgeschlossen=True)
    zeitraum = {}
    for feld, bedingung in (("von", "belegdatum__gte"), ("bis", "belegdatum__lte")):
        wert = request.GET.get(feld, "")
        try:
            if wert:
                liste = liste.filter(**{bedingung: date.fromisoformat(wert)})
        except ValueError:
            wert = ""
        zeitraum[feld] = wert
    parameter = request.GET.copy()
    parameter.pop("seite", None)
    seite = Paginator(liste.order_by("-belegdatum", "-id"), 50).get_page(request.GET.get("seite"))
    return render(request, "verkauf/belege.html", {
        "seite": seite, "q": q, "art": art, "status": status, "von": zeitraum["von"], "bis": zeitraum["bis"],
        "arten": Belegart.choices, "parameter": parameter.urlencode(), "anzahl": seite.paginator.count})

@mit_mandant
@require_POST
def wiedervorlage_neu(request, kunde_id):
    kunde = get_object_or_404(Kunde, pk=kunde_id)
    try:
        faellig = datum_oder_none(request.POST.get("faellig"))
        text = request.POST.get("text", "").strip()[:300]
        if not faellig or not text:
            raise ValueError("Bitte Datum und Notiz angeben.")
        beleg = Beleg.objects.filter(pk=request.POST.get("beleg") or None, mandant=request.mandant, kunde=kunde).first()
        Wiedervorlage.objects.create(mandant=request.mandant, kunde=kunde, beleg=beleg, faellig=faellig, text=text)
        messages.success(request, "Wiedervorlage gespeichert.")
    except ValueError as fehler:
        messages.error(request, str(fehler))
    return redirect("verkauf:kunde", kunde_id=kunde.pk)


@mit_mandant
@require_POST
def wiedervorlage_erledigt(request, wiedervorlage_id):
    from django.utils import timezone
    w = get_object_or_404(Wiedervorlage, pk=wiedervorlage_id, mandant=request.mandant)
    w.erledigt, w.erledigt_am = True, timezone.localdate()
    w.save()
    return redirect("verkauf:start")
