"""Bearbeiten und Neuanlegen von Kunden und Lieferanten im Dialog (Registerkarten). Die Ansichten liefern ein HTML-Fragment
für den Dialog; gespeichert wird per fetch, bei Fehlern kommt das Fragment mit den Meldungen zurück (Status 422)."""
from django.db import transaction
from django.db.models import Q
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse

from apps.verkauf.views import mit_mandant

from . import forms as f
from .models import Artikel, ArtikelMandant, Kunde, KundeMandant, Lieferant, LieferantMandant


def naechste_nummer(modell) -> str:
    zahlen = [int(n) for n in modell.objects.values_list("nummer", flat=True) if n.isdigit()]
    return str(max(zahlen, default=0) + 1)


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


def baue_tabs(art, form, formsets, mandantform):
    tabs = [{"id": kennung, "titel": titel, "felder": felder, "form": form, "formset": None, "extra": None}
            for kennung, titel, felder in form.TABS]
    for kennung, titel, fs in formsets:
        tabs.append({"id": kennung, "titel": titel, "felder": [], "form": None, "formset": fs, "extra": None})
    tabs.append({"id": "mandant", "titel": "Mandant", "felder": [], "form": None, "formset": None, "extra": mandantform})
    return fehlerhafte_tabs(tabs)


def bearbeiten(request, art: str, pk=None):
    """art: 'kunde' oder 'lieferant'; ohne pk wird neu angelegt."""
    kunde = art == "kunde"
    modell = Kunde if kunde else Lieferant
    instanz = get_object_or_404(modell, pk=pk) if pk else modell(nummer=naechste_nummer(modell))
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
        "aktion": request.path, "kunde": kunde, "mandant": mandant})
    antwort.status_code = status
    return antwort


@mit_mandant
def kunde_bearbeiten(request, pk=None):
    return bearbeiten(request, "kunde", pk)


@mit_mandant
def lieferant_bearbeiten(request, pk=None):
    return bearbeiten(request, "lieferant", pk)

@mit_mandant
def artikel_bearbeiten(request, pk=None):
    """Artikelstamm im Dialog mit Registerkarten; Lagerplatz und Bestandsgrenzen gelten je Mandant."""
    mandant = request.mandant
    instanz = get_object_or_404(Artikel, pk=pk) if pk else Artikel(nummer=naechste_nummer(Artikel))
    daten = request.POST if request.method == "POST" else None
    form = f.ArtikelForm(daten, instance=instanz)
    md_instanz = (ArtikelMandant.objects.filter(artikel=instanz, mandant=mandant).first() if pk else None) or ArtikelMandant(mandant=mandant)
    mandantform = f.ArtikelMandantForm(daten, instance=md_instanz, prefix="md")
    status = 200
    if request.method == "POST":
        if alle_gueltig(form, mandantform):
            with transaction.atomic():
                objekt = form.save()
                md = mandantform.save(commit=False)
                md.artikel, md.mandant = objekt, mandant
                md.save()
            return JsonResponse({"ok": True, "ziel": reverse("lager:artikel", args=[objekt.pk])})
        status = 422
    antwort = render(request, "stammdaten/modal.html", {
        "titel": f"Artikel {instanz.nummer} – {instanz.bezeichnung}" if pk else "Neuer Artikel",
        "tabs": baue_tabs("artikel", form, [], mandantform), "form": form, "neu": not pk, "warnung": "",
        "aktion": request.path, "kunde": False, "mandant": mandant})
    antwort.status_code = status
    return antwort
