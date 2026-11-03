"""Bearbeiten und Neuanlegen von Kunden und Lieferanten im Dialog (Registerkarten). Die Ansichten liefern ein HTML-Fragment
für den Dialog; gespeichert wird per fetch, bei Fehlern kommt das Fragment mit den Meldungen zurück (Status 422)."""
import json
from decimal import Decimal

from django.db import transaction
from django.db.models import Q
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.verkauf.views import dezimal, mit_mandant

from . import forms as f
from . import ustid
from .models import (Artikel, ArtikelMandant, Artikeltyp, GruppenKomponente, Kunde, KundeMandant, Lieferant, LieferantMandant, Stammnummer,
                     UstIdPruefung)
from .preise import komponenten_zeile


def alle_gueltig(*objekte) -> bool:
    ergebnis = [o.is_valid() for o in objekte if o is not None]  # alle prüfen, damit jede Registerkarte ihre Fehler zeigt
    return all(ergebnis)


def hat_fehler(tab) -> bool:
    if tab["form"] is not None:
        return any(n in tab["form"].errors for n in tab["felder"])
    if tab["formset"] is not None:
        return any(tab["formset"].errors) or bool(tab["formset"].non_form_errors())
    return bool(tab["extra"].errors)


def fehlerhafte_tabs(tabs):
    for tab in tabs:
        tab["fehler"] = hat_fehler(tab)
    return tabs


def komponenten_lesen(post, gruppe):
    """Komponenten aus dem Formular; gleiche Artikel werden zusammengefasst.
    Rückgabe: (Artikel und Mengen, Zeilen für die Maske samt ungültiger Eingaben, Fehlermeldungen)."""
    zeilen, anzeige, fehler = {}, {}, []
    for pk, text in zip(post.getlist("komp_id"), post.getlist("komp_menge")):
        artikel = Artikel.objects.filter(pk=pk).first() if pk.isdigit() else None
        if artikel is None:
            fehler.append("Eine Komponente wurde nicht gefunden.")
            continue
        anzeige.setdefault(artikel.pk, {**komponenten_zeile(artikel, Decimal("1")), "menge": text})  # bleibt sichtbar, auch wenn ungültig
        try:
            menge = dezimal(text)
        except ValueError:
            menge = None
        if menge is None or not Decimal("0.001") <= menge <= Decimal("9999999"):
            fehler.append(f"{artikel.nummer}: Die Menge muss größer als 0 sein.")
            continue
        if artikel.typ == Artikeltyp.G or artikel.pk == gruppe.pk:
            fehler.append(f"{artikel.nummer}: Eine Gruppe darf keine weitere Gruppe enthalten.")
            continue
        zeilen[artikel.pk] = (artikel, zeilen[artikel.pk][1] + menge if artikel.pk in zeilen else menge)
    return list(zeilen.values()), list(anzeige.values()), fehler


def komponenten_speichern(gruppe, gewaehlt):
    """Nur tatsächliche Änderungen schreiben, damit das Änderungsprotokoll stimmt."""
    vorhanden = {k.komponente_id: k for k in gruppe.komponenten.all()}
    for artikel, menge in gewaehlt:
        k = vorhanden.pop(artikel.pk, None)
        if k is None:
            GruppenKomponente.objects.create(gruppe=gruppe, komponente=artikel, menge=menge)
        elif k.menge != menge:
            k.menge = menge
            k.save()
    for k in vorhanden.values():
        k.delete()


@mit_mandant
def komponenten_suche(request):
    """Artikelsuche für den Reiter Komponenten: Lagerartikel und Leistungen, keine Gruppen."""
    q = request.GET.get("q", "").strip()
    if len(q) < 2:
        return JsonResponse({"treffer": []})
    treffer = Artikel.objects.filter(nicht_aktiv=False, typ__in=[Artikeltyp.A, Artikeltyp.L])
    for wort in q.split():
        treffer = treffer.filter(Q(nummer__icontains=wort) | Q(bezeichnung__icontains=wort) | Q(herstellernummer__icontains=wort)
                                 | Q(hersteller__name__icontains=wort) | Q(ean__icontains=wort))
    return JsonResponse({"treffer": [komponenten_zeile(a, Decimal("1")) for a in treffer.order_by("nummer")[:20]]})


def baue_tabs(art, form, formsets, mandantform):
    tabs = [{"id": kennung, "titel": titel, "felder": felder, "form": form, "formset": None, "extra": None}
            for kennung, titel, felder in form.TABS]
    for kennung, titel, fs in formsets:
        tabs.append({"id": kennung, "titel": titel, "felder": [], "form": None, "formset": fs, "extra": None})
    tabs.append({"id": "mandant", "titel": "Mandant", "felder": [], "form": None, "formset": None, "extra": mandantform})
    return fehlerhafte_tabs(tabs)


def artikel_tabs(form, mandantform, komponenten, fehler=False):
    """Artikel-Registerkarten; der Reiter Komponenten steht nach den Preisen und ist nur bei Typ G sichtbar."""
    tabs = baue_tabs("artikel", form, [], mandantform)
    tabs.insert(2, {"id": "komponenten", "titel": "Komponenten", "felder": [], "form": None, "formset": None, "extra": None,
                    "komponenten": komponenten, "fehler": fehler})
    return tabs


def bearbeiten(request, art: str, pk=None):
    """art: 'kunde' oder 'lieferant'; ohne pk wird neu angelegt."""
    kunde = art == "kunde"
    modell = Kunde if kunde else Lieferant
    zaehler = Stammnummer.Art.KUNDE if kunde else Stammnummer.Art.LIEFERANT
    instanz = get_object_or_404(modell, pk=pk) if pk else modell(nummer=Stammnummer.vorschlag(zaehler))
    FormKlasse = f.KundeForm if kunde else f.LieferantForm
    mandant = request.mandant
    daten = request.POST if request.method == "POST" else None

    form = FormKlasse(daten, instance=instanz)
    if kunde:
        ap_set = f.AnsprechpartnerKundeSet(daten, instance=instanz, prefix="ap")
        an_set = f.AnschriftSet(daten, instance=instanz, prefix="an")
        formsets = [("ap", "Ansprechpartner", ap_set), ("an", "Anschriften", an_set)]
        md_modell, md_form = KundeMandant, f.KundeMandantForm
        md_instanz = KundeMandant.objects.filter(kunde=instanz, mandant=mandant).first() if pk else None
        md_instanz = md_instanz or KundeMandant(mandant=mandant)
    else:
        ap_set = f.AnsprechpartnerLieferantSet(daten, instance=instanz, prefix="ap",
                                               queryset=instanz.ansprechpartner.filter(Q(mandant=mandant) | Q(mandant__isnull=True)) if pk
                                               else None)
        an_set = None
        formsets = [("ap", "Ansprechpartner", ap_set)]
        md_modell, md_form = LieferantMandant, f.LieferantMandantForm
        md_instanz = LieferantMandant.objects.filter(lieferant=instanz, mandant=mandant).first() if pk else None
        md_instanz = md_instanz or LieferantMandant(mandant=mandant)
    mandantform = md_form(daten, instance=md_instanz, prefix="md")

    if request.method == "POST":
        warnung = ""
        if kunde and not pk and form.is_valid() and not request.POST.get("trotzdem"):  # Dublettenprüfung beim Neukunden
            gleich = Kunde.objects.filter(name1__iexact=form.cleaned_data["name1"], plz=form.cleaned_data["plz"]).first()
            if gleich:
                warnung = (f"Es gibt schon einen Kunden „{gleich.name1}“ in {gleich.plz} {gleich.ort} (Nr. {gleich.nummer}). "
                           "Wirklich einen weiteren anlegen?")
        if alle_gueltig(form, ap_set, an_set, mandantform) and not warnung:
            with transaction.atomic():
                if not pk:
                    Stammnummer.belegen(zaehler, form.cleaned_data["nummer"])
                objekt = form.save()
                for fs in (ap_set, an_set):
                    if fs is None:
                        continue
                    fs.instance = objekt
                    neue = fs.save(commit=False)
                    for o in fs.deleted_objects:
                        o.delete()
                    for o in neue:
                        if not kunde:
                            o.mandant = mandant  # Ansprechpartner von Lieferanten gelten je Mandant
                        o.save()
                md = mandantform.save(commit=False)
                setattr(md, "kunde" if kunde else "lieferant", objekt)
                md.mandant = mandant
                md.save()
            ziel = reverse("verkauf:kunde", args=[objekt.pk]) if kunde else None
            return JsonResponse({"ok": True, "ziel": ziel})
        status = 422
    else:
        warnung, status = "", 200

    titel = (f"{'Kunde' if kunde else 'Lieferant'} {instanz.nummer} – {instanz.name1}" if pk
             else f"Neuer {'Kunde' if kunde else 'Lieferant'}")
    antwort = render(request, "stammdaten/modal.html", {
        "titel": titel, "tabs": baue_tabs(art, form, formsets, mandantform), "form": form, "neu": not pk, "warnung": warnung,
        "aktion": request.path, "kunde": kunde, "mandant": mandant,
        "ust_url": reverse(f"stammdaten:{art}_ust_id_pruefen", args=[pk]) if pk else None,
        "ust_pruefungen": UstIdPruefung.objects.filter(mandant=mandant, **{art: instanz}).select_related("benutzer")[:5] if pk else []})
    antwort.status_code = status
    return antwort


@mit_mandant
def kunde_bearbeiten(request, pk=None):
    return bearbeiten(request, "kunde", pk)


@mit_mandant
def lieferant_bearbeiten(request, pk=None):
    return bearbeiten(request, "lieferant", pk)


def ust_id_pruefen(request, art: str, pk):
    """Prüft die Eingaben der offenen Maske. Das Datum setzt erst „Speichern“, der Nachweis entsteht sofort, auch bei ungültiger Nummer."""
    partner = get_object_or_404(Kunde if art == "kunde" else Lieferant, pk=pk)
    p = request.POST
    name = f"{p.get('name1', '').strip()} {p.get('name2', '').strip()}".strip()  # lange Firmennamen stehen oft auf zwei Zeilen
    try:
        ergebnis = ustid.pruefen(request.mandant, p.get("ust_id"), p.get("land") or partner.land, firmenname=name[:150],
                                 strasse=p.get("strasse", "").strip()[:200], plz=p.get("plz", "").strip()[:10], ort=p.get("ort", "").strip()[:100])
    except ustid.UstIdFehler as fehler:
        return JsonResponse({"ok": False, "text": str(fehler)})
    pruefung = UstIdPruefung.objects.create(mandant=request.mandant, benutzer=request.user, **{art: partner}, **ergebnis)
    return JsonResponse({"ok": True, "gueltig": pruefung.gueltig, "ust_id": pruefung.ust_id, "text": pruefung.meldung, "abgleich": pruefung.abgleich,
                         "abweichung": pruefung.abweichung, "datum": timezone.localdate().isoformat()})


@mit_mandant
@require_POST
def kunde_ust_id_pruefen(request, pk):
    return ust_id_pruefen(request, "kunde", pk)


@mit_mandant
@require_POST
def lieferant_ust_id_pruefen(request, pk):
    return ust_id_pruefen(request, "lieferant", pk)


@mit_mandant
def artikel_bearbeiten(request, pk=None):
    """Artikelstamm im Dialog mit Registerkarten; Lagerplatz und Bestandsgrenzen gelten je Mandant."""
    mandant = request.mandant
    instanz = get_object_or_404(Artikel, pk=pk) if pk else Artikel(nummer=Stammnummer.vorschlag(Artikeltyp.A))
    daten = request.POST if request.method == "POST" else None
    form = f.ArtikelForm(daten, instance=instanz)
    if not pk:  # jede Art hat ihren Zähler, die Maske tauscht die Nummer beim Wechsel des Typs
        form.fields["nummer"].widget.attrs["data-vorschlaege"] = json.dumps({t: Stammnummer.vorschlag(t) for t in (Artikeltyp.A, Artikeltyp.L, Artikeltyp.G)})
    komponenten = [komponenten_zeile(k.komponente, k.menge) for k in instanz.komponenten.select_related("komponente")] if pk else []
    md_instanz = (ArtikelMandant.objects.filter(artikel=instanz, mandant=mandant).first() if pk else None) or ArtikelMandant(mandant=mandant)
    mandantform = f.ArtikelMandantForm(daten, instance=md_instanz, prefix="md")
    status = 200
    if request.method == "POST":
        gueltig = alle_gueltig(form, mandantform)
        # der Marker setzt erst das Skript des Reiters: ohne geladenen Reiter bleiben die Komponenten unberührt
        gruppe = form.cleaned_data.get("typ", instanz.typ) == Artikeltyp.G and "komp_geladen" in request.POST
        gewaehlt, anzeige, fehler = komponenten_lesen(request.POST, instanz) if gruppe else ([], komponenten, [])
        komponenten = anzeige
        for text in fehler:
            form.add_error(None, text)
        if gueltig and not fehler:
            with transaction.atomic():
                if not pk:
                    Stammnummer.belegen(form.cleaned_data["typ"], form.cleaned_data["nummer"])
                objekt = form.save()
                md = mandantform.save(commit=False)
                md.artikel, md.mandant = objekt, mandant
                md.save()
                if gruppe:
                    komponenten_speichern(objekt, gewaehlt)
            # Neu: zur Artikelseite; Bearbeiten: auf der Seite bleiben, von der aus geöffnet wurde (z. B. Artikelliste)
            return JsonResponse({"ok": True, "ziel": None if pk else reverse("lager:artikel", args=[objekt.pk])})
        status = 422
    antwort = render(request, "stammdaten/modal.html", {
        "titel": f"Artikel {instanz.nummer} – {instanz.bezeichnung}" if pk else "Neuer Artikel",
        "tabs": artikel_tabs(form, mandantform, komponenten, bool(form.non_field_errors())), "form": form, "neu": not pk, "warnung": "",
        "aktion": request.path, "kunde": False, "mandant": mandant})
    antwort.status_code = status
    return antwort
