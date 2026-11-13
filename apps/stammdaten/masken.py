"""Bearbeiten und Neuanlegen von Kunden und Lieferanten im Dialog (Registerkarten). Die Ansichten liefern ein HTML-Fragment
für den Dialog; gespeichert wird per fetch, bei Fehlern kommt das Fragment mit den Meldungen zurück (Status 422)."""
import json
from decimal import Decimal
from urllib.parse import urlencode

from django import forms as djforms
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F, Q
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.verkauf.views import dezimal, mit_mandant

from . import forms as f
from . import kopie, ustid
from .models import (BILD_FORMATE, MAX_BILD_BYTES, MAX_BILDER, Artikel, ArtikelBild, ArtikelMandant, Artikeltyp, GruppenKomponente, Kunde, KundeMandant,
                     Lieferant, LieferantMandant, Preislistenartikel, Stammnummer, UstIdPruefung)
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


ARTIKELTABELLE_MAX = 300


@mit_mandant
def artikel_tabelle(request):
    """Artikelliste für die Tabellensuche der Suchfelder (z. B. Nachfolger, „*“ + Enter): Nummer, Bezeichnung,
    Lagerbestand des Mandanten und VK3 netto. „*“ allein = alle; sonst muss jedes Wort vorkommen (Sterne werden ignoriert)."""
    from django.db.models import Sum

    from apps.lager.models import Lagerbewegung

    from .preise import stufenpreis
    q = request.GET.get("q", "").strip()
    treffer = Artikel.objects.filter(nicht_aktiv=False, typ__in=[Artikeltyp.A, Artikeltyp.G])
    ohne = request.GET.get("ohne", "")
    if ohne.isdigit():
        treffer = treffer.exclude(pk=ohne)
    for wort in q.replace("*", " ").split():
        treffer = treffer.filter(Q(nummer__icontains=wort) | Q(bezeichnung__icontains=wort) | Q(herstellernummer__icontains=wort)
                                 | Q(hersteller__name__icontains=wort) | Q(ean__icontains=wort))
    artikel = list(treffer.order_by("nummer")[:ARTIKELTABELLE_MAX + 1])
    mehr = len(artikel) > ARTIKELTABELLE_MAX
    artikel = artikel[:ARTIKELTABELLE_MAX]
    bestaende = dict(Lagerbewegung.objects.filter(mandant=request.mandant, artikel_id__in=[a.pk for a in artikel])
                     .order_by().values_list("artikel_id").annotate(s=Sum("menge")))

    def zahl(wert, stellen):
        return "" if wert is None else f"{wert:,.{stellen}f}".replace(",", "X").replace(".", ",").replace("X", ".")

    return JsonResponse({"mehr": mehr, "treffer": [
        {"pk": a.pk, "nummer": a.nummer, "bezeichnung": a.bezeichnung,
         "bestand": "" if a.typ != Artikeltyp.A else zahl(b := bestaende.get(a.pk) or Decimal("0"), 0 if b == b.to_integral() else 3), "vk3": zahl(stufenpreis(a, 3), 2)}
        for a in artikel]})


def baue_tabs(art, form, formsets, mandantform):
    tabs = [{"id": kennung, "titel": titel, "felder": felder, "form": form, "formset": None, "extra": None}
            for kennung, titel, felder in form.TABS]
    for kennung, titel, fs in formsets:
        tabs.append({"id": kennung, "titel": titel, "felder": [], "form": None, "formset": fs, "extra": None})
    tabs.append({"id": "mandant", "titel": "Mandant", "felder": [], "form": None, "formset": None, "extra": mandantform})
    return fehlerhafte_tabs(tabs)


def bilder_daten(artikel, quelle=None):
    """Vorhandene Bilder für die Galerie im Reiter (Reihenfolge = Position). Bei einer Kopie zeigt die Galerie die Bilder der Vorlage."""
    bilder = artikel.bilder.all() if artikel.pk else quelle.bilder.all() if quelle else []
    return [{"id": b.pk, "url": b.bild.url, "vorschau": b.vorschau_url, "alt": b.alt_text} for b in bilder]


def bild_pruefen(datei):
    """Fehlermeldung zu einer hochgeladenen Datei oder None, wenn sie als Bild (JPG, PNG, WEBP, höchstens 10 MB) taugt."""
    if datei.size > MAX_BILD_BYTES:
        return f"{datei.name}: die Datei ist größer als {MAX_BILD_BYTES // (1024 * 1024)} MB."
    try:
        geprueft = djforms.ImageField().clean(datei)
    except ValidationError as e:
        return f"{datei.name}: {e.messages[0]}"
    if geprueft.image.format not in BILD_FORMATE:
        return f"{datei.name}: nur JPG, PNG oder WEBP sind erlaubt."
    return None


def bilder_lesen(post, files, artikel, quelle=None):
    """Liest die Galerie aus dem Formular. Die Reihenfolge in „bild_reihe“ ist die Position; „b<id>“ ist ein vorhandenes Bild,
    „n<marke>“ eine neue Datei (bild_neu_<marke>); was fehlt, wird gelöscht. Ohne Marker „bilder_geladen“ bleibt alles unberührt.
    Rückgabe: (Plan oder None, Fehlermeldungen); Plan = Liste aus (vorhandenes Bild, Datei, Alt-Text)."""
    if "bilder_geladen" not in post:
        return None, []
    vorhanden = {f"b{b.pk}": b for b in (artikel.bilder.all() if artikel.pk else quelle.bilder.all() if quelle else [])}
    plan, fehler, gesehen = [], [], set()
    for eintrag in post.getlist("bild_reihe"):
        if eintrag in gesehen:
            continue
        gesehen.add(eintrag)
        alt_text = post.get(f"bild_alt_{eintrag}", "").strip()[:150]
        if eintrag in vorhanden:
            plan.append((vorhanden[eintrag], None, alt_text))
        elif eintrag.startswith("n"):
            datei = files.get(f"bild_neu_{eintrag[1:]}")
            meldung = bild_pruefen(datei) if datei is not None else "Eine Bilddatei fehlt."
            if meldung:
                fehler.append(meldung)
            else:
                plan.append((None, datei, alt_text))
    if len(plan) > MAX_BILDER:
        fehler.append(f"Es sind höchstens {MAX_BILDER} Bilder je Artikel möglich.")
    return plan, fehler


def bilder_speichern(artikel, plan):
    """Setzt die Galerie um: nicht mehr aufgeführte Bilder werden gelöscht (samt Datei), der Rest bekommt Position 1, 2, …"""
    if plan is None:
        return
    behalten = {bild.pk for bild, _, _ in plan if bild}
    for bild in artikel.bilder.all():
        if bild.pk not in behalten:
            bild.delete()
    artikel.bilder.update(position=F("position") + 100)  # Platz schaffen, damit das Umsortieren die Eindeutigkeit nicht verletzt
    for position, (bild, datei, alt_text) in enumerate(plan, start=1):
        if bild and bild.artikel_id != artikel.pk:  # Bild der Vorlage (Kopie): eigene Datei für den neuen Artikel
            kopie.bild_duplizieren(bild, artikel, position, alt_text)
        elif bild:
            bild.position, bild.alt_text = position, alt_text
            bild.save(update_fields=["position", "alt_text"])
        else:
            neu = ArtikelBild(artikel=artikel, position=position, alt_text=alt_text)
            neu.bild.save(datei.name, datei, save=False)
            neu.save()


def artikel_tabs(form, mandantform, komponenten, fehler=False, bilder=None, bilderfehler=False):
    """Artikel-Registerkarten; Komponenten (nur Typ G) und Bilder stehen nach den Preisen."""
    tabs = baue_tabs("artikel", form, [], mandantform)
    tabs.insert(2, {"id": "komponenten", "titel": "Komponenten", "felder": [], "form": None, "formset": None, "extra": None,
                    "komponenten": komponenten, "fehler": fehler})
    bilder = bilder or []
    tabs.insert(3, {"id": "bilder", "titel": "Bilder", "felder": [], "form": None, "formset": None, "extra": None,
                    "daten": bilder, "anzahl": len(bilder), "max": MAX_BILDER, "max_bytes": MAX_BILD_BYTES,
                    "max_mb": MAX_BILD_BYTES // (1024 * 1024), "fehler": bilderfehler})
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
    # Kopie (Issue 15): „Neuer Artikel“ mit den Werten einer Vorlage; angelegt wird erst beim Speichern (die Vorlage bleibt unverändert)
    para = request.POST if request.method == "POST" else request.GET
    vorlage = get_object_or_404(Artikel, pk=para.get("kopie_von")) if not pk and para.get("kopie_von", "").isdigit() else None
    bilder_kopie = vorlage if vorlage and para.get("kopie_bilder") else None
    # P → A (Issue 24): Preislistenartikel als neuen Lagerartikel übernehmen; vorher Dublettenprüfung
    pl_eintrag = (get_object_or_404(Preislistenartikel.objects.select_related("lieferant"), pk=para["kopie_von_pl"])
                  if not pk and not vorlage and para.get("kopie_von_pl", "").isdigit() else None)
    # aus dem Wareneingang geöffnet: nach dem Speichern den neuen Artikel dort übernehmen (Bestellposition bzw. Scan-Menge)
    we_bezug, we_fehler, we_para = None, "", {}
    if pl_eintrag and para.get("we"):
        from apps.einkauf.services import we_bezug_holen
        we_bezug = we_bezug_holen(mandant, pl_eintrag, para.get("we"), para.get("we_bp", ""), para.get("we_menge", ""))
        if we_bezug is None:
            if request.method != "POST":
                raise Http404("Wareneingang oder Position nicht (mehr) offen.")
            # beim Speichern inzwischen gebucht o. Ä.: Meldung im Formular statt Fehlerseite, Bezug bleibt (speichert nie ohne ihn)
            we_fehler = "Der Wareneingang oder die Position ist nicht mehr offen (z. B. inzwischen gebucht) – es wurde nichts angelegt."
            we_para = {k: para.get(k, "") for k in ("we", "we_bp", "we_menge")}
        else:
            we_para = {"we": we_bezug["we"].pk, "we_bp": we_bezug["bp"].pk if we_bezug["bp"] else "", "we_menge": we_bezug["menge"]}
    if pl_eintrag and request.method == "GET" and not request.GET.get("trotzdem"):
        dubletten = kopie.dubletten_zu_preisliste(pl_eintrag)
        if dubletten:
            weiter = f"{reverse('stammdaten:artikel_neu')}?{urlencode({'kopie_von_pl': pl_eintrag.pk, 'trotzdem': 1, **we_para})}"
            return render(request, "stammdaten/kopie_dublette.html", {
                "eintrag": pl_eintrag, "dubletten": dubletten, "weiter": weiter})
    if pk:
        instanz = get_object_or_404(Artikel, pk=pk)
    elif pl_eintrag:
        instanz = kopie.neuer_artikel_aus_preisliste(pl_eintrag)
    else:
        instanz = kopie.neuer_artikel(vorlage) if vorlage else Artikel(nummer=Stammnummer.vorschlag(Artikeltyp.A))
    daten = request.POST if request.method == "POST" else None
    form = f.ArtikelForm(daten, instance=instanz)
    if we_para:  # aus dem Wareneingang: immer Lagerartikel (Typ A), der Typ ist nicht wählbar
        form.fields["typ"].disabled = True
        form.fields["typ"].initial = Artikeltyp.A
    if not pk:  # jede Art hat ihren Zähler, die Maske tauscht die Nummer beim Wechsel des Typs
        form.fields["nummer"].widget.attrs["data-vorschlaege"] = json.dumps({t: Stammnummer.vorschlag(t) for t in (Artikeltyp.A, Artikeltyp.L, Artikeltyp.G)})
    if vorlage:
        komponenten = [komponenten_zeile(k, menge) for k, menge in kopie.komponenten_werte(vorlage)]
    else:
        komponenten = [komponenten_zeile(k.komponente, k.menge) for k in instanz.komponenten.select_related("komponente")] if pk else []
    md_instanz = (ArtikelMandant.objects.filter(artikel=instanz, mandant=mandant).first() if pk else None) or ArtikelMandant(
        mandant=mandant, **(kopie.mandant_werte(vorlage, mandant) if vorlage else {}))
    mandantform = f.ArtikelMandantForm(daten, instance=md_instanz, prefix="md")
    status = 200
    if request.method == "POST":
        gueltig = alle_gueltig(form, mandantform)
        if we_fehler:
            form.add_error(None, we_fehler)
            gueltig = False
        # der Marker setzt erst das Skript des Reiters: ohne geladenen Reiter bleiben die Komponenten unberührt
        gruppe = form.cleaned_data.get("typ", instanz.typ) == Artikeltyp.G and "komp_geladen" in request.POST
        gewaehlt, anzeige, fehler = komponenten_lesen(request.POST, instanz) if gruppe else ([], komponenten, [])
        komponenten = anzeige
        for text in fehler:
            form.add_error(None, text)
        bilderplan, bildfehler = bilder_lesen(request.POST, request.FILES, instanz, bilder_kopie)
        for text in bildfehler:
            form.add_error(None, f"Bilder: {text}")
        if gueltig and not fehler and not bildfehler:
            try:
                with transaction.atomic():
                    if not pk:
                        Stammnummer.belegen(form.cleaned_data["typ"], form.cleaned_data["nummer"])
                    objekt = form.save()
                    md = mandantform.save(commit=False)
                    md.artikel, md.mandant = objekt, mandant
                    md.save()
                    if gruppe:
                        komponenten_speichern(objekt, gewaehlt)
                    if pl_eintrag:
                        kopie.preisliste_verknuepfen(objekt, pl_eintrag)
                    if we_bezug:  # gleiche Transaktion: ist der Wareneingang inzwischen gebucht, wird auch nichts angelegt
                        from apps.einkauf.services import we_artikel_uebernehmen
                        we_artikel_uebernehmen(we_bezug, objekt)
                    bilder_speichern(objekt, bilderplan)  # zuletzt: Dateien erst, wenn alles andere gelungen ist
            except ValueError as grund:  # z. B. Wareneingang inzwischen gebucht: nichts angelegt, Meldung im Formular
                if not pk:
                    form.instance.pk = None
                form.add_error(None, str(grund))
            else:
                if we_bezug:
                    return JsonResponse({"ok": True, "ziel": reverse("einkauf:wareneingang", args=[we_bezug["we"].pk])})
                # Neu: zur Artikelseite; Bearbeiten: auf der Seite bleiben, von der aus geöffnet wurde (z. B. Artikelliste)
                return JsonResponse({"ok": True, "ziel": None if pk else reverse("lager:artikel", args=[objekt.pk])})
        status = 422
    else:
        bildfehler = []
    antwort = render(request, "stammdaten/modal.html", {
        "titel": f"Artikel {instanz.nummer} – {instanz.bezeichnung}" if pk else "Neuer Artikel",
        "tabs": artikel_tabs(form, mandantform, komponenten, bool(form.non_field_errors()),
                             bilder_daten(instanz, bilder_kopie), bool(bildfehler)), "form": form, "neu": not pk, "warnung": "",
        "aktion": request.path, "kunde": False, "mandant": mandant, "kopie_von": vorlage, "kopie_bilder": bool(bilder_kopie),
        "kopie_von_pl": pl_eintrag, "kopie_artikel": instanz if pk else None, "we_para": we_para})
    antwort.status_code = status
    return antwort
