import os
import uuid

from django.contrib import messages
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.stammdaten.models import Lieferant
from apps.kern.rechte import admin_bei_aenderung, nur_admin
from apps.verkauf.views import mit_mandant

from . import assistent, preislisten
from .models import Dateityp, Preislistenprofil, Quelle


def tmp_speichern(inhalt: bytes, name: str) -> str:
    token = f"{uuid.uuid4().hex}__{os.path.basename(name)}"
    default_storage.save(f"tmp/preislisten/{token}", ContentFile(inhalt))
    return token


def tmp_lesen(token: str):
    pfad = f"tmp/preislisten/{os.path.basename(token or '')}"
    if not token or not default_storage.exists(pfad):
        raise preislisten.PreislistenFehler("Die hochgeladene Datei ist nicht mehr vorhanden – bitte erneut hochladen.")
    with default_storage.open(pfad) as f:
        return f.read(), token.split("__", 1)[-1]


def profil_aus_post(post, lieferant, erkannt) -> Preislistenprofil:
    """Format aus der erkannten Vorgabe, überschrieben durch die Eingaben im Formular."""
    zusatz = {"kopfzeile": post.get("kopfzeile") == "1", "dezimaltrennzeichen": post.get("dezimaltrennzeichen", ",")}
    for feld in ("zeichensatz", "trennzeichen", "xml_satz"):
        if post.get(feld):
            zusatz[feld] = post[feld]
    return assistent.muster_profil(lieferant, erkannt, **zusatz)


@mit_mandant
@nur_admin
def assistent_ansicht(request, profil_id=None):
    """Schritt 1: Datei hochladen. Schritt 2: Spalten zuordnen und als Profil speichern."""
    vorhanden = get_object_or_404(Preislistenprofil.objects.select_related("lieferant"), pk=profil_id) if profil_id else None
    kontext = {"vorhanden": vorhanden, "lieferanten": Lieferant.objects.filter(gesperrt=False), "felder": preislisten.FELDER, "schritt": 1}
    if request.method == "POST":
        aktion = request.POST.get("aktion")
        try:
            lieferant = vorhanden.lieferant if vorhanden else get_object_or_404(Lieferant, pk=request.POST.get("lieferant"))
            if aktion == "datei":
                datei = request.FILES.get("datei")
                if not datei:
                    raise ValueError("Bitte eine Preisliste (CSV, TXT, XLSX, XLS oder XML) wählen.")
                inhalt = datei.read()
                token = tmp_speichern(inhalt, datei.name)
                erkannt = assistent.format_erkennen(datei.name, inhalt)
                profil = assistent.muster_profil(lieferant, erkannt, **({"dezimaltrennzeichen": vorhanden.dezimaltrennzeichen} if vorhanden else {}))
                return zuordnung(request, kontext, profil, inhalt, token, datei.name, erkannt, request.POST.get("name", ""), vorhanden)
            inhalt, dateiname = tmp_lesen(request.POST.get("token"))
            erkannt = assistent.format_erkennen(dateiname, inhalt)
            profil = profil_aus_post(request.POST, lieferant, erkannt)
            if aktion == "aktualisieren":
                return zuordnung(request, kontext, profil, inhalt, request.POST["token"], dateiname, erkannt, request.POST.get("name", ""),
                                 vorhanden, auswahl={f: request.POST.get(f"sp_{f}", "") for f in preislisten.FELDER})
            if aktion == "speichern":
                namen, _ = assistent.spalten_und_beispiele(profil, inhalt)
                zuordnung_ = {f: assistent.spaltenangabe(profil, request.POST.get(f"sp_{f}", "")) for f in preislisten.FELDER}
                if not zuordnung_["bestellnummer"] or not zuordnung_["ek"]:
                    raise ValueError("Bestellnummer und EK-Preis müssen zugeordnet werden.")
                name = request.POST.get("name", "").strip() or f"Preisliste {lieferant.name1}"
                werte = {"name": name[:100], "dateityp": profil.dateityp, "zeichensatz": profil.zeichensatz,
                         "trennzeichen": profil.trennzeichen, "textbegrenzer": profil.textbegrenzer or '"',
                         "dezimaltrennzeichen": profil.dezimaltrennzeichen, "kopfzeile": profil.kopfzeile,
                         "erste_datenzeile": 2 if profil.kopfzeile else 1, "xml_satz": profil.xml_satz,
                         "neue_artikel": request.POST.get("neue_artikel") == "1",
                         "entfernen_veraltete": request.POST.get("entfernen_veraltete") == "1",
                         **{f"sp_{f}": v for f, v in zuordnung_.items()}}
                if vorhanden:
                    for k, v in werte.items():
                        setattr(vorhanden, k, v)
                    vorhanden.save()
                    gespeichert = vorhanden
                else:
                    gespeichert = Preislistenprofil.objects.create(lieferant=lieferant, quelle=Quelle.UPLOAD, **werte)
                messages.success(request, "Profil gespeichert – hier die Vorschau mit Ihren Einstellungen.")
                return redirect(f"{gespeichert_url(gespeichert)}?token={request.POST['token']}")
        except (ValueError, preislisten.PreislistenFehler) as fehler:
            messages.error(request, str(fehler))
    return render(request, "einkauf/assistent.html", kontext)


def gespeichert_url(profil):
    from django.urls import reverse
    return reverse("einkauf:preisliste", args=[profil.pk])


def zuordnung(request, kontext, profil, inhalt, token, dateiname, erkannt, name, vorhanden, auswahl=None):
    namen, beispiele = assistent.spalten_und_beispiele(profil, inhalt)
    if auswahl is None:
        if vorhanden and vorhanden.kopfzeile == profil.kopfzeile:
            auswahl = {f: getattr(vorhanden, f"sp_{f}") for f in preislisten.FELDER}
            if not profil.kopfzeile:
                auswahl = {f: (f"Spalte {v}" if v else "") for f, v in auswahl.items()}
        else:
            auswahl = assistent.spalten_raten(namen)
    kontext.update(schritt=2, profil=profil, namen=namen, beispiele=beispiele, token=token, dateiname=dateiname, auswahl=auswahl,
                   name=name or (vorhanden.name if vorhanden else ""), erkannt=erkannt,
                   lieferant=profil.lieferant, neue_artikel=vorhanden.neue_artikel if vorhanden else True,
                   entfernen_veraltete=vorhanden.entfernen_veraltete if vorhanden else True)
    return render(request, "einkauf/assistent.html", kontext)


@mit_mandant
@admin_bei_aenderung
def artikelpreise(request):
    """EK/VK-Preise je Artikel aus Excel oder CSV einlesen (Vorschau, dann übernehmen)."""
    kontext = {"schritt": 1, "preisfelder": assistent.PREISFELDER, "schluessel": assistent.SCHLUESSEL}
    if request.method == "POST":
        aktion = request.POST.get("aktion")
        try:
            if aktion == "datei":
                datei = request.FILES.get("datei")
                if not datei:
                    raise ValueError("Bitte eine Datei (XLSX, XLS, CSV oder TXT) wählen.")
                inhalt = datei.read()
                erkannt = assistent.format_erkennen(datei.name, inhalt)
                if erkannt["dateityp"] == Dateityp.XML:
                    raise ValueError("XML wird für Artikelpreise nicht unterstützt – bitte Excel oder CSV verwenden.")
                token = tmp_speichern(inhalt, datei.name)
                profil = assistent.muster_profil(None, erkannt)
                namen, beispiele = assistent.spalten_und_beispiele(profil, inhalt)
                raten = assistent.spalten_raten(namen)
                auswahl = {"schluessel_spalte": raten.get("bestellnummer", ""), "ek": raten.get("ek", "")}
                kontext.update(schritt=2, token=token, dateiname=datei.name, namen=namen, beispiele=beispiele, auswahl=auswahl,
                               profil=profil, schluessel_art="nummer")
            else:
                inhalt, dateiname = tmp_lesen(request.POST.get("token"))
                erkannt = assistent.format_erkennen(dateiname, inhalt)
                profil = profil_aus_post(request.POST, None, erkannt)
                auswahl = {"schluessel_spalte": request.POST.get("schluessel_spalte", ""),
                           **{f: request.POST.get(f"sp_{f}", "") for f in assistent.PREISFELDER}}
                ergebnis = assistent.artikelpreise(
                    profil, inhalt, request.POST.get("schluessel_art", "nummer"), auswahl["schluessel_spalte"],
                    {f: auswahl[f] for f in assistent.PREISFELDER}, request.POST.get("brutto") == "1", anwenden=aktion == "anwenden")
                namen, beispiele = assistent.spalten_und_beispiele(profil, inhalt)
                kontext.update(schritt=2, token=request.POST["token"], dateiname=dateiname, namen=namen, beispiele=beispiele,
                               auswahl=auswahl, profil=profil, schluessel_art=request.POST.get("schluessel_art", "nummer"),
                               brutto=request.POST.get("brutto") == "1", ergebnis=ergebnis, angewendet=aktion == "anwenden")
                if aktion == "anwenden":
                    default_storage.delete(f"tmp/preislisten/{os.path.basename(request.POST['token'])}")
                    messages.success(request, f"{ergebnis['geaendert']} Artikel aktualisiert.")
        except (ValueError, preislisten.PreislistenFehler) as fehler:
            messages.error(request, str(fehler))
            kontext["schritt"] = 1 if aktion == "datei" else kontext.get("schritt", 1)
    return render(request, "einkauf/artikelpreise.html", kontext)
