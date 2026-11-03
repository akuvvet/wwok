from django.http import HttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.cache import cache_control

from apps.verkauf.views import mit_mandant

from . import dashboard as kennzahlen


SYMBOL_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect width="32" height="32" rx="7" fill="#0b5cad"/>'
              '<path d="M6 9l4.5 14L16 13l5.5 10L26 9" fill="none" stroke="#fff" stroke-width="3.4" stroke-linecap="round" stroke-linejoin="round"/></svg>')


@cache_control(max_age=86400, public=True)
def favicon(request):
    """Seitensymbol für Seiten ohne eigenes <link rel=icon> (z. B. Verwaltung); ohne Anmeldung abrufbar."""
    return HttpResponse(SYMBOL_SVG, content_type="image/svg+xml")


@mit_mandant
def dashboard(request):
    """Startseite: Kennzahlen des gewählten Mandanten (für alle angemeldeten Benutzer sichtbar)."""
    mandant = request.mandant
    tage = kennzahlen.zeitraum_pruefen(request.GET.get("tage"))
    heute = timezone.localdate()
    von, bis, vorher_von, vorher_bis = kennzahlen.zeitraeume(heute, tage)
    return render(request, "kern/dashboard.html", {
        "tage": tage, "zeitraeume": kennzahlen.ZEITRAEUME, "von": von, "bis": bis,
        "kacheln": kennzahlen.kacheln(mandant, tage, heute),
        "vergleiche": kennzahlen.zeitraumvergleiche(mandant, heute),
        "verlauf": kennzahlen.verlauf(mandant, tage, heute),
        "kunden": kennzahlen.top_kunden(mandant, von, bis),
        "artikel": kennzahlen.top_artikel(mandant, von, bis),
        "lieferanten": kennzahlen.top_lieferanten(mandant, von, bis),
        "kanaele": kennzahlen.kanaele(mandant, von, bis),
        "arbeit": kennzahlen.arbeitsliste(mandant, von, heute)})
