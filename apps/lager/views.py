from collections import Counter
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.db import transaction
from django.db.models import Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.kern.models import Mandant
from apps.kern.seiten import blaettern
from apps.shop.models import Shop, ShopArtikel
from apps.stammdaten.models import Artikel, ArtikelMandant, Kategorie, _ek_quelle, ek_je_artikel, keine_leistung, kennungen_filter
from apps.stammdaten.preise import gruppen_kalkulation
from apps.verkauf.views import mit_mandant

from apps.verkauf.models import Beleg, Position

from .models import Bewegungsart, Lagerbewegung, Reparaturbewegung, Seriennummer, bestand, reparaturbestand, reserviert, reserviert_je_artikel
from .services import (buche_manuell, inventur, reparatur_ausbuchen, reparatur_umbuchen, retoure_aus_gutschrift,
                       seriennummer_wieder_einlagern, seriennummern_einlagern, seriennummern_zuordnen, umbuchen, zeilen)

MANUELL = [Bewegungsart.WARENEINGANG, Bewegungsart.EIGENVERBRAUCH, Bewegungsart.RETOURE, Bewegungsart.KORREKTUR]


def zahl(text, standard=None):
    text = (text or "").strip().replace(",", ".")
    if not text:
        return standard
    try:
        wert = Decimal(text)
    except InvalidOperation:
        raise ValueError(f"Ungültige Zahl: {text}")
    if not wert.is_finite():
        raise ValueError(f"Ungültige Zahl: {text}")
    return wert


def suche(q, artikel=None, mandant=None):
    """Artikelsuche der Listen (Artikel & Bestand, Inventur): dieselbe Syntax wie in der Belegmaske (apps/stammdaten/suche.py):
    Wörter in beliebiger Reihenfolge in Nummer, Bezeichnung, Herstellernummer, Hersteller oder einer Kennung (EAN, ASIN, SKU,
    FNSKU, ISBN, UPC, eBay-ID), „*“ = alle, „%“ wie LIKE, „>5“ = Lagerbestand des Mandanten über 5."""
    from apps.stammdaten.suche import artikel_filtern
    artikel = Artikel.objects.filter(nicht_aktiv=False) if artikel is None else artikel
    return artikel_filtern(artikel, q, mandant)




def kategorien_baum():
    """Alle Kategorien als flache Liste (Hauptkategorie, darunter eingerückt die Unterkategorien) für das Filter-Auswahlfeld."""
    alle = list(Kategorie.objects.order_by("name"))
    kinder = {}
    for k in alle:
        kinder.setdefault(k.eltern_id, []).append(k)
    ergebnis, gesehen = [], set()

    def anhaengen(eltern_id, tiefe):
        for k in kinder.get(eltern_id, []):
            if k.pk in gesehen:  # Schutz gegen Zyklen
                continue
            gesehen.add(k.pk)
            k.einrueckung = "  " * (tiefe - 1) + "– " if tiefe else ""
            ergebnis.append(k)
            anhaengen(k.pk, tiefe + 1)

    anhaengen(None, 0)
    return ergebnis


def nach_kategorie(artikel, kategorie):
    """Gewählte Kategorie samt Unterkategorien (bis zu drei Ebenen); leer oder ungültig = kein Filter."""
    if not kategorie.isdigit():
        return artikel
    ids = {int(kategorie)}
    for _ in range(3):
        ids |= set(Kategorie.objects.filter(eltern_id__in=ids).values_list("pk", flat=True))
    # Haupt- oder weitere Kategorie; distinct, weil ein Artikel über mehrere Kategorien des Baums zweimal vorkommen kann
    return artikel.filter(Q(kategorie_id__in=ids) | Q(weitere_kategorien__in=ids)).distinct()


@mit_mandant
def uebersicht(request):
    q = request.GET.get("q", "").strip()
    nur_bestand = request.GET.get("bestand") == "1"
    nicht_aktiv = request.GET.get("nicht_aktiv") == "1"  # Haken: nur die als nicht aktiv markierten Artikel
    kategorie = request.GET.get("kategorie", "")
    summen = {s["artikel"]: s["s"] for s in Lagerbewegung.objects.filter(mandant=request.mandant)
              .values("artikel").annotate(s=Sum("menge"))}
    reparatur = {s["artikel"]: s["s"] for s in Reparaturbewegung.objects.filter(mandant=request.mandant)
                 .values("artikel").annotate(s=Sum("menge"))}
    basis = Artikel.objects.filter(nicht_aktiv=nicht_aktiv)
    artikel = nach_kategorie(suche(q, basis, request.mandant) if q else basis, kategorie)
    if nur_bestand:
        artikel = artikel.filter(pk__in=[a for a, s in (*summen.items(), *reparatur.items()) if s])
    seite, parameter = blaettern(request, artikel.select_related("steuersatz").prefetch_related("bilder").order_by("nummer"))
    zeilen = []
    eks = ek_je_artikel(seite.object_list)
    reserviert_summen = reserviert_je_artikel(request.mandant, [a.pk for a in seite.object_list if summen.get(a.pk)])
    vk_feld = f"vk{request.mandant.standard_vk}"  # Standard-VK des Mandanten (Verwaltung → Mandanten)
    # dieselbe Regel wie Artikel.objects.shop_sichtbar(): verdeckt nur, wenn alle Kategorien (Haupt- und weitere) offline sind
    wegen_kategorie = set(Artikel.objects.wegen_kategorie_offline().filter(pk__in=[a.pk for a in seite.object_list]).values_list("pk", flat=True))
    shops = list(Shop.objects.filter(mandant=request.mandant, aktiv=True).values_list("pk", flat=True))
    im_shop = Counter(ShopArtikel.objects.filter(shop_id__in=shops, im_shop_aktiv=True, artikel__in=list(seite.object_list))
                      .values_list("artikel_id", flat=True))  # in wie vielen Shops des Mandanten schon gemeldet
    for a in seite.object_list:
        b = summen.get(a.pk, Decimal("0"))
        r = reserviert_summen.get(a.pk, Decimal("0")) if b else Decimal("0")
        ek, quelle = eks[a.pk]
        if not a.online:
            online = ("nein", "nicht online")
        elif a.nicht_aktiv:
            online = ("verdeckt", "online gesetzt, aber nicht im Shop: Artikel ist nicht aktiv")
        elif a.pk in wegen_kategorie:
            online = ("verdeckt", "online gesetzt, aber nicht im Shop: Kategorie ist offline")
        elif a.typ in ("A", "G") and im_shop[a.pk] < len(shops):  # nur diese Typen gehen an den Shop
            online = ("verdeckt", "online gesetzt, aber noch nicht im Shop sichtbar: noch nicht mit dem Shop abgeglichen")
        else:
            online = ("ja", "online – im Shop sichtbar")
        zeilen.append({"artikel": a, "online": online, "vk": getattr(a, vk_feld), "bestand": b, "reserviert": r, "verfuegbar": max(b - r, Decimal("0")), "reparatur": reparatur.get(a.pk, Decimal("0")),
                       "ek": ek, "ek_quelle": quelle})
    lagerwert = Decimal("0")  # mit demselben EK wie der VK: Import-EK, sonst manueller EK (README 18)
    mit_bestand = list(Artikel.objects.filter(pk__in=[k for k, s in summen.items() if s]))
    for pk, (ek, _) in ek_je_artikel(mit_bestand).items():
        if ek is not None:
            lagerwert += summen[pk] * ek
    return render(request, "lager/uebersicht.html", {
        "zeilen": zeilen, "q": q, "nur_bestand": nur_bestand, "nicht_aktiv": nicht_aktiv, "lagerwert": lagerwert.quantize(Decimal("0.01")),
        "seite": seite, "parameter": parameter, "kategorie": kategorie, "kategorien": kategorien_baum()})


@mit_mandant
def artikel_detail(request, artikel_id):
    artikel = get_object_or_404(Artikel, pk=artikel_id)
    daten = ArtikelMandant.objects.filter(artikel=artikel, mandant=request.mandant).first()
    b = bestand(request.mandant, artikel)
    r = reserviert(request.mandant, artikel)
    gruppe = None
    if artikel.typ == "G":  # Bestand einer Gruppe ergibt sich aus den Komponenten
        from .models import gruppen_verfuegbar
        gruppe = gruppen_verfuegbar(request.mandant, artikel)
    from apps.einkauf.models import WareneingangPosition
    from apps.einkauf.services import offen_bestellt
    letzter_we = WareneingangPosition.objects.filter(
        artikel=artikel, wareneingang__mandant=request.mandant, wareneingang__gebucht=True
    ).select_related("wareneingang__lieferant").order_by("-wareneingang__datum", "-id").first()
    ek, ek_quelle = _ek_quelle(artikel)
    from apps.stammdaten import historie as artikel_historie
    from apps.stammdaten.preise import stufenpreis
    eingabe_am, eingabe_von = artikel_historie.eingabe(artikel)
    vk1 = stufenpreis(artikel, 1)  # wirksamer VK1 netto (fest oder aus EK + Aufschlag), je Preiseinheit
    stueck = vk1 / Decimal(artikel.preiseinheit or 1) if vk1 is not None else None  # Grundpreis wie im Beleg aus dem Stückpreis
    brutto = stueck * (1 + artikel.steuersatz.prozent / 100) if stueck is not None else None
    return render(request, "lager/artikel.html", {
        "eingabe_am": eingabe_am, "eingabe_von": eingabe_von, "historie": artikel_historie.historie(artikel, request.mandant),
        "historie_max": artikel_historie.HISTORIE_MAX, "weitere_kategorien": artikel.weitere_kategorien.order_by("name"),
        "grundpreis_netto": artikel.grundpreis_text(stueck), "grundpreis_brutto": artikel.grundpreis_text(brutto),
        "ek": ek, "ek_quelle": ek_quelle, "ek_datum": artikel.ek_datum(ek_quelle),
        "bestellt": offen_bestellt(request.mandant, artikel), "letzter_we": letzter_we,
        "lieferanten": artikel.lieferanten.select_related("lieferant").order_by("ek"),
        "gruppe_verfuegbar": gruppe, "kalkulation": gruppen_kalkulation(artikel) if artikel.typ == "G" else None,
        "artikel": artikel, "daten": daten, "bestand": b, "reserviert": r, "verfuegbar": max(b - r, Decimal("0")),
        "bewegungen": Lagerbewegung.objects.filter(mandant=request.mandant, artikel=artikel).select_related("beleg", "benutzer")[:100],
        "reparatur": reparaturbestand(request.mandant, artikel),
        "reparaturbewegungen": Reparaturbewegung.objects.filter(mandant=request.mandant, artikel=artikel).select_related("beleg", "benutzer")[:100],
        "arten": [(a.value, a.label) for a in MANUELL],
        "seriennummern": Seriennummer.objects.filter(mandant=request.mandant, artikel=artikel).select_related(
            "position__beleg", "wareneingang__lieferant")[:200],
        # Rückverfolgung ohne Seriennummer: von welchem Lieferanten kam der Artikel zuletzt
        "wareneingaenge": WareneingangPosition.objects.filter(
            artikel=artikel, wareneingang__mandant=request.mandant, wareneingang__gebucht=True
        ).select_related("wareneingang__lieferant").order_by("-wareneingang__datum", "-id")[:20],
        "andere_mandanten": Mandant.objects.filter(aktiv=True).exclude(pk=request.mandant.pk)})


@mit_mandant
@require_POST
def buchen(request, artikel_id):
    artikel = get_object_or_404(Artikel, pk=artikel_id)
    try:
        keine_leistung(artikel)
        menge = zahl(request.POST.get("menge"))
        if menge is None:
            raise ValueError("Menge fehlt.")
        art = request.POST.get("art")
        if art not in [a.value for a in MANUELL]:
            raise ValueError("Diese Buchungsart ist hier nicht erlaubt.")
        seriennummern = zeilen(request.POST.get("seriennummern"))
        if seriennummern and art not in (Bewegungsart.WARENEINGANG, Bewegungsart.RETOURE):
            raise ValueError("Seriennummern nur bei Wareneingang oder Retoure.")
        if seriennummern and len(seriennummern) != abs(menge):
            raise ValueError(f"{len(seriennummern)} Seriennummern, aber Menge {abs(menge):f}.")
        with transaction.atomic():  # schlägt die Seriennummer fehl, wird auch die Bestandsbuchung zurückgenommen
            ek = zahl(request.POST.get("ek"))
            buche_manuell(request.mandant, artikel, art, menge, request.POST.get("bemerkung", "").strip(), request.user, ek=ek)
            if seriennummern:
                seriennummern_einlagern(request.mandant, artikel, chr(10).join(seriennummern), request.POST.get("bemerkung", "").strip())
        messages.success(request, "Gebucht.")
    except ValueError as fehler:
        messages.error(request, str(fehler))
    return redirect("lager:artikel", artikel_id=artikel.pk)


@mit_mandant
@require_POST
def inventur_buchen(request, artikel_id):
    artikel = get_object_or_404(Artikel, pk=artikel_id)
    try:
        keine_leistung(artikel)
        gezaehlt = zahl(request.POST.get("gezaehlt"))
        if gezaehlt is None or gezaehlt < 0:
            raise ValueError("Gezählter Bestand fehlt.")
        buchung = inventur(request.mandant, artikel, gezaehlt, request.user)
        messages.success(request, f"Differenz {buchung.menge:+} gebucht." if buchung else "Keine Differenz.")
    except ValueError as fehler:
        messages.error(request, str(fehler))
    return redirect("lager:artikel", artikel_id=artikel.pk)


@mit_mandant
@require_POST
def stamm(request, artikel_id):
    """Lagerplatz und Mindestbestand pro Mandant."""
    artikel = get_object_or_404(Artikel, pk=artikel_id)
    try:
        keine_leistung(artikel)
    except ValueError as fehler:
        messages.error(request, str(fehler))
        return redirect("lager:artikel", artikel_id=artikel.pk)
    daten, _ = ArtikelMandant.objects.get_or_create(artikel=artikel, mandant=request.mandant)
    daten.lagerplatz = request.POST.get("lagerplatz", "").strip()[:40]
    mindest = request.POST.get("mindestbestand", "").strip()
    daten.mindestbestand = int(mindest) if mindest.isdigit() else None
    daten.save()
    messages.success(request, "Gespeichert.")
    return redirect("lager:artikel", artikel_id=artikel.pk)


@mit_mandant
@require_POST
def umbuchung(request, artikel_id):
    artikel = get_object_or_404(Artikel, pk=artikel_id)
    try:
        keine_leistung(artikel)
        ziel = get_object_or_404(Mandant, pk=request.POST.get("nach"), aktiv=True)
        umbuchen(request.mandant, ziel, artikel, zahl(request.POST.get("menge"), Decimal("0")), request.user,
                 zeilen(request.POST.get("seriennummern")))
        messages.success(request, f"Nach {ziel.kurzname} umgebucht.")
    except ValueError as fehler:
        messages.error(request, str(fehler))
    return redirect("lager:artikel", artikel_id=artikel.pk)


@mit_mandant
@require_POST
def reparatur_buchen(request, artikel_id):
    """Reparaturbestand abbauen: repariert in den Lagerbestand oder ausbuchen (Schrott, Rückgabe an den Lieferanten)."""
    artikel = get_object_or_404(Artikel, pk=artikel_id)
    try:
        keine_leistung(artikel)
        menge = zahl(request.POST.get("menge"), Decimal("0"))
        bemerkung = request.POST.get("bemerkung", "").strip()[:200]
        aktion = request.POST.get("aktion")
        if aktion == "lager":
            reparatur_umbuchen(request.mandant, artikel, menge, bemerkung, request.user)
            messages.success(request, "In den Lagerbestand umgebucht.")
        elif aktion == "ausbuchen":
            reparatur_ausbuchen(request.mandant, artikel, menge, bemerkung, request.user)
            messages.success(request, "Aus dem Reparaturbestand ausgebucht.")
        else:
            raise ValueError("Diese Buchungsart ist hier nicht erlaubt.")
    except ValueError as fehler:
        messages.error(request, str(fehler))
    return redirect("lager:artikel", artikel_id=artikel.pk)


@mit_mandant
@require_POST
def seriennummer_einlagern(request, seriennummer_id):
    sn = get_object_or_404(Seriennummer.objects.select_related("position__beleg"), pk=seriennummer_id, mandant=request.mandant)
    try:
        seriennummer_wieder_einlagern(sn)
        messages.success(request, f"Seriennummer {sn.nummer} ist wieder im Lager. Der Bestand wurde nicht verändert.")
    except ValueError as fehler:
        messages.error(request, str(fehler))
    return redirect("lager:artikel", artikel_id=sn.artikel_id)


@mit_mandant
def seriennummer_suche(request):
    q = request.GET.get("q", "").strip()
    treffer = Seriennummer.objects.filter(mandant=request.mandant, nummer__icontains=q).select_related(
        "artikel", "position__beleg__kunde", "wareneingang__lieferant")[:100] if q else []
    from apps.werkstatt.models import Reparatur
    reparaturen = Reparatur.objects.filter(mandant=request.mandant, seriennummer__icontains=q).select_related("kunde")[:50] if q else []
    return render(request, "lager/seriennummern.html", {"q": q, "treffer": treffer, "reparaturen": reparaturen})


@mit_mandant
@require_POST
def position_seriennummern(request, position_id):
    position = get_object_or_404(Position.objects.select_related("beleg", "artikel"), pk=position_id,
                                 beleg__mandant=request.mandant)
    try:
        if not position.artikel_id:
            raise ValueError("Nur bei Artikelpositionen.")
        position.beleg.stand_pruefen()
        seriennummern_zuordnen(position, request.POST.get("seriennummern", ""))
        messages.success(request, "Seriennummern gespeichert.")
    except ValueError as fehler:
        messages.error(request, str(fehler))
    from apps.verkauf.vorgang import aktiv_melden
    aktiv_melden(position.beleg)  # nicht gespeicherter Beleg: Seriennummern erfassen zählt als Aktivität
    return redirect("verkauf:beleg", beleg_id=position.beleg_id)


@mit_mandant
@require_POST
def gutschrift_retoure(request, beleg_id):
    beleg = get_object_or_404(Beleg, pk=beleg_id, mandant=request.mandant)
    try:
        beleg.vorlaeufig_sperre("Die Retoure geht")
        anzahl = retoure_aus_gutschrift(beleg, request.user)
        messages.success(request, f"{anzahl} Artikel ins Lager zurückgebucht.")
    except ValueError as fehler:
        messages.error(request, str(fehler))
    return redirect("verkauf:beleg", beleg_id=beleg.pk)


def _inventur_zeilen(request):
    """Zeilen der Zählliste nach Suche, Kategorie und Haken „Nicht aktiv“ (ganze Liste, ohne Blättern)."""
    summen = {s["artikel"]: s["s"] for s in Lagerbewegung.objects.filter(mandant=request.mandant)
              .values("artikel").annotate(s=Sum("menge"))}
    plaetze = {d.artikel_id: d.lagerplatz for d in ArtikelMandant.objects.filter(mandant=request.mandant)}
    q = request.GET.get("q", "").strip()
    kategorie = request.GET.get("kategorie", "")
    nicht_aktiv = request.GET.get("nicht_aktiv") == "1"
    lagerartikel = Artikel.objects.filter(typ="A")
    alle = lagerartikel.filter(pk__in=[a for a, s in summen.items() if s])
    artikel = nach_kategorie(suche(q, lagerartikel, request.mandant) if q else alle, kategorie).filter(nicht_aktiv=nicht_aktiv)
    zeilen_ = sorted(({"artikel": a, "soll": summen.get(a.pk) or Decimal("0"), "platz": plaetze.get(a.pk, "")}
                      for a in artikel.select_related("kategorie")),
                     key=lambda z: (z["artikel"].nummer.lower() != q.lower(), z["platz"], z["artikel"].nummer))
    return zeilen_, q, kategorie, nicht_aktiv


def _inventur_ausgabe(request, ausgabe, zeilen_, q, kategorie, nicht_aktiv):
    """Zählliste drucken (PDF, nur die Liste) bzw. als CSV/Excel exportieren (Wunsch des Nutzers 26.11.2026)."""
    from django.http import HttpResponse

    from .inventur_datei import csv_export, xlsx_export, zaehlliste_pdf
    filter_text = " · ".join(t for t in (
        f"Suche „{q}“" if q else "",
        f"Kategorie {Kategorie.objects.filter(pk=kategorie).first()}" if kategorie.isdigit() else "",
        "nur nicht aktive Artikel" if nicht_aktiv else "") if t)
    name = f"zaehlliste-{request.mandant.kurzname}-{timezone.localdate():%Y-%m-%d}".lower()
    if ausgabe == "pdf":
        antwort = HttpResponse(zaehlliste_pdf(zeilen_, request.mandant, filter_text), content_type="application/pdf")
        antwort["Content-Disposition"] = f'inline; filename="{name}.pdf"'
    elif ausgabe == "csv":
        antwort = HttpResponse(csv_export(zeilen_), content_type="text/csv; charset=utf-8")
        antwort["Content-Disposition"] = f'attachment; filename="{name}.csv"'
    else:
        antwort = HttpResponse(xlsx_export(zeilen_, request.mandant),
                               content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        antwort["Content-Disposition"] = f'attachment; filename="{name}.xlsx"'
    return antwort


@mit_mandant
@require_POST
def inventur_import(request):
    """Gezählte Mengen aus CSV/Excel: zeigt eine Vorschau mit Soll, gezählt und Differenz; gebucht wird erst dort
    über „Differenzen buchen“ (derselbe Weg wie in der Zählliste, mit Prüfung auf Buchungen seit dem Einlesen)."""
    from .inventur_datei import InventurImportFehler, import_lesen
    datei = request.FILES.get("datei")
    if datei is None:
        messages.error(request, "Bitte eine CSV- oder Excel-Datei auswählen.")
        return redirect("lager:inventur_liste")
    try:
        mengen, fehler, anzahl = import_lesen(datei.name, datei.read())
    except InventurImportFehler as f:
        messages.error(request, str(f))
        return redirect("lager:inventur_liste")
    gefunden = {a.nummer.lower(): a for a in Artikel.objects.filter(nummer__in=list(mengen))}
    for n in [n for n in mengen if n.lower() not in gefunden]:  # Groß-/Kleinschreibung der Datei egal
        a = Artikel.objects.filter(nummer__iexact=n).first()
        if a is not None:
            gefunden[n.lower()] = a
    je_artikel = {}
    for nummer, menge in mengen.items():
        a = gefunden.get(nummer.lower())
        if a is None:
            fehler.append(f"{nummer}: Artikel nicht gefunden")
        elif a.typ != "A":
            fehler.append(f"{nummer}: kein Lagerartikel (Leistung oder Gruppe), wird nicht gezählt")
        else:
            z = je_artikel.setdefault(a.pk, {"artikel": a, "gezaehlt": Decimal("0"), "zeilen": 0})
            z["gezaehlt"] += menge
            z["zeilen"] += anzahl.get(nummer, 1)
    zeilen_ = sorted(je_artikel.values(), key=lambda z: z["artikel"].nummer)
    for z in zeilen_:
        z["soll"] = bestand(request.mandant, z["artikel"])
        z["differenz"] = z["gezaehlt"] - z["soll"]
    return render(request, "lager/inventur_import.html", {
        "zeilen": zeilen_, "fehler": fehler, "dateiname": datei.name,
        "abweichungen": sum(1 for z in zeilen_ if z["differenz"])})


@mit_mandant
def inventur_liste(request):
    """Zählliste mit Erfassung: gezählte Mengen eintragen, Differenzen werden gebucht.
    Standard: nur aktive Artikel; Haken „Nicht aktiv“ zeigt nur die als nicht aktiv markierten.
    Ohne Suchbegriff stehen nur Artikel mit Bestand in der Liste (Zählliste zum Drucken). Mit Suchbegriff auch Artikel mit Bestand 0,
    damit sich ein Artikel einzählen lässt, der laut System nicht da ist; ein Treffer auf die Artikelnummer steht oben.
    Suche, Kategorie und dieser Haken grenzen nur die Anzeige ein; gebucht wird jede eingetragene Zählung,
    auch wenn der Artikel nach einem Filterwechsel gerade nicht angezeigt wird (die Maske trägt die Werte mit)."""
    lagerartikel = Artikel.objects.filter(typ="A")
    if request.method == "POST":
        gebucht = 0
        gesendet = [k[9:] for k, v in request.POST.items() if k.startswith("gezaehlt_") and k[9:].isdigit() and v.strip()]
        for a in lagerartikel.filter(pk__in=gesendet).order_by("nummer"):
            wert = request.POST.get(f"gezaehlt_{a.pk}", "").strip()
            if wert:
                try:
                    gebucht += bool(inventur(request.mandant, a, zahl(wert), request.user, soll=zahl(request.POST.get(f"soll_{a.pk}"))))
                except ValueError as fehler:
                    messages.error(request, f"{a.nummer}: {fehler}")
        messages.success(request, f"{gebucht} Differenzbuchung(en) erstellt.")
        return redirect(request.get_full_path())
    zeilen_, q, kategorie, nicht_aktiv = _inventur_zeilen(request)
    ausgabe = request.GET.get("ausgabe", "")
    if ausgabe in ("pdf", "csv", "xlsx"):
        return _inventur_ausgabe(request, ausgabe, zeilen_, q, kategorie, nicht_aktiv)
    # Blättern wie in allen Tabellen (Wunsch des Nutzers 20.11.2026); eingetragene Zählungen merkt sich die Seite im Browser
    seite, parameter = blaettern(request, zeilen_, 100)
    return render(request, "lager/inventur.html", {
        "zeilen": seite.object_list, "seite": seite, "parameter": parameter, "q": q, "kategorie": kategorie,
        "nicht_aktiv": nicht_aktiv, "kategorien": kategorien_baum()})


@mit_mandant
def artikel_details(request):
    """Angaben für den Detailbereich des Auswahlfensters (Wunsch des Nutzers 19.11.2026): Hauptbild, Herstellernummer, Hersteller,
    EAN, Kategorie und Online-Status. Eigener Artikel über ?nummer=…, Preislistenartikel über ?pl=…"""
    from django.http import JsonResponse
    from apps.stammdaten.models import Preislistenartikel
    pl = request.GET.get("pl", "")
    if pl.isdigit():
        e = get_object_or_404(Preislistenartikel.objects.select_related("lieferant"), pk=pl)
        return JsonResponse({"bild": "", "felder": [
            ["Herstellernummer", e.herstellernummer], ["Hersteller", e.hersteller], ["EAN", e.ean],
            ["Kategorie", e.kategorie and f"{e.kategorie} (laut {e.lieferant.kurz})"], ["Online", "– (Preislistenartikel)"],
            ["Lieferant", f"{e.lieferant.kurz}, Bestand {e.bestand if e.bestand is not None else 'unbekannt'}"]]})
    a = get_object_or_404(Artikel.objects.select_related("hersteller", "kategorie"), nummer=request.GET.get("nummer", ""))
    bild = a.bilder.order_by("position", "pk").first()
    url = ""
    if bild is not None:
        datei = bild.vorschau or bild.bild
        url = datei.url if datei else ""
    if a.nicht_aktiv:
        online = "nicht aktiv"
    elif not a.online:
        online = "offline"
    elif Artikel.objects.filter(pk=a.pk).shop_sichtbar().exists():
        online = "online"
    else:
        online = "online-Haken gesetzt, aber alle Kategorien offline"
    kategorien = [str(k) for k in ([a.kategorie] if a.kategorie_id else []) + list(a.weitere_kategorien.all())]
    return JsonResponse({"bild": url, "felder": [
        ["Herstellernummer", a.herstellernummer], ["Hersteller", a.hersteller.name if a.hersteller_id else ""], ["EAN", a.ean],
        ["Kategorie", " · ".join(kategorien)], ["Online", online],
        ["Erweiterte Bezeichnung", a.bezeichnung_erweitert.strip()]]})  # Wunsch des Nutzers 19.11.2026
