"""Flexible Listen: Jeder Benutzer speichert je Liste seine Ansicht (Spalten, Reihenfolge, Breite)."""
import json

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods

from .models import Listenansicht

SCHLUESSEL_MAX = 200          # Länge des Schlüssels (Seitenpfad | Tabellennummer | Kennzeichen)
EINSTELLUNG_MAX = 8 * 1024    # Größe der Einstellung als JSON in Byte
ANFRAGE_MAX = 16 * 1024       # Größe der ganzen Anfrage
SPALTEN_MAX = 80
ANSICHTEN_JE_BENUTZER_MAX = 500
DICHTEN = ("normal", "kompakt")


def bereinige(einstellung):
    """Prüft die Einstellung und gibt nur die erwarteten Felder zurück; sonst ValueError."""
    if not isinstance(einstellung, dict):
        raise ValueError("Die Einstellung muss ein Objekt sein.")
    sauber = {}
    dichte = einstellung.get("dichte", "normal")
    if dichte not in DICHTEN:
        raise ValueError("Ungültige Zeilendichte.")
    sauber["dichte"] = dichte
    spalten = einstellung.get("spalten", [])
    if not isinstance(spalten, list) or len(spalten) > SPALTEN_MAX:
        raise ValueError("Ungültige Spaltenliste.")
    gesehen = set()
    sauber_spalten = []
    for s in spalten:
        if not isinstance(s, dict):
            raise ValueError("Ungültige Spalte.")
        i, v, b, t = s.get("i"), s.get("v", True), s.get("b"), s.get("t", "")
        if isinstance(i, bool) or not isinstance(i, int) or not 0 <= i < 200 or i in gesehen:
            raise ValueError("Ungültige Spaltennummer.")
        if not isinstance(v, bool):
            raise ValueError("Ungültige Sichtbarkeit.")
        if b is not None and (isinstance(b, bool) or not isinstance(b, int) or not 30 <= b <= 1500):
            raise ValueError("Ungültige Spaltenbreite.")
        if not isinstance(t, str) or len(t) > 80:
            raise ValueError("Ungültiger Spaltentitel.")
        gesehen.add(i)
        sauber_spalten.append({"i": i, "t": t, "v": v, "b": b})
    sauber["spalten"] = sauber_spalten
    if len(json.dumps(sauber)) > EINSTELLUNG_MAX:
        raise ValueError("Die Einstellung ist zu groß.")
    return sauber


def _fehler(text, status=400):
    return JsonResponse({"fehler": text}, status=status)


def _nutzdaten(request):
    """Liest die Anfrage als JSON oder als Formular."""
    if len(request.body) > ANFRAGE_MAX:
        raise ValueError("Die Anfrage ist zu groß.")
    if (request.content_type or "").startswith("application/json"):
        try:
            daten = json.loads(request.body.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            raise ValueError("Ungültiges JSON.")
        if not isinstance(daten, dict):
            raise ValueError("Ungültige Anfrage.")
        return daten
    daten = {"schluessel": request.POST.get("schluessel", ""), "zuruecksetzen": bool(request.POST.get("zuruecksetzen"))}
    if "einstellung" in request.POST:
        try:
            daten["einstellung"] = json.loads(request.POST["einstellung"])
        except ValueError:
            raise ValueError("Ungültiges JSON.")
    return daten


@login_required
@require_http_methods(["GET", "POST"])
def listenansicht(request):
    """GET ?schluessel=… oder ?seite=… liest, POST speichert bzw. setzt zurück – nur die eigenen Ansichten."""
    eigene = Listenansicht.objects.filter(benutzer=request.user)
    if request.method == "GET":
        schluessel, seite = request.GET.get("schluessel"), request.GET.get("seite")
        if schluessel is not None:
            if not schluessel or len(schluessel) > SCHLUESSEL_MAX:
                return _fehler("Ungültiger Schlüssel.")
            ansicht = eigene.filter(schluessel=schluessel).first()
            antwort = JsonResponse({"einstellung": ansicht.einstellung if ansicht else {}})
        elif seite is not None:
            if not seite or len(seite) > SCHLUESSEL_MAX:
                return _fehler("Ungültige Seite.")
            ansichten = {a.schluessel: a.einstellung for a in eigene.filter(schluessel__startswith=seite + "|")}
            antwort = JsonResponse({"ansichten": ansichten})
        else:
            return _fehler("Schlüssel fehlt.")
        antwort["Cache-Control"] = "no-store"
        return antwort

    try:
        daten = _nutzdaten(request)
    except ValueError as fehler:
        return _fehler(str(fehler))
    schluessel = daten.get("schluessel")
    if not isinstance(schluessel, str) or not schluessel or len(schluessel) > SCHLUESSEL_MAX:
        return _fehler("Ungültiger Schlüssel.")
    if daten.get("zuruecksetzen") in (True, "1", "true"):
        eigene.filter(schluessel=schluessel).delete()
        return JsonResponse({"einstellung": {}})
    try:
        einstellung = bereinige(daten.get("einstellung"))
    except ValueError as fehler:
        return _fehler(str(fehler))
    if not eigene.filter(schluessel=schluessel).exists() and eigene.count() >= ANSICHTEN_JE_BENUTZER_MAX:
        return _fehler("Zu viele gespeicherte Ansichten.")
    Listenansicht.objects.update_or_create(
        benutzer=request.user, schluessel=schluessel, defaults={"einstellung": einstellung}
    )
    return JsonResponse({"einstellung": einstellung})
