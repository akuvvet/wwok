"""Preis-Radar: Liste je Kategorie, nach Euro-Wert absteigend (Klick auf die Dashboard-Kachel). Issue 12, Schritt 3."""
from django.contrib import messages
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from apps.kern.rechte import ist_admin
from apps.verkauf.views import mit_mandant

from . import radar
from .models import Artikel


@mit_mandant
def radar_liste(request):
    kategorie = request.GET.get("kategorie", "verlust")
    if kategorie != "senkung" and kategorie not in radar.NAMEN:
        raise Http404("Unbekannte Kategorie")
    if kategorie in radar.NUR_LISTE:
        raise Http404("Diese Kategorie steht nur in der Artikelliste")
    if request.method == "POST":
        aktion, pk = request.POST.get("aktion", ""), request.POST.get("artikel", "")
        artikel = get_object_or_404(Artikel, pk=pk) if pk.isdigit() else None
        if artikel is None:
            messages.error(request, "Artikel nicht gefunden.")
        elif aktion == "gesehen":
            radar.gesehen_setzen(artikel, request.mandant)
            messages.success(request, f"{artikel.nummer} ausgeblendet, bis sich EK oder VK ändern.")
        elif aktion == "vk_uebernehmen":
            if not ist_admin(request.user):
                messages.error(request, "Dafür ist die Berechtigung „Admin“ nötig.")
            else:
                try:
                    vk = radar.vk_senkung_uebernehmen(artikel, request.mandant)
                    messages.success(request, f"VK1 von {artikel.nummer} auf {vk:.2f} € gesetzt.".replace(".", ","))
                except ValueError as fehler:
                    messages.error(request, str(fehler))
        return redirect(f"{reverse('stammdaten:radar')}?kategorie={kategorie}")
    kacheln = radar.auswertung(request.mandant)
    return render(request, "stammdaten/radar.html", {
        "kategorie": kategorie, "kacheln": kacheln, "zeilen": radar.zeilen(request.mandant, kategorie),
        "titel": "VK-Senkung prüfen" if kategorie == "senkung" else radar.NAMEN[kategorie]})
