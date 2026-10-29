from functools import wraps

from django.contrib import messages
from django.shortcuts import redirect


def ist_admin(benutzer) -> bool:
    """Admin = Administrator oder Mitarbeiter mit Verwaltungszugang (is_staff); alle anderen sind Mitarbeiter."""
    return bool(benutzer.is_authenticated and (benutzer.is_superuser or benutzer.is_staff))


def nur_admin(view):
    """Nur für Admins. Innerhalb von @mit_mandant verwenden (dort wird der Login geprüft)."""
    @wraps(view)
    def umhuellt(request, *args, **kwargs):
        if not ist_admin(request.user):
            messages.error(request, "Dafür ist die Berechtigung „Admin“ nötig.")
            return redirect(request.META.get("HTTP_REFERER") or "verkauf:start")
        return view(request, *args, **kwargs)
    return umhuellt


def admin_bei_aenderung(view):
    """Ansehen darf jeder angemeldete Benutzer, Änderungen (POST) nur Admins. Innerhalb von @mit_mandant verwenden."""
    @wraps(view)
    def umhuellt(request, *args, **kwargs):
        if request.method not in ("GET", "HEAD", "OPTIONS") and not ist_admin(request.user):
            messages.error(request, "Dafür ist die Berechtigung „Admin“ nötig.")
            return redirect(request.META.get("HTTP_REFERER") or "verkauf:start")
        return view(request, *args, **kwargs)
    return umhuellt
