from django.shortcuts import render
from django.utils import timezone

from apps.verkauf.views import mit_mandant

from . import dashboard as kennzahlen


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
