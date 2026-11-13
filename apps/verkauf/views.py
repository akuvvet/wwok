from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import OperationalError, transaction
from django.db.models import Q
from django.http import Http404, HttpResponse, JsonResponse
from django.utils.http import url_has_allowed_host_and_scheme, urlencode
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.views.decorators.http import require_POST

from apps.kern.kontext import MANDANT_COOKIE, mandant_oder_none
from apps.kern.models import Mandant
from apps.stammdaten.models import (Ansprechpartner, Anschriftart, Artikel, Kunde, Preislistenartikel, Zahlungsbedingung)

from apps.lager.models import BestandReichtNicht
from .models import (runden, zahlungsbedingung_vorkasse, Abo, AboPosition, Beleg, BelegGesperrt, Belegart, Briefprodukt, Mengenregel, Position, Ruecknahme, Versand, Versandart,
                     Vorgangsweg, Wiedervorlage, letzte_preise, regel_vorschlag)
from . import vorgang
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


def mit_mandant(view):
    """Login nötig und ein Mandant gewählt; sonst zur Startseite."""
    @login_required
    def umhuellt(request, *args, **kwargs):
        mandant = mandant_oder_none(request)
        if mandant is None:
            messages.info(request, "Bitte zuerst einen Mandanten auswählen: Firma prüfen und auf „Auswählen“ klicken.")
            return redirect("verkauf:start")
        request.mandant = mandant
        return view(request, *args, **kwargs)
    umhuellt.__name__ = view.__name__
    return umhuellt


def beleg_holen(request, beleg_id):
    return get_object_or_404(Beleg.objects.select_related("kunde", "mandant"), pk=beleg_id, mandant=request.mandant)


KUNDENSUCHE_FELDER = ["nummer", "name1", "name2", "name3", "ort", "ansprechpartner__vorname", "ansprechpartner__nachname"]


def kunden_suchen(q):
    """Kundensuche wie auf der Startseite: jedes Wort in einem Namen, einem Ansprechpartner, der Nummer oder dem Ort;
    Reihenfolge egal, „*“ allein = alle, Stern am Ende = Wortanfang."""
    filt = Q()
    if q != "*":
        vergleich = "istartswith" if q.endswith("*") else "icontains"
        for wort in q.rstrip("*").split():
            filt &= Q(*[Q(**{f"{feld}__{vergleich}": wort}) for feld in KUNDENSUCHE_FELDER], _connector=Q.OR)
    return Kunde.objects.filter(filt, anonymisiert=False).distinct()


@login_required
def start(request):
    if request.method == "POST":
        mandant = get_object_or_404(Mandant, pk=request.POST.get("mandant"), aktiv=True)
        request.session["mandant_id"] = mandant.pk
        weiter = request.POST.get("weiter", "")  # nach dem Wechsel zurück auf die Seite, von der aus gewechselt wurde
        if not weiter or not url_has_allowed_host_and_scheme(weiter, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
            weiter = "dashboard" if request.POST.get("weiter") is not None else "verkauf:start"
        antwort = redirect(weiter)
        antwort.set_cookie(MANDANT_COOKIE, mandant.pk, max_age=365 * 24 * 3600, httponly=True, samesite="Lax",
                           secure=settings.SESSION_COOKIE_SECURE)  # auch nach neuer Anmeldung derselbe Mandant
        return antwort
    mandant = mandant_oder_none(request)
    if mandant:
        Beleg.leere_entwuerfe_entfernen(mandant)
        Beleg.vorlaeufige_verwerfen(mandant)  # verlassene nicht gespeicherte Belege werden verworfen
    q = request.GET.get("q", "").strip()
    treffer = None
    if mandant and q:
        treffer = kunden_suchen(q)[:100]
        if len(treffer) == 1:
            return redirect("verkauf:kunde", kunde_id=treffer[0].pk)
    offene = vorlaeufige = None
    if mandant and treffer is None:  # ohne Suche: die offenen (nicht abgeschlossenen) Belege zum Weiterbearbeiten
        offen_basis = Beleg.objects.filter(mandant=mandant, abgeschlossen=False, positionen__isnull=False).distinct().select_related("kunde")
        offene = Beleg.versand_markieren(offen_basis.filter(vorlaeufig=False).order_by("-belegdatum", "-id")[:100])
        vorlaeufige = Beleg.versand_markieren(offen_basis.filter(vorlaeufig=True).order_by("-belegdatum", "-id")[:100])  # „nicht gespeichert“, getrennt von den Altentwürfen
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
        "offene": offene, "vorlaeufige": vorlaeufige})


def _berechnete_lieferscheine(ids) -> set:
    """IDs der Lieferscheine aus `ids`, die schon in eine Rechnung übernommen wurden (zwei Abfragen für beliebig viele Belege)."""
    ids = list(ids)
    if not ids:
        return set()
    direkt = set(Beleg.objects.filter(vorgaenger_id__in=ids, art=Belegart.RECHNUNG).values_list("vorgaenger_id", flat=True))
    sammel = set(Beleg.lieferscheine.through.objects.filter(to_beleg_id__in=ids).values_list("to_beleg_id", flat=True))
    return direkt | sammel


@mit_mandant
def kunde_detail(request, kunde_id):
    from datetime import date

    from django.core.paginator import Paginator

    from apps.finanzen import services as fin
    kunde = get_object_or_404(Kunde, pk=kunde_id)
    Beleg.leere_entwuerfe_entfernen(request.mandant, kunde)
    Beleg.vorlaeufige_verwerfen(request.mandant)
    belege = Beleg.objects.filter(Q(positionen__isnull=False) | Q(uebernahmen_aus__isnull=False), kunde=kunde, mandant=request.mandant).distinct()         .select_related("kunde", "zahlungsbedingung", "vorgaenger").prefetch_related("umwandlungen")  # Belege ohne Position gelten nicht als gespeichert – außer einer Bestellung, deren Menge vollständig in Folgebelege wanderte (erledigt, Verlauf)
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
    if ohne_storno:  # erledigte Gutschriften und ihre voll gutgeschriebenen Rechnungen ausblenden; Offenes bleibt sichtbar
        erledigt = [g for g in Beleg.objects.filter(kunde=kunde, mandant=request.mandant, art=Belegart.GUTSCHRIFT,
                                                    abgeschlossen=True).select_related("vorgaenger")
                    if abs(fin.gutschrift_rest(g)) < fin.CENT]
        storniert = [g.vorgaenger_id for g in erledigt if g.vorgaenger_id and g.summe_brutto >= g.vorgaenger.summe_brutto
                     and abs(fin.offen(g.vorgaenger)) < fin.CENT]
        belege = belege.exclude(pk__in=[g.pk for g in erledigt] + storniert).exclude(art=Belegart.GUTSCHRIFT, abgeschlossen=False)
    parameter = request.GET.copy()
    parameter.pop("seite", None)
    seite = Paginator(belege, 25).get_page(request.GET.get("seite"))
    zeilen = []
    seite.object_list = Beleg.versand_markieren(seite.object_list)
    # Summen, Positionen und Abrechenbarkeit für die ganze Seite gesammelt (keine Abfragen je Zeile)
    summen = fin.summen_je_beleg(seite.object_list)
    positionen = {}
    for p in Position.objects.filter(beleg__in=seite.object_list).select_related("artikel").order_by("beleg_id", "pk"):
        positionen.setdefault(p.beleg_id, []).append(p)
    ls_ids = [b.pk for b in seite.object_list if b.art == Belegart.LIEFERSCHEIN and b.abgeschlossen]
    berechnet = _berechnete_lieferscheine(ls_ids)
    from apps.finanzen.anzahlung import status_je_proforma
    pf_status = status_je_proforma(seite.object_list)
    null = Decimal("0")
    for b in seite.object_list:
        rechnung = b.art == Belegart.RECHNUNG and b.abgeschlossen
        gutschrift = b.art == Belegart.GUTSCHRIFT and b.abgeschlossen
        erledigt = (summen["bezahlt"].get(b.pk, null) + summen["gutgeschrieben"].get(b.pk, null) + summen["angerechnet"].get(b.pk, null)
                    - (summen["abgegeben"].get(b.pk, null) if rechnung else null))  # weiterverrechnete Überzahlung
        status = pf_status.get(b.pk)
        proforma = status is not None and status["code"] != "entwurf"
        re_status = None
        if rechnung:  # Zahlstatus der Rechnung als Symbol (Issue 62)
            rest = b.summe_brutto - erledigt
            re_status = "bezahlt" if rest == 0 else "ueberzahlt" if rest < 0 else "teilbezahlt" if erledigt > 0 else "offen"
        zeilen.append({
            "beleg": b, "faellig": fin.faellig_am(b) if rechnung else None, "zahlstatus": status, "re_status": re_status,
            "bezahlt": erledigt if rechnung else status["bezahlt"] if proforma else None,
            "offen": b.summe_brutto - erledigt if rechnung else status["offen"] if proforma else
                     -(b.summe_brutto - summen["verrechnet"].get(b.pk, null)) if gutschrift else None,
            "abrechenbar": b.pk in ls_ids and b.pk not in berechnet,
            "bezug": b.vorgaenger if b.vorgaenger_id and b.vorgaenger.nummer else None,
            "vormals": b.vormals,
            "positionen": positionen.get(b.pk, [])[:15]})
    lieferscheine = list(Beleg.objects.filter(kunde=kunde, mandant=request.mandant, art=Belegart.LIEFERSCHEIN,
                                              abgeschlossen=True).values_list("pk", flat=True))
    offene_lieferscheine = len(lieferscheine) - len(_berechnete_lieferscheine(lieferscheine))
    mandantendaten = kunde.mandantendaten.filter(mandant=request.mandant).first()
    offen = fin.kunde_offen(request.mandant, kunde)
    limit = mandantendaten.kreditlimit if mandantendaten else None
    return render(request, "verkauf/kunde.html", {
        "offen": offen, "limit_ueberschritten": bool(limit) and offen > limit,
        "kredit_differenz": offen - limit if limit and offen > limit else None, "sammelrechnung": offene_lieferscheine >= 2,
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
    if art == Belegart.GUTSCHRIFT:
        messages.error(request, "Eine Gutschrift entsteht nur aus einer abgeschlossenen Rechnung: dort unter „Mehr“ → „Storno / Gutschrift“.")
        return redirect("verkauf:kunde", kunde_id=kunde.pk)
    if art == Belegart.LIEFERSCHEIN:
        messages.error(request, "Ein Lieferschein entsteht aus einer Bestellung: erst die Bestellung erfassen, "
                                "dann dort „Als Lieferschein übernehmen“.")
        return redirect("verkauf:kunde", kunde_id=kunde.pk)
    if art == Belegart.PROFORMA:
        messages.error(request, "Eine Proforma-Rechnung entsteht aus einer Bestellung: dort unter „Mehr“ → „Kopieren als …“ → „Proforma-Rechnung“.")
        return redirect("verkauf:kunde", kunde_id=kunde.pk)
    if art == Belegart.PROFORMA_STORNO:
        messages.error(request, "Ein Storno entsteht nur aus einer Proforma-Rechnung.")
        return redirect("verkauf:kunde", kunde_id=kunde.pk)
    with transaction.atomic():  # vorläufig: keine Nummer, bis der Beleg mit „Speichern: abschließen & Nummer vergeben“ gespeichert wird
        beleg = Beleg.objects.create(
            mandant=request.mandant, art=art, kunde=kunde, vorlaeufig=True,
            ansprechpartner=kunde.ansprechpartner.filter(hauptkontakt=True).first(),
            zahlungsbedingung=kunde.zahlungsbedingung,
            rechnungsanschrift=adresse_text(kunde, Anschriftart.RECHNUNG),
            versandanschrift=adresse_text(kunde, Anschriftart.VERSAND), bearbeiter=request.user)
        if kunde.standard_versandart_id and kunde.standard_versandart.aktiv:  # Standard-Versandart des Kunden samt Kosten
            try:
                beleg.versandart_setzen(kunde.standard_versandart.name)
                beleg.save(update_fields=["versandart", "versandkosten", "verpackung", "versicherung"])
            except ValueError as fehler:  # z. B. „nur Inland“ bei Auslandskunden: Beleg entsteht ohne Versandart
                messages.warning(request, f"{fehler} Bitte am Beleg eine andere Versandart wählen.")
        vorgang.vorgang_beginnen(beleg, Vorgangsweg.NEU)
    return redirect("verkauf:beleg", beleg_id=beleg.pk)


@mit_mandant
def beleg_detail(request, beleg_id):
    from apps.lager.services import druckhindernis, retoure_offen
    from apps.lager.models import bestand, reserviert
    beleg = beleg_holen(request, beleg_id)
    beleg.arbeitsstand_beenden()  # Altdaten: ein früher offener Arbeitsstand wird still beendet, der aktuelle Stand gilt
    vorgang.aktiv_melden(beleg)  # vorläufiger Beleg: Aufruf zählt als Aktivität
    positionen = list(beleg.positionen.select_related("artikel", "preisliste_artikel__lieferant"))
    letzte = {} if beleg.art == Belegart.GUTSCHRIFT else letzte_preise(
        beleg.mandant, beleg.kunde, [p.artikel_id for p in positionen], [p.preisliste_artikel_id for p in positionen], ohne_beleg=beleg.pk)
    for p in positionen:  # Warnung, wenn der verfügbare Bestand nicht reicht
        p.letzter = None if p.gruppe_id else letzte.get(("a", p.artikel_id) if p.artikel_id else ("p", p.preisliste_artikel_id))
        p.knapp = None
        if p.artikel_id and p.artikel.typ == "A" and not beleg.abgeschlossen and beleg.art in ("AB", "LS", "RE"):
            frei = bestand(beleg.mandant, p.artikel) - reserviert(beleg.mandant, p.artikel)
            if beleg.art == "AB":
                frei += p.offen_menge  # die eigene Reservierung nicht doppelt zählen
            elif beleg.vorgaenger_id and beleg.vorgaenger.art == "AB":  # der Rückstand der Bestellung blockiert ihre Teillieferung nicht
                frei += sum((q.offen_menge for q in beleg.vorgaenger.positionen.filter(artikel=p.artikel)), Decimal("0"))
            if frei < (p.offen_menge if beleg.art == "AB" else p.menge):
                p.knapp = frei
        if beleg.art == "AB" and p.pk and not p.gruppe_id:  # Altdaten (Kopie): „5 von 10 geliefert, 5 offen“
            p.geliefert = p.uebernommen
            p.rueckstand = p.menge - p.geliefert if p.geliefert > 0 else None
    from . import uebernahme
    verlauf_ab = uebernahme.verlauf_aus(beleg) if beleg.art == Belegart.AUFTRAG else []
    verlauf_ziel = uebernahme.verlauf_in(beleg) if beleg.art in uebernahme.FOLGEARTEN else []
    if verlauf_ab:  # „7 übernommen in Rechnung …, 3 offen“ an der Bestellposition
        texte = uebernahme.rueckstand_text(beleg, [p for p in positionen if p.pk and not p.gruppe_id])
        for p in positionen:
            p.uebernahme_text = texte.get(p.pk)
    from apps.finanzen.services import kunde_offen
    md = beleg.kunde.mandantendaten.filter(mandant=beleg.mandant).first()
    limit = md.kreditlimit if md else None
    offen = kunde_offen(beleg.mandant, beleg.kunde)
    kredit_warnung = bool(limit) and beleg.art not in ("AN", "PF", "SP") and not beleg.abgeschlossen and offen + beleg.summe_brutto > limit  # Angebote binden nicht
    kredit_differenz = (offen + beleg.summe_brutto - limit) if kredit_warnung else None
    from apps.finanzen import anzahlung
    pf_status = anzahlung.proforma_status(beleg) if beleg.art == Belegart.PROFORMA else None
    lieferantenbestellungen = []
    if beleg.art in (Belegart.ANGEBOT, Belegart.AUFTRAG):  # Historie der Lieferantenkopien (Issue 61)
        from apps.einkauf import services as einkauf
        lieferantenbestellungen = list(einkauf.lieferantenbestellungen(beleg))
        if lieferantenbestellungen:
            bestellt = einkauf.bestellt_je_position(beleg)
            for p in positionen:
                p.bestellt = bestellt.get(p.pk)
    return render(request, "verkauf/beleg.html", {
        "pf_status": pf_status, "pf_storno": beleg.proforma_storno, "pf_rechnung": beleg.proforma_rechnung,
        "verlauf_ab": verlauf_ab, "verlauf_ziel": verlauf_ziel,
        "lieferantenbestellungen": lieferantenbestellungen,
        "anzahlungen": list(beleg.anzahlungen.select_related("quelle")) if beleg.art == Belegart.RECHNUNG else [],
        "kredit_warnung": kredit_warnung, "kredit_differenz": kredit_differenz,"kunde_offen": offen, "kreditlimit": limit,
        "esd_positionen": any(p.artikel_id and p.artikel.esd_key for p in positionen),
        "hat_lagerartikel": any(p.artikel_id and p.artikel.typ == "A" for p in positionen), "retoure_offen": retoure_offen(beleg),
        "beleg": beleg, "positionen": positionen, "loeschbar": beleg.loeschhindernis() is None or beleg.verwerfen_moeglich,
        "verwerfen_moeglich": beleg.verwerfen_moeglich,
        "druckhindernis": druckhindernis(beleg),
        "versendungen": list(beleg.versendungen.select_related("briefprodukt")),
        "gruppen": [p for p in positionen if p.typ == "G" and not p.gruppe_id],
        # Angebot: nach einem G-Artikel landen die folgenden Artikel standardmäßig in der zuletzt angelegten Gruppe
        "gruppe_vorgabe": max((p.pk for p in positionen if p.typ == "G" and not p.gruppe_id), default=None)
        if beleg.art == Belegart.ANGEBOT else None,
        "arten": Belegart.choices,
        "zahlungsbedingungen": Zahlungsbedingung.objects.all(), "versandarten": Versandart.objects.filter(aktiv=True),
        "ansprechpartner": beleg.kunde.ansprechpartner.all()})


VERKLEMMT = "Der Beleg wird gerade von einer anderen Anfrage bearbeitet (zweiter Tab oder Benutzer). Es wurde nichts geändert – bitte erneut versuchen."


def sperr_schutz(view):
    """Meldung statt Fehlerseite bei gesperrten Belegen und Fachregel-Verstößen; jede Anfrage auf einen vorläufigen Beleg
    (Positionen, Kopf, Abschluss …) zählt als Aktivität."""
    def umhuellt(request, *args, **kwargs):
        try:
            return view(request, *args, **kwargs)
        except BelegGesperrt as fehler:
            messages.error(request, str(fehler))
        except ValueError as fehler:
            messages.error(request, str(fehler))
        except OperationalError:  # z. B. Deadlock, weil gleichzeitig verworfen/abgeschlossen wurde: die Transaktion ist zurückgerollt
            messages.error(request, VERKLEMMT)
        finally:
            Beleg.objects.filter(pk=kwargs["beleg_id"], mandant=request.mandant, vorlaeufig=True).update(zuletzt_aktiv=timezone.now())
        return redirect("verkauf:beleg", beleg_id=kwargs["beleg_id"])
    umhuellt.__name__ = view.__name__
    return umhuellt


@mit_mandant
@require_POST
@sperr_schutz
def beleg_kopf(request, beleg_id):
    beleg = beleg_holen(request, beleg_id)
    beleg.arbeitsstand_beenden()  # Altdaten: ein früher offener Arbeitsstand wird still beendet
    neue_versandart = request.POST.get("versandart", "").strip()
    versandart_geaendert = neue_versandart != beleg.versandart and not beleg.gesperrt
    if not versandart_geaendert:
        beleg.versandart = neue_versandart
    if beleg.art == Belegart.PROFORMA:  # immer Vorkasse, egal was gewählt wurde
        beleg.zahlungsbedingung = zahlungsbedingung_vorkasse()
    elif beleg.art != Belegart.PROFORMA_STORNO:
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
    if gruppe.beleg.positionen_gesperrt:  # update() umgeht die Sperre in Position.save()
        raise BelegGesperrt(f"{gruppe.beleg} ist abgeschlossen – Positionen lassen sich nicht mehr ändern.")
    if gruppe.preis_fest and gruppe.menge:
        neu = (gruppe.einzelpreis + betrag / gruppe.menge).quantize(Decimal("0.01"))
        Position.objects.filter(pk=gruppe.pk).update(einzelpreis=neu)
        gruppe.einzelpreis = neu


def steuer_passt_zur_gruppe(steuer, gruppe):
    """Versteuert wird nur der Gruppenkopf; eine Unterposition mit anderem Satz ergäbe eine falsche Umsatzsteuer."""
    if steuer != gruppe.steuer_prozent:
        raise ValueError("Position und Gruppe haben unterschiedliche Steuersätze – die Position gehört nicht in diese Gruppe.")


@transaction.atomic
def gruppe_wechseln(p, ziel, alter_betrag):
    """Position nachträglich in eine Gruppe verschieben, in eine andere oder heraus; ziel=None = eigene Position. Der Belegwert
    bleibt gleich: Ein Positionsrabatt geht in den Preis ein (Unterpositionen haben keinen Rabatt). Wo das nicht
    aufgeht, wird der Wechsel abgelehnt, statt still Beträge oder die Steuer zu ändern."""
    alt = p.gruppe
    if ziel:
        if p.verschoben and not alt:  # Rückbuchen erfasst nur Hauptpositionen: sonst ginge die Menge der Bestellung verloren
            raise ValueError("Die Position stammt aus der Bestellung (übernommene Menge) und lässt sich nicht in eine Gruppe verschieben.")
        if not (p.artikel_id or p.preisliste_artikel_id):
            raise ValueError("Unterpositionen brauchen einen Artikel.")
        steuer_passt_zur_gruppe(p.steuer_prozent, ziel)
        if p.lieferschein_id != ziel.lieferschein_id:  # der Vermerk zum Lieferschein erscheint nur an der Gruppe
            raise ValueError("Position und Gruppe stammen aus unterschiedlichen Lieferscheinen – die Position lässt sich nicht verschieben.")
    if (alt and alt.rabatt_prozent) or (ziel and ziel.rabatt_prozent):
        raise ValueError("Die Gruppe hat einen Rabatt – bitte den Rabatt zuerst entfernen, sonst ändert sich der Belegwert.")
    if alt and alt.preis_fest and alt.menge and alt.einzelpreis - alter_betrag / alt.menge < 0:
        raise ValueError("Der Gruppenpreis würde negativ – bitte zuerst den Gruppenpreis anpassen.")
    preis = p.einzelpreis
    if not alt:
        faktor = (Decimal("100") - p.rabatt_prozent) / 100
        preis = runden(p.einzelpreis * faktor)
        if runden(p.menge * preis) != runden(p.menge * p.einzelpreis * faktor):
            raise ValueError("Der Rabatt geht nicht auf den Cent genau im Preis auf – bitte den Preis von Hand setzen und den Rabatt entfernen.")
    if alt:
        gruppenpreis_erhoehen(alt, -alter_betrag)
    if ziel:
        gruppenpreis_erhoehen(ziel, p.menge * preis)
    p.einzelpreis, p.rabatt_prozent = preis, Decimal("0")
    p.gruppe = ziel
    p.save()
    if alt and not alt.preis_fest and not alt.unterpositionen.exists():  # ohne Unterpositionen gibt es keine Summe mehr
        Position.objects.filter(pk=alt.pk).update(einzelpreis=0)


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
    gruppe_id = request.POST.get("gruppe", "")
    vorbelegt = request.POST.get("gruppe_auto") == "1"  # Angebot: Gruppe nur vorausgewählt, nicht ausdrücklich gewählt
    if vorbelegt and (
            typ == "G" or not request.POST.get("pl") and (artikel is None or artikel.typ == "G")):
        gruppe_id = ""  # nur vorbelegt, nicht gewählt: ein neuer Gruppenkopf oder eine freie Textposition bleibt ohne Gruppe
    menge = dezimal(request.POST.get("menge"), Decimal("1"))
    rabatt = dezimal(request.POST.get("rabatt"), Decimal("0"))
    if menge <= 0:
        raise ValueError("Die Menge muss größer als 0 sein (Rückgaben laufen über die Gutschrift).")
    if not Decimal("0") <= rabatt <= Decimal("100"):
        raise ValueError("Der Rabatt muss zwischen 0 und 100 % liegen.")

    def anderer_steuersatz(steuer, gruppe):  # vorbelegte Gruppe mit anderem Satz: Position ohne Gruppe anlegen statt Fehler
        if vorbelegt and steuer != gruppe.steuer_prozent:
            messages.info(request, f"Nicht in die Gruppe „{gruppe.bezeichnung}“ übernommen: anderer Steuersatz.")
            return True
        return False

    def kein_rabatt():  # Unterpositionen gehen mit Menge × Preis in den Gruppenpreis ein; ein Rabatt würde verloren gehen
        if rabatt:
            raise ValueError("Positionen in einer Gruppe haben keinen eigenen Rabatt – Preis anpassen, "
                             "Rabatt am Gruppenkopf eintragen oder die Gruppe für diese Position abwählen.")
    if request.POST.get("pl"):  # Artikel aus der Lieferantenpreisliste (Typ P): ohne eigene Artikelanlage verkaufbar
        from apps.stammdaten.models import Steuersatz
        from apps.stammdaten.preise import preislistenpreis
        gruppe_pl = None
        if gruppe_id:  # Preislistenartikel als Unterposition einer Gruppe
            gruppe_pl = get_object_or_404(Position, pk=gruppe_id, beleg=beleg, typ="G", gruppe__isnull=True)
        eintrag = get_object_or_404(Preislistenartikel.objects.select_related("lieferant"), pk=request.POST["pl"])
        preis = dezimal(request.POST.get("preis"))
        if preis is None:
            preis = preislistenpreis(eintrag, beleg.kunde)
        if preis is None:
            raise ValueError("Für Preislistenartikel ist noch kein Aufschlag hinterlegt (Verwaltung → Globale Preisvorgabe) – bitte den Preis eintragen.")
        standard = Steuersatz.objects.filter(nummer=1).first()
        steuer_pl = standard.prozent if standard else Decimal("19")
        if gruppe_pl and anderer_steuersatz(steuer_pl, gruppe_pl):
            gruppe_pl = None
        if gruppe_pl:
            steuer_passt_zur_gruppe(steuer_pl, gruppe_pl)
            kein_rabatt()
        neue_teil = Position.objects.create(
            beleg=beleg, gruppe=gruppe_pl, typ="P", preisliste_artikel=eintrag, bezeichnung=(request.POST.get("bezeichnung", "").strip() or eintrag.bezeichnung)[:255],
            menge=menge, einzelpreis=preis, rabatt_prozent=rabatt, steuer_prozent=steuer_pl)
        if gruppe_pl:
            gruppenpreis_erhoehen(gruppe_pl, menge * preis)
        beleg.berechne()
        return redirect("verkauf:beleg", beleg_id=beleg.pk)
    gruppe = None
    if gruppe_id:  # Unterposition einer frei zusammengestellten Gruppe
        gruppe = get_object_or_404(Position, pk=gruppe_id, beleg=beleg, typ="G", gruppe__isnull=True)
        if not artikel:
            raise ValueError("Unterpositionen brauchen einen Artikel.")
    bezeichnung = request.POST.get("bezeichnung", "").strip() or (artikel.bezeichnung if artikel else "")
    if not bezeichnung:
        raise ValueError("Artikel oder Bezeichnung fehlt.")
    preis = dezimal(request.POST.get("preis"))
    steuer = artikel.steuersatz.prozent if artikel else Decimal("19")
    if gruppe and anderer_steuersatz(steuer, gruppe):
        gruppe = None
    if gruppe:
        kein_rabatt()

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
            steuer_passt_zur_gruppe(steuer, gruppe)
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
    messages.success(request, "Gespeichert.")
    return redirect("verkauf:beleg", beleg_id=beleg.pk)


@mit_mandant
@require_POST
@sperr_schutz
def positionen_import(request, beleg_id):
    """Positionen aus einer CSV- oder Excel-Datei anhängen."""
    from apps.stammdaten.leistung_import import ImportFehler
    from .position_import import positionen_importieren
    beleg = beleg_holen(request, beleg_id)
    datei = request.FILES.get("datei")
    if datei is None or datei.size > 5 * 1024 * 1024:
        raise ValueError("Bitte eine CSV- oder Excel-Datei bis 5 MB auswählen.")
    try:
        anzahl, netto = positionen_importieren(beleg, datei.name, datei.read())
    except ImportFehler as fehler:
        raise ValueError("\n".join([str(fehler), *fehler.fehler[:20], *(["…"] if len(fehler.fehler) > 20 else [])]))
    messages.success(request, f"{anzahl} Position(en) importiert, netto {str(netto).replace('.', ',')} €.")
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
    messages.success(request, "Gespeichert.")
    return redirect("verkauf:beleg", beleg_id=beleg.pk)


def gruppenmenge_anpassen(gruppe, menge):
    """Teilrückgabe einer Gruppe in der Gutschrift: Die Unterpositionen ändern sich im selben Verhältnis wie der Kopf."""
    teile = list(gruppe.unterpositionen.all())
    for t in teile:
        t.menge = t.menge * menge / gruppe.menge
        if t.menge != t.menge.quantize(Decimal("0.001")):
            raise ValueError(f"Die Menge {menge:f} lässt sich nicht gleichmäßig auf „{t.bezeichnung}“ verteilen. Bitte die Unterpositionen einzeln ändern.")
    for t in teile:
        t.save()


@mit_mandant
@require_POST
@sperr_schutz
def position_aendern(request, beleg_id, position_id):
    """Menge, Preis, Rabatt, Zusatztext und Gruppe einer Position ändern (Unterpositionen einer Gruppe: kein Preis und Rabatt)."""
    with transaction.atomic():
        return _position_aendern(request, beleg_id, position_id)


def _position_aendern(request, beleg_id, position_id):
    beleg = beleg_holen(request, beleg_id)
    p = get_object_or_404(Position, pk=position_id, beleg=beleg)
    menge = dezimal(request.POST.get("menge"), p.menge)
    if menge <= 0:
        raise ValueError("Die Menge muss größer als 0 sein.")
    alter_betrag = p.menge * p.einzelpreis
    neuer_preis = rabatt = None
    if not p.gruppe_id:  # Eingaben zuerst prüfen: scheitert später etwas, wird nichts zurückgebucht und nichts gemeldet
        neuer_preis = dezimal(request.POST.get("preis"), p.einzelpreis)
        rabatt = dezimal(request.POST.get("rabatt"), Decimal("0"))
        if not 0 <= rabatt <= 100:
            raise ValueError("Der Rabatt muss zwischen 0 und 100 % liegen.")
    meldungen = []
    if menge != p.menge and p.verschoben and not p.gruppe_id and not beleg.positionen_gesperrt:
        meldungen = _mitziehen(p, menge)  # Menge aus der Bestellung: Differenz geht zurück bzw. kommt aus dem Rest
    if beleg.art == Belegart.GUTSCHRIFT and p.typ == "G" and not p.gruppe_id and menge != p.menge:
        gruppenmenge_anpassen(p, menge)  # gebucht werden die Unterpositionen, sie müssen dem Kopf folgen
    p.menge = menge
    p.zusatztext = request.POST.get("zusatztext", "")
    if not p.gruppe_id:
        if p.typ == "G" and neuer_preis != p.einzelpreis:
            p.preis_fest = True  # von Hand geänderter Gruppenpreis bleibt stehen
        p.einzelpreis = neuer_preis
        p.rabatt_prozent = rabatt
    ziel_id = request.POST.get("gruppe")
    if ziel_id is None or (p.typ == "G" and not p.gruppe_id) or str(p.gruppe_id or "") == ziel_id:
        p.save()  # eine Gruppe selbst lässt sich nicht in eine Gruppe verschieben
    else:
        gruppe_wechseln(p, get_object_or_404(Position, pk=ziel_id, beleg=beleg, typ="G", gruppe__isnull=True) if ziel_id else None, alter_betrag)
    beleg.berechne()
    for stufe, text in meldungen:  # erst nach erfolgreichem Speichern melden
        getattr(messages, stufe)(request, text)
    messages.success(request, "Gespeichert.")
    return redirect("verkauf:beleg", beleg_id=beleg.pk)


def _mitziehen(p, menge) -> list:
    """Menge einer aus der Bestellung übernommenen Position geändert: Differenz zurückbuchen bzw. aus dem Rest nachziehen.
    Rückgabe: Meldungen [(Stufe, Text)], die der Aufrufer erst nach erfolgreichem Speichern ausgibt."""
    from . import uebernahme
    erg = uebernahme.menge_anpassen(p, menge)
    ab = p.beleg.vorgaenger
    if ab is None:
        return []
    nr = ab.nummer or ""
    meldungen = []
    if erg["zurueck"]:
        meldungen.append(("info", f"{uebernahme._menge_text(erg['zurueck'])} × {p.bezeichnung} gehen zurück in die Bestellung {nr}."))
    if erg["nachgezogen"]:
        meldungen.append(("info", f"{uebernahme._menge_text(erg['nachgezogen'])} × {p.bezeichnung} kommen zusätzlich aus dem Rest der Bestellung {nr}."))
    if erg["zusatz"]:
        meldungen.append(("warning", f"{uebernahme._menge_text(erg['zusatz'])} × {p.bezeichnung} sind Zusatzmenge: "
                                     f"in der Bestellung {nr} ist davon nichts mehr offen, sie bleibt unberührt."))
    return meldungen


@mit_mandant
@require_POST
@sperr_schutz
def position_loeschen(request, beleg_id, position_id):
    beleg = beleg_holen(request, beleg_id)
    p = get_object_or_404(Position, pk=position_id, beleg=beleg)
    with transaction.atomic():
        if p.gruppe_id:  # Unterposition einer Gruppe mit festem Preis: der Preis sinkt um ihren Betrag
            gruppenpreis_erhoehen(p.gruppe, -(p.menge * p.einzelpreis))
        elif p.verschoben and not beleg.positionen_gesperrt:  # übernommene Menge geht zurück in die Bestellung
            from . import uebernahme
            rueck = uebernahme.zurueckbuchen(p, p.menge)
            if rueck:
                messages.info(request, f"{uebernahme._menge_text(rueck)} × {p.bezeichnung} gehen zurück in die Bestellung "
                                       f"{beleg.vorgaenger.nummer or ''}.")
        p.delete()
        beleg.berechne()
    messages.success(request, "Gespeichert.")
    return redirect("verkauf:beleg", beleg_id=beleg.pk)


RUECKNAHME_FOLGE = {Ruecknahme.REPARATUR: Ruecknahme.LAGER, Ruecknahme.LAGER: Ruecknahme.KEINE, Ruecknahme.KEINE: Ruecknahme.REPARATUR}


@mit_mandant
@require_POST
@sperr_schutz
def position_ruecknahme(request, beleg_id, position_id):
    """Spalte B der Gutschrift: Ein Klick wechselt R → B → keine Ware zurück."""
    beleg = beleg_holen(request, beleg_id)
    if beleg.art != Belegart.GUTSCHRIFT:
        raise ValueError("Der Bestand lässt sich nur bei einer Gutschrift wählen.")
    p = get_object_or_404(Position, pk=position_id, beleg=beleg, artikel__typ="A")
    if p.ruecknahme:
        p.ruecknahme = RUECKNAHME_FOLGE[p.ruecknahme]
        p.save()
    else:  # älterer Entwurf: nie gemischt, sonst bliebe ein Teil der Ware ungebucht
        beleg.positionen.update(ruecknahme=Ruecknahme.REPARATUR)
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
    from apps.esd.services import KeysFehlen, fehlende_keys
    ohne_key = request.POST.get("esd_ohne_key") == "1"
    try:
        beleg.abschliessen(esd_ohne_key=ohne_key)
    except KeysFehlen as fehler:  # Rückfrage: ohne Key abschließen und drucken, Key später unter ESD-Keys zuordnen
        return render(request, "verkauf/esd_ohne_key.html", {"beleg": beleg_holen(request, beleg_id), "meldung": str(fehler),
                                                              "danach": request.POST.get("danach", "")})
    except BestandReichtNicht as fehler:
        from apps.lager.services import teilung
        beleg = beleg_holen(request, beleg_id)  # Zustand vor dem abgebrochenen Abschluss
        lieferbar, knapp = teilung(beleg)
        if lieferbar and knapp:  # Teillieferung anbieten
            return render(request, "verkauf/teillieferung.html", {
                "beleg": beleg, "lieferbar": lieferbar, "knapp": knapp, "meldung": str(fehler)})
        raise
    messages.success(request, f"{beleg} abgeschlossen.")
    if ohne_key:
        offen = ", ".join(f"{p.artikel.nummer} ({anzahl}×)" for p, anzahl in fehlende_keys(beleg))
        if offen:
            messages.warning(request, f"Ohne ESD-Key abgeschlossen: {offen}. Key bitte unter ESD-Keys nachträglich zuordnen.")
    danach = set(request.POST.get("danach", "").split(","))
    if danach & {"pdf", "druck"}:  # zuerst das PDF; eine gewählte E-Mail folgt von der Ansicht aus
        parameter = "&".join(n + "=1" for n in ("druck", "mail") if n in danach)
        ziel = reverse("verkauf:beleg_ansicht", args=[beleg.pk])
        return redirect(f"{ziel}?{parameter}" if parameter else ziel)
    if "mail" in danach:
        return redirect("verkauf:beleg_mail", beleg_id=beleg.pk)
    return redirect("verkauf:kunde", kunde_id=beleg.kunde_id)


@mit_mandant
@require_POST
@sperr_schutz
def beleg_folgebeleg(request, beleg_id):
    beleg = beleg_holen(request, beleg_id)
    # Jedes Formular schickt die erwartete Ausgangsart mit: veralteter Tab oder Zurück-Taste nach dem Umwandeln
    # darf nicht still die nächste Stufe (z. B. einen Lieferschein) auslösen.
    if request.POST.get("von_art") != beleg.art:
        messages.error(request, f"{beleg} wurde inzwischen umgewandelt oder geändert – bitte den Beleg neu öffnen. Es wurde nichts übernommen.")
        return redirect("verkauf:beleg", beleg_id=beleg.pk)
    art = request.POST.get("art") or beleg.folgeart  # Bestellung: zusätzlich art=RE (direkt als Rechnung übernehmen)
    with transaction.atomic():  # erst abschließen, dann übernehmen: scheitert die Übernahme, bleibt auch der Abschluss aus
        frage = _erst_abschliessen(request, beleg, art=art, von_art=beleg.art)
        if frage is not None:
            return frage
        entscheidungen = _uebernahme_entscheidungen(request, beleg, art, extra={"art": art, "von_art": beleg.art})
        if isinstance(entscheidungen, HttpResponse):
            return entscheidungen  # Rückfrage bei zu wenig Bestand
        vorher = (beleg.art, beleg.get_art_display(), beleg.nummer or ("(nicht gespeichert)" if beleg.vorlaeufig else None))
        neu = beleg.folgebeleg_erzeugen(entscheidungen=entscheidungen, art=art, vorlaeufig=True)
    if neu.pk == beleg.pk:  # der Beleg selbst wurde umgewandelt, es gibt keinen neuen
        messages.success(request, f"{vorher[1]} {vorher[2] or '(Entwurf)'} in {neu.get_art_display()} "
                                  f"{neu.nummer or '(nicht gespeichert)'} umgewandelt – ohne „Speichern: abschließen & Nummer vergeben“ wird das verworfen.")
        return redirect("verkauf:beleg", beleg_id=neu.pk)
    if vorher[0] == Belegart.AUFTRAG and art == Belegart.RECHNUNG and (beleg.zahlungen.exists() or beleg.anzahlung_verrechnungen.exists()):
        messages.info(request, f"Zur Bestellung {vorher[2] or ''} gibt es Zahlungen: Es wurde eine neue Rechnung angelegt, "
                               "die Bestellung bleibt mit ihren Zahlungen bestehen.")
    _uebernahme_meldung(request, beleg, neu)
    return redirect("verkauf:beleg", beleg_id=neu.pk)


def _erst_abschliessen(request, beleg, **extra):
    """Übernehmen oder Kopieren aus einem noch nicht gespeicherten Beleg: erst Rückfrage „Erst abschließen & Nummer vergeben?“,
    nach der Bestätigung (erst_abschliessen=1) wird abgeschlossen und dann übernommen – es gibt nie Ketten vorläufiger Belege.
    Rückgabe: None (weiter) oder die Rückfrage-Seite. Ein Fehler beim Abschließen bricht ab (Meldung am Beleg)."""
    if not beleg.vorlaeufig:
        return None
    if beleg.vorlaeufiges_angebot:  # Regel des Nutzers: aus einem nicht gespeicherten Angebot ohne Rückfrage; es wird mit dem Ziel abgeschlossen
        return None
    if request.POST.get("erst_abschliessen") != "1":
        felder = [(k, w) for k in request.POST for w in request.POST.getlist(k) if k not in ("csrfmiddlewaretoken", "erst_abschliessen")]
        return render(request, "verkauf/erst_abschliessen_frage.html", {"beleg": beleg, "aktion": request.path, "felder": felder, **extra})
    # Bestellung mit Preislistenartikeln (P): die Übernahme verschiebt sie sofort in ein neues Angebot, der Abschluss darf daran nicht scheitern
    beleg.abschliessen(p_erlaubt=beleg.art == Belegart.AUFTRAG and extra.get("art") in (Belegart.LIEFERSCHEIN, Belegart.RECHNUNG, Belegart.PROFORMA))
    return None


def _uebernahme_entscheidungen(request, beleg, art, extra=None):
    """Bestellung → Lieferschein/Rechnung: reicht der verfügbare Bestand für eine Position nicht, wird je Position gefragt
    (komplett reservieren oder Teillieferung). Rückgabe: Entscheidungen {Positions-ID: Menge}, None (keine Rückfrage nötig)
    oder die Antwortseite mit der Rückfrage."""
    if beleg.art != Belegart.AUFTRAG or art not in (Belegart.LIEFERSCHEIN, Belegart.RECHNUNG):
        return None
    engpaesse = beleg.uebernahme_engpaesse()
    if not engpaesse:
        return None
    entscheidungen, fehlt = {}, False
    for e in engpaesse:
        pk = e["position"].pk
        wahl = request.POST.get(f"wahl_{pk}")
        if wahl == "A":
            entscheidungen[pk] = Decimal("0")
        elif wahl == "B":
            try:
                menge = Decimal(request.POST.get(f"menge_{pk}", "").replace(",", "."))
            except InvalidOperation:
                menge = None
            if menge is None or menge <= 0 or menge > e["verfuegbar"]:
                e["fehler"] = f"Bitte eine Menge von 0,001 bis {e['verfuegbar'].normalize():f} angeben."
                fehlt = True
            else:
                entscheidungen[pk] = menge
        else:
            fehlt = True
    if fehlt:
        for e in engpaesse:
            e["wahl"] = request.POST.get(f"wahl_{e['position'].pk}", "")
            e["menge"] = request.POST.get(f"menge_{e['position'].pk}") or f"{e['verfuegbar'].normalize():f}"
        return render(request, "verkauf/uebernahme_frage.html", {
            "beleg": beleg, "art": art, "art_name": Belegart(art).label, "engpaesse": engpaesse,
            "aktion": request.path, "extra": extra or {}})
    if not any(m > 0 for m in beleg.uebernahme_mengen(entscheidungen).values()):  # alles bleibt reserviert: es gibt nichts zu übernehmen
        messages.info(request, "Nichts übernommen: Die Positionen bleiben komplett in der Bestellung reserviert "
                               "und werden mit dem Wareneingang nachreserviert.")
        return redirect("verkauf:beleg", beleg_id=beleg.pk)
    return entscheidungen


def _uebernahme_meldung(request, beleg, neu):
    info = getattr(neu, "p_info", None)
    if info and info.get("angebot"):
        angebot_nr = info["angebot"].nummer or "(bekommt seine Nummer beim Abschluss)"
        messages.warning(request, f"{info['anzahl']} Preislistenartikel (P) wurden in das neue Angebot {angebot_nr} übernommen, "
                                  f"die Bestellung {beleg.nummer or ''} {'wird mit dem Abschluss erledigt' if neu.vorlaeufig else 'ist erledigt'}. P gibt es nur im Angebot – "
                                  "sie bleiben dort, bis sie als eigene Artikel (A) angelegt sind.")
    elif info:
        messages.warning(request, f"{info['anzahl']} Preislistenartikel (P) sind im {beleg.get_art_display()} {beleg.nummer or ''} geblieben und nicht mitkopiert – "
                                  "P gibt es nur im Angebot, bis sie als eigene Artikel (A) angelegt sind.")
    if beleg.art != Belegart.AUFTRAG:
        return
    from . import uebernahme
    neu_ = getattr(neu, "uebernahmen_neu", None) or []
    rest = beleg.offene_mengen()
    if neu_:
        teile = ", ".join(f"{uebernahme._menge_text(e.menge)} × {e.bezeichnung}" for e in neu_)
        text = f"Aus der Bestellung {beleg.nummer or ''} übernommen: {teile}."
        if rest:
            text += (f" In der Bestellung bleiben nur noch {len(rest)} Position(en) mit dem Rest offen "
                     "(für den nächsten Wareneingang reserviert).")
        else:
            text += " In der Bestellung ist nichts mehr offen – sie ist erledigt."
        messages.info(request, text)
    elif rest:
        messages.info(request, f"Rückstand: {len(rest)} Position(en) der Bestellung {beleg.nummer or ''} bleiben ganz oder teilweise offen "
                               "und sind für den nächsten Wareneingang reserviert.")


@mit_mandant
@require_POST
@sperr_schutz
def beleg_teillieferung(request, beleg_id):
    beleg = beleg_holen(request, beleg_id)
    name, art_name, war_vorlaeufig = str(beleg), beleg.get_art_display(), beleg.vorlaeufig
    neu = beleg.teillieferung_abschliessen()
    rest = getattr(neu, "rest_beleg", None)
    if rest is not None and rest.art == Belegart.AUFTRAG:
        messages.success(request, f"Teillieferung {neu} abgeschlossen. Der Rest bleibt in der Bestellung {rest.nummer or ''} (für den nächsten Wareneingang reserviert).")
        return redirect("verkauf:beleg", beleg_id=rest.pk)
    if war_vorlaeufig and rest is not None:
        messages.success(request, f"Teillieferung {neu} abgeschlossen. Der Rest bleibt im {art_name} ({name}) – nicht gespeichert, "
                                  "bis er mit „Speichern: abschließen & Nummer vergeben“ abgeschlossen wird.")
        return redirect("verkauf:beleg", beleg_id=rest.pk)
    messages.success(request, f"Teillieferung {neu} abgeschlossen. Die übrigen Positionen bleiben im {art_name}-Entwurf offen.")
    return redirect("verkauf:kunde", kunde_id=beleg.kunde_id)


@mit_mandant
@require_POST
@sperr_schutz
def beleg_kopieren(request, beleg_id):
    """Beleg als beliebige Belegart kopieren (z. B. Angebot → Rechnung); der Ursprungsbeleg wird auf Wunsch abgeschlossen."""
    beleg = beleg_holen(request, beleg_id)
    art = request.POST.get("art", "")
    urbeleg = request.POST.get("urbeleg_abschliessen") == "1"
    with transaction.atomic():  # erst abschließen, dann kopieren (siehe beleg_folgebeleg)
        frage = _erst_abschliessen(request, beleg, art=art, urbeleg_abschliessen="1" if urbeleg else "0")
        if frage is not None:
            return frage
        entscheidungen = _uebernahme_entscheidungen(request, beleg, art, extra={"art": art, "urbeleg_abschliessen": "1" if urbeleg else "0"})
        if isinstance(entscheidungen, HttpResponse):
            return entscheidungen
        neu = beleg.kopieren_als(art, urbeleg_abschliessen=urbeleg, entscheidungen=entscheidungen, vorlaeufig=True)
    _uebernahme_meldung(request, beleg, neu)
    messages.success(request, f"{beleg.get_art_display()} als {neu.get_art_display()} kopiert – nicht gespeichert, "
                              "bis „Speichern: abschließen & Nummer vergeben“ gewählt wird.")
    return redirect("verkauf:beleg", beleg_id=neu.pk)


@mit_mandant
@require_POST
@sperr_schutz
def beleg_umwandeln(request, beleg_id):
    """Vollständig bezahlte Proforma-Rechnung in eine abgeschlossene Rechnung umwandeln (Vorkasse verrechnet)."""
    from apps.finanzen.anzahlung import proforma_umwandeln
    beleg = beleg_holen(request, beleg_id)
    rechnung = proforma_umwandeln(beleg, request.user)
    messages.success(request, f"{beleg} in {rechnung} umgewandelt – Zahlungsbedingung „Betrag dankend erhalten“, die Zahlung ist verrechnet.")
    return redirect("verkauf:beleg", beleg_id=rechnung.pk)


@mit_mandant
@require_POST
@sperr_schutz
def beleg_proforma_storno(request, beleg_id):
    """Proforma-Rechnung stornieren (nach vollständiger Rückzahlung); es entsteht ein eigener Storno-Beleg."""
    from apps.finanzen.anzahlung import proforma_stornieren
    beleg = beleg_holen(request, beleg_id)
    storno = proforma_stornieren(beleg, request.POST.get("grund", ""), request.user)
    messages.success(request, f"{beleg} storniert – {storno} gespeichert.")
    return redirect("verkauf:beleg", beleg_id=storno.pk)


@mit_mandant
@xframe_options_sameorigin
def beleg_lieferschein_pdf(request, beleg_id):
    """Lieferschein zur Rechnung drucken (ohne Preise); es entsteht kein Beleg und keine Lagerbuchung."""
    from .pdf import beleg_pdf
    beleg = beleg_holen(request, beleg_id)
    if (gesperrt := _vorlaeufig_gesperrt(request, beleg, "Drucken")) is not None:
        return gesperrt
    if beleg.art != Belegart.RECHNUNG or not beleg.abgeschlossen:
        messages.error(request, "Einen Lieferschein zum Drucken gibt es nur zu einer abgeschlossenen Rechnung.")
        return redirect("verkauf:beleg", beleg_id=beleg.pk)
    antwort = HttpResponse(beleg_pdf(beleg, als_lieferschein=True), content_type="application/pdf")
    antwort["Content-Disposition"] = f'inline; filename="Lieferschein-{beleg.nummer}.pdf"'
    return antwort


def _vorlaeufig_gesperrt(request, beleg, aktion):
    """Vor dem Speichern gibt es nichts nach außen (PDF, Druck, Mail, Zahlung, Versand …): Meldung und Rückkehr zum Beleg."""
    try:
        beleg.vorlaeufig_sperre(aktion)
    except ValueError as fehler:
        messages.error(request, str(fehler))
        return redirect("verkauf:beleg", beleg_id=beleg.pk)
    return None


@mit_mandant
@require_POST
def beleg_verwerfen_view(request, beleg_id):
    """Nicht gespeicherten Beleg verwerfen: alles, was sein Vorgang verändert hat (Mengen in der Bestellung, Umwandlung, Angebot),
    wird zurückgenommen. Mit Kopfzeile X-Wwok-Stand (Rückfrage beim Verlassen, per fetch) antwortet die View mit JSON
    {ok, weiter}, sonst mit Meldung und Weiterleitung."""
    als_json = bool(request.headers.get("X-Wwok-Stand"))
    beleg = Beleg.objects.filter(pk=beleg_id, mandant=request.mandant).first()
    if beleg is None:  # schon verworfen (z. B. von der Bereinigung)
        weiter = reverse("verkauf:start")
        if als_json:
            return JsonResponse({"ok": True, "weiter": weiter})
        messages.info(request, "Der Beleg ist schon verworfen.")
        return redirect(weiter)
    name = str(beleg)
    try:
        ergebnis = vorgang.beleg_verwerfen(beleg, request.user, "von Hand")
    except (ValueError, BelegGesperrt, OperationalError) as fehler:
        text = VERKLEMMT if isinstance(fehler, OperationalError) else str(fehler)
        if als_json:
            return JsonResponse({"ok": False, "fehler": text}, status=409)
        messages.error(request, text)
        return redirect("verkauf:beleg", beleg_id=beleg.pk)
    if ergebnis is None:  # inzwischen gespeichert (abgeschlossen) oder gar nicht vorläufig
        weiter = reverse("verkauf:beleg", args=[beleg.pk])
        if als_json:
            return JsonResponse({"ok": False, "weiter": weiter, "fehler": "Der Beleg ist schon gespeichert – es gibt nichts zu verwerfen."})
        messages.warning(request, "Der Beleg ist schon gespeichert (oder nie vorläufig) – es gibt nichts zu verwerfen.")
        return redirect(weiter)
    for text in ergebnis["meldungen"]:
        messages.warning(request, text)
    messages.success(request, f"{name} verworfen – alles zurückgenommen.")
    if als_json:
        return JsonResponse({"ok": True, "weiter": ergebnis["weiter"]})
    return redirect(ergebnis["weiter"])


@mit_mandant
@require_POST
def beleg_aktiv(request, beleg_id):
    """Puls der geöffneten Belegmaske: hält den vorläufigen Beleg am Leben (zuletzt_aktiv). ok=False: der Beleg wurde inzwischen
    verworfen (Maske geschlossen oder ohne Lebenszeichen) oder gespeichert; weiter zeigt, wohin die Maske wechseln soll."""
    if Beleg.objects.filter(pk=beleg_id, mandant=request.mandant, vorlaeufig=True, abgeschlossen=False).update(zuletzt_aktiv=timezone.now()):
        return JsonResponse({"ok": True})
    beleg = Beleg.objects.filter(pk=beleg_id, mandant=request.mandant).first()
    return JsonResponse({"ok": False, "weiter": reverse("verkauf:beleg", args=[beleg.pk]) if beleg else reverse("verkauf:start")})


@mit_mandant
@require_POST
def beleg_verlassen(request, beleg_id):
    """Signal beim Schließen von Tab/Browser (navigator.sendBeacon): der nicht gespeicherte Beleg gilt in VERLASSEN_SEKUNDEN als
    verlassen und wird bei der nächsten Bereinigung verworfen (Cron jede Minute, Start- und Kundenseite). Lädt die Maske neu
    (F5), setzt ihr Aufruf die Zeit wieder auf jetzt und der Beleg bleibt."""
    from .models import VERLASSEN_SEKUNDEN, VORLAEUFIG_MINUTEN
    zeit = timezone.now() - timedelta(minutes=VORLAEUFIG_MINUTEN) + timedelta(seconds=VERLASSEN_SEKUNDEN)
    Beleg.objects.filter(pk=beleg_id, mandant=request.mandant, vorlaeufig=True, abgeschlossen=False).update(zuletzt_aktiv=zeit)
    return HttpResponse(status=204)


@mit_mandant
@require_POST
def beleg_loeschen(request, beleg_id):
    beleg = beleg_holen(request, beleg_id)
    if beleg.verwerfen_moeglich:  # nicht gespeicherter Beleg: wird verworfen (alles zurück), nicht gelöscht
        return beleg_verwerfen_view(request, beleg_id)
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
        rechnung = rechnung_aus_lieferscheinen(lieferscheine, vorlaeufig=True)
    except ValueError as fehler:
        messages.error(request, str(fehler))
        return redirect("verkauf:kunde", kunde_id=kunde.pk)
    messages.success(request, f"Rechnung aus {len(lieferscheine)} Lieferschein(en) angelegt – mit „Speichern: abschließen & Nummer vergeben“ "
                              "übernehmen, sonst wird sie verworfen.")
    return redirect("verkauf:beleg", beleg_id=rechnung.pk)


@mit_mandant
@require_POST
@sperr_schutz
def beleg_gutschrift(request, beleg_id):
    beleg = beleg_holen(request, beleg_id)
    ruecknahme = request.POST.get("ruecknahme", "")
    if ruecknahme not in Ruecknahme.values:
        if beleg.positionen.filter(artikel__typ="A").exists():
            raise ValueError("Bitte wählen, wohin die zurückgegebene Ware gebucht wird.")
        ruecknahme = Ruecknahme.KEINE
    neu = beleg.gutschrift_erzeugen(ruecknahme, vorlaeufig=True)
    messages.success(request, "Gutschrift angelegt – mit „Speichern: abschließen & Nummer vergeben“ übernehmen, sonst wird sie verworfen.")
    return redirect("verkauf:beleg", beleg_id=neu.pk)


@mit_mandant
def beleg_xrechnung(request, beleg_id):
    """Reine XRechnung (XML, Standard für Behörden) zu einer abgeschlossenen Rechnung/Gutschrift."""
    from .zugferd import ERechnungAusgangFehler, xml_erzeugen
    beleg = beleg_holen(request, beleg_id)
    if (gesperrt := _vorlaeufig_gesperrt(request, beleg, "Die XRechnung")) is not None:
        return gesperrt
    try:
        xml = xml_erzeugen(beleg, xrechnung=True)
    except ERechnungAusgangFehler as fehler:
        messages.error(request, str(fehler))
        return redirect("verkauf:beleg", beleg_id=beleg.pk)
    antwort = HttpResponse(xml, content_type="application/xml")
    antwort["Content-Disposition"] = f'attachment; filename="{beleg.nummer}_xrechnung.xml"'
    return antwort


@mit_mandant
def beleg_ansicht(request, beleg_id):
    """PDF in der Seite anzeigen; mit ?druck=1 öffnet sich gleich der Druckdialog des Browsers."""
    from apps.lager.services import druckhindernis
    beleg = beleg_holen(request, beleg_id)
    if (gesperrt := _vorlaeufig_gesperrt(request, beleg, "Die PDF-Ansicht und der Druck gehen")) is not None:
        return gesperrt
    hindernis = druckhindernis(beleg)
    if hindernis:
        messages.error(request, hindernis)
        return redirect("verkauf:beleg", beleg_id=beleg.pk)
    return render(request, "verkauf/beleg_ansicht.html", {"beleg": beleg, "druck": request.GET.get("druck") == "1",
                                                          "mail": request.GET.get("mail") == "1"})


@mit_mandant
@xframe_options_sameorigin
def beleg_pdf_ansicht(request, beleg_id):
    from apps.lager.services import druckhindernis
    beleg = beleg_holen(request, beleg_id)
    if (gesperrt := _vorlaeufig_gesperrt(request, beleg, "Das PDF geht")) is not None:
        return gesperrt
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
    if (gesperrt := _vorlaeufig_gesperrt(request, beleg, "Eine E-Mail geht")) is not None:
        return gesperrt
    beleg.arbeitsstand_beenden()  # Altdaten: ein früher offener Arbeitsstand wird still beendet
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
def api_kunden(request):
    """Kundensuche für Auswahlfelder (z. B. Werkstatt-Annahme), gleiche Regeln wie die Kundensuche."""
    q = request.GET.get("q", "").strip()
    if len(q.rstrip("*")) < 2:
        return JsonResponse({"treffer": []})
    kunden = kunden_suchen(q).order_by("name1").prefetch_related("ansprechpartner")[:20]
    return JsonResponse({"treffer": [{
        "nummer": k.nummer, "name": " ".join(filter(None, [k.name1, k.name2, k.name3])), "ort": k.ort, "gesperrt": k.gesperrt,
        "ansprechpartner": ", ".join(f"{a.vorname} {a.nachname}".strip() for a in k.ansprechpartner.all())[:120]} for k in kunden]})


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
    artikel = list(artikel)
    letzte = letzte_preise(request.mandant, kunde, [a.pk for a in artikel], [e.pk for e in preisliste]) if kunde else {}

    def letzter(schluessel):
        z = letzte.get(schluessel)
        return {"preis": f"{z['preis']:.2f}".replace(".", ","), "datum": z["datum"].strftime("%d.%m.%Y"), "nummer": z["nummer"]} if z else None

    for a in artikel:
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
                        "preis": f"{preis:.2f}" if preis is not None else "", "sonderpreis": quelle == "sonderpreis",
                        "letzter": letzter(("a", a.pk))})
    from apps.stammdaten.preise import preislistenpreis
    for e in preisliste:  # Lieferantenartikel (Typ P) mit Kurzname des Lieferanten
        preis = preislistenpreis(e, kunde)
        treffer.append({"nummer": e.nummer, "bezeichnung": e.bezeichnung, "typ": "P", "verfuegbar": None, "sonderpreis": False,
                        "preis": f"{preis:.2f}" if preis is not None else "", "letzter": letzter(("p", e.pk)), "pl": e.pk, "ek": str(e.ek),
                        "lieferant": e.lieferant.kurz,
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
    """Aus einer Rechnung eine wiederkehrende Rechnung machen: Positionen werden übernommen, die erste Folgerechnung ist am Starttermin.
    Halbmonatlich schlägt das Programm je Position die Mengenregel aus Zeitraum und Menge vor."""
    from datetime import date
    beleg = beleg_holen(request, beleg_id)
    beleg.vorlaeufig_sperre("Wiederkehrend einrichten geht")
    if beleg.art != Belegart.RECHNUNG:
        raise ValueError("Wiederkehrend einrichten geht nur aus einer Rechnung.")
    beleg.stand_pruefen()
    if not beleg.positionen.exists():
        raise ValueError("Die Rechnung hat noch keine Position.")
    if beleg.p_positionen().exists():
        raise ValueError("Die Rechnung enthält Preislistenartikel (P) – erst Wareneingang buchen, dann wiederkehrend einrichten.")
    if beleg.positionen.filter(gruppe__isnull=False, artikel__typ="A").exists():
        raise ValueError("Die Rechnung enthält eine Gruppe mit Lagerartikeln – die Folgerechnung würde das Lager nicht abbuchen.")
    try:
        monate = int(request.POST.get("monate", "1"))
    except ValueError:
        monate = 0
    if monate not in dict(Abo.RHYTHMEN):
        raise ValueError("Bitte einen gültigen Rhythmus wählen (halbmonatlich, monatlich, vierteljährlich, halbjährlich oder jährlich).")
    try:
        start = date.fromisoformat(request.POST.get("start", ""))
    except ValueError:
        raise ValueError("Bitte ein gültiges Startdatum angeben.")
    abo = Abo.objects.create(
        mandant=beleg.mandant, kunde=beleg.kunde, bezeichnung=(request.POST.get("bezeichnung") or f"nach {beleg.nummer or 'Rechnung'}")[:100],
        monate=monate, naechster_termin=start, starttag=start.day, zahlungsbedingung=beleg.zahlungsbedingung,
        rabatt_prozent=beleg.rabatt_prozent, versandkosten=beleg.versandkosten, verpackung=beleg.verpackung,
        versicherung=beleg.versicherung, vortext=beleg.vortext, schlusstext=beleg.schlusstext)
    kalender = 0
    for p in beleg.positionen.filter(gruppe__isnull=True).order_by("pos", "id"):
        regel, nur_monatsende = regel_vorschlag(p.menge, p.zusatztext) if abo.halbmonatlich else (Mengenregel.FEST, False)
        kalender += regel != Mengenregel.FEST
        AboPosition.objects.create(abo=abo, artikel=p.artikel, bezeichnung=p.bezeichnung, menge=p.menge, einzelpreis=p.einzelpreis,
                                   rabatt_prozent=p.rabatt_prozent, steuer_prozent=p.steuer_prozent, regel=regel, nur_monatsende=nur_monatsende)
    hinweis = f" {kalender} Position(en) rechnen nach dem Kalender, Mengenregeln bitte in der Verwaltung prüfen." if abo.halbmonatlich else ""
    messages.success(request, f"Wiederkehrende Rechnung „{abo.bezeichnung}“ angelegt: nächste Rechnung am {abo.naechster_termin:%d.%m.%Y}.{hinweis}")
    return redirect("verkauf:abos")


@mit_mandant
@require_POST
def abo_aktion(request, abo_id):
    abo = get_object_or_404(Abo, pk=abo_id, mandant=request.mandant)
    aktion = request.POST.get("aktion")
    if aktion == "jetzt":
        offen = abo.offener_beleg()
        if offen:  # sonst würde schon der nächste Zeitraum abgerechnet
            return redirect("verkauf:beleg", beleg_id=offen.pk)
        try:
            beleg = abo.rechnung_erzeugen()
        except ValueError as fehler:
            messages.error(request, str(fehler))
            return redirect("verkauf:abos")
        if beleg is None:
            messages.info(request, "Zu diesem Stichtag ist nichts abzurechnen" + (f", nächste Bestellung am {abo.naechster_termin:%d.%m.%Y}." if abo.aktiv else "; die wiederkehrende Rechnung ist damit beendet."))
            return redirect("verkauf:abos")
        messages.success(request, f"Bestellung {beleg.nummer} für {abo.kunde.name1} angelegt – daraus die Rechnung erstellen.")
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
    if (gesperrt := _vorlaeufig_gesperrt(request, beleg, "Der Versand geht")) is not None:
        return gesperrt
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
        elif aktion == "brief":
            from . import internetmarke as im
            bp = Briefprodukt.objects.filter(pk=request.POST.get("briefprodukt") or None, aktiv=True).first()
            if bp is None:
                messages.error(request, "Bitte ein Briefprodukt wählen.")
                return redirect("verkauf:beleg_versand", beleg_id=beleg.pk)
            # Kauf vorab als „läuft“ eintragen – unter Sperre des Belegs, damit zwei Anfragen (Doppelklick, zweiter Tab,
            # Neuladen) nie zwei Marken kaufen
            kurz_zuvor = timezone.now() - timedelta(seconds=60)
            with transaction.atomic():
                Beleg.objects.select_for_update().get(pk=beleg.pk)
                offen = beleg.versendungen.filter(dienst="dp").filter(
                    Q(kauf_status__in=[Versand.LAEUFT, Versand.UNKLAR])
                    | Q(angelegt_am__gte=kurz_zuvor, erstattung_beantragt__isnull=True)).first()
                if offen is None:
                    v = Versand.objects.create(beleg=beleg, dienst="dp", briefprodukt=bp, porto=bp.preis,
                                               gewicht_kg=gewicht_roh, kauf_status=Versand.LAEUFT)
            if offen is not None:
                messages.error(request, {
                    Versand.UNKLAR: "Ein früherer Briefmarkenkauf zu diesem Beleg ist unklar – bitte in der Portokasse prüfen "
                                    "und den Eintrag „Kauf unklar“ danach löschen (bzw. die Marke nutzen).",
                    Versand.LAEUFT: "Für diesen Beleg läuft gerade ein Briefmarkenkauf – bitte kurz warten und neu laden.",
                }.get(offen.kauf_status, "Für diesen Beleg wurde gerade eine Briefmarke gekauft – Doppelklick? "
                                         "Bitte eine Minute warten, wenn wirklich eine zweite Marke nötig ist."))
                return redirect("verkauf:beleg_versand", beleg_id=beleg.pk)
            try:
                im.marke_kaufen(v)
                messages.success(request, f"Briefmarke gekauft ({bp}). Etikett: „Etikett (PDF)“.")
            except im.PostFehler as fehler:
                if v.shop_order_id and not v.kauf_status:  # Marke ist gekauft, nur das Etikett fehlt noch
                    messages.warning(request, f"Briefmarke gekauft, Etikett noch nicht geladen: {fehler}. Bitte „Etikett (PDF)“ erneut öffnen.")
                elif v.kauf_status == Versand.UNKLAR:
                    messages.error(request, str(fehler))
                else:  # abgelehnt bzw. vor dem Kauf gescheitert: nichts bezahlt
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
            elif aktion == "erstatten" and v.dienst == "dp":
                from . import internetmarke as im
                if v.erstattung_beantragt:
                    messages.info(request, "Die Erstattung ist bereits beantragt.")
                else:
                    try:
                        im.erstatten(v)
                        messages.success(request, "Erstattung beantragt – die Post bucht das Porto in die Portokasse zurück.")
                    except im.PostFehler as fehler:
                        messages.error(request, str(fehler))
            elif aktion == "loeschen":
                if v.dienst == "dp" and v.shop_order_id and not v.erstattung_beantragt:
                    messages.error(request, "Eine gekaufte Briefmarke wird nicht gelöscht – bitte „Erstatten“ wählen.")
                    return redirect("verkauf:beleg_versand", beleg_id=beleg.pk)
                if v.dienst == "dp" and v.shop_order_id:
                    messages.error(request, "Gekaufte Briefmarken bleiben zur Nachweisführung gespeichert.")
                    return redirect("verkauf:beleg_versand", beleg_id=beleg.pk)
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
        "produkte": Versand.PRODUKTE, "produkt": Versand.produkt_vorschlag(gewicht, inland, not ohne_gewicht),
        "briefprodukte": Briefprodukt.objects.filter(aktiv=True),
        "briefprodukt": Briefprodukt.vorschlag(gewicht, not ohne_gewicht)})


@mit_mandant
def versand_aufkleber(request, beleg_id, versand_id):
    from . import versand as vs
    beleg = beleg_holen(request, beleg_id)
    if (gesperrt := _vorlaeufig_gesperrt(request, beleg, "Der Versand geht")) is not None:
        return gesperrt
    v = get_object_or_404(Versand, pk=versand_id, beleg=beleg)
    if not v.label and v.dienst == "dp":  # Briefmarke gekauft, Etikett noch nicht geladen
        from . import internetmarke as im
        try:
            im.etikett_laden(v)
        except im.PostFehler as fehler:
            messages.error(request, f"Etikett der Briefmarke nicht abrufbar: {fehler}")
            return redirect("verkauf:beleg_versand", beleg_id=beleg.pk)
    pdf = bytes(v.label) if v.label else vs.aufkleber_pdf(v)  # amtliches DHL-/Post-Etikett, sonst interner Aufkleber
    Versand.objects.filter(pk=v.pk).update(gedruckt_am=timezone.now())  # am Beleg sichtbar: Label gedruckt
    antwort = HttpResponse(pdf, content_type="application/pdf")
    antwort["Content-Disposition"] = f'inline; filename="paketaufkleber_{beleg.nummer or beleg.pk}.pdf"'
    return antwort

@mit_mandant
def belege(request):
    """Alle Belege des Mandanten, kundenübergreifend: Suche, Belegart, Status, Zeitraum."""
    from datetime import date

    from django.core.paginator import Paginator
    liste = Beleg.objects.filter(Q(positionen__isnull=False) | Q(uebernahmen_aus__isnull=False), mandant=request.mandant).distinct().select_related("kunde").prefetch_related("umwandlungen")
    q = request.GET.get("q", "").strip()
    for wort in q.split():
        liste = liste.filter(Q(nummer__icontains=wort) | Q(umwandlungen__von_nummer__icontains=wort) | Q(kunde__name1__icontains=wort) | Q(kunde__nummer__icontains=wort)
                             | Q(referenz__icontains=wort))
    art = request.GET.get("art", "")
    if art in Belegart.values:
        liste = liste.filter(art=art)
    # Aufruf ohne Filter (z. B. über das Menü): Standard „Entwurf“ und „letzte 30 Tage“. Ein geleertes Feld zeigt alles;
    # Links mit eigenen Filtern (Dashboard, globale Suche) bleiben unbegrenzt.
    standard_30 = not any(k != "seite" for k in request.GET)
    status = request.GET.get("status", "offen" if standard_30 else "")
    if status == "offen":
        liste = liste.filter(abgeschlossen=False)
    elif status == "abgeschlossen":
        liste = liste.filter(abgeschlossen=True)
    elif status == "dashboard" and art in (Belegart.AUFTRAG, Belegart.ANGEBOT):
        # genau die Regel der Dashboard-Kachel (offene Kundenbestellungen bzw. offene Angebote)
        from django.utils import timezone

        from apps.kern import dashboard
        regel = (dashboard.kundenbestellungen_offen(request.mandant) if art == Belegart.AUFTRAG
                 else dashboard.angebote_offen(request.mandant, timezone.localdate()))
        liste = liste.filter(pk__in=regel.order_by().values("pk"))
    zahlung = request.GET.get("zahlung", "")  # eigener Parameter: status=offen bedeutet „Entwurf“
    if zahlung in ("offen", "bezahlt"):
        from apps.finanzen.anzahlung import status_je_proforma
        pfs = list(liste.filter(art=Belegart.PROFORMA, abgeschlossen=True).exclude(folgebelege__art=Belegart.PROFORMA_STORNO)
                   .exclude(folgebelege__art=Belegart.RECHNUNG).order_by())
        codes = ("offen", "teilbezahlt") if zahlung == "offen" else ("bezahlt", "ueberzahlt")
        liste = liste.filter(pk__in=[pk for pk, st in status_je_proforma(pfs).items() if st["code"] in codes])
    zeitraum = {}
    if standard_30:
        from datetime import timedelta

        from django.utils import timezone
        zeitraum_standard = {"von": (timezone.localdate() - timedelta(days=30)).isoformat()}
    else:
        zeitraum_standard = {}
    for feld, bedingung in (("von", "belegdatum__gte"), ("bis", "belegdatum__lte")):
        wert = request.GET.get(feld, zeitraum_standard.get(feld, ""))
        try:
            if wert:
                liste = liste.filter(**{bedingung: date.fromisoformat(wert)})
        except ValueError:
            wert = ""
        zeitraum[feld] = wert
    parameter = request.GET.copy()
    parameter.pop("seite", None)
    seite = Paginator(liste.order_by("-belegdatum", "-id"), 50).get_page(request.GET.get("seite"))
    seite.object_list = Beleg.versand_markieren(seite.object_list)
    from apps.finanzen.anzahlung import status_je_proforma
    zahlstatus = status_je_proforma(seite.object_list)
    for b in seite.object_list:
        b.zahlstatus = zahlstatus.get(b.pk)
    return render(request, "verkauf/belege.html", {
        "seite": seite, "q": q, "art": art, "status": status, "zahlung": zahlung, "von": zeitraum["von"], "bis": zeitraum["bis"],
        "standard_30": standard_30, "arten": Belegart.choices, "parameter": parameter.urlencode(), "anzahl": seite.paginator.count})

@mit_mandant
def suche(request):
    """Globale Suche der Kopfzeile: eindeutige Belegnummer → Beleg, eindeutige Kundennummer → Kundenkarte,
    sonst die Liste „Alle Belege“ mit der Suche."""
    q = request.GET.get("q", "").strip()
    if q:
        belege_treffer = list(Beleg.objects.filter(mandant=request.mandant, nummer__iexact=q).values_list("pk", flat=True)[:2])
        if not belege_treffer:  # alte Nummer eines umgewandelten Belegs (Angebot/Bestellung): führt zum umgewandelten Beleg
            belege_treffer = list(Beleg.objects.filter(mandant=request.mandant, umwandlungen__von_nummer__iexact=q)
                                  .values_list("pk", flat=True).distinct()[:2])
        if len(belege_treffer) == 1:
            return redirect("verkauf:beleg", beleg_id=belege_treffer[0])
        kunden_treffer = list(Kunde.objects.filter(nummer__iexact=q, anonymisiert=False).values_list("pk", flat=True)[:2])
        if len(kunden_treffer) == 1:
            return redirect("verkauf:kunde", kunde_id=kunden_treffer[0])
    ziel = reverse("verkauf:belege")
    return redirect(ziel + "?" + urlencode({"q": q}) if q else ziel)

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
    weiter = request.POST.get("weiter", "")  # zurück zur aufrufenden Seite, nur interne Pfade
    if weiter and url_has_allowed_host_and_scheme(weiter, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return redirect(weiter)
    return redirect("verkauf:kunde", kunde_id=w.kunde_id)
