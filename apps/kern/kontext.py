"""Gemeinsame Template-Variablen und der aktive Mandant der Anfrage."""
from .models import Mandant

# Bereiche ohne eigenes Hauptmenü gehören zu einem anderen: Shop zu Verkauf, ESD-Keys zu Artikel
BEREICH = {"shop": "verkauf", "esd": "lager"}
MANDANT_COOKIE = "wwok_mandant"  # letzter Mandant je Browser, gilt auch nach neuer Anmeldung


def mandant_oder_none(request):
    """Mandant der Sitzung; sonst der zuletzt in diesem Browser gewählte; sonst der einzige aktive."""
    aktive = Mandant.objects.filter(aktiv=True)
    mandant_id = request.session.get("mandant_id")
    if mandant_id:
        mandant = aktive.filter(pk=mandant_id).first()
        if mandant:
            return mandant
    cookie = request.COOKIES.get(MANDANT_COOKIE, "")
    mandant = aktive.filter(pk=cookie).first() if cookie.isdigit() else None
    if mandant is None and aktive.count() == 1:
        mandant = aktive[0]
    if mandant is not None:
        request.session["mandant_id"] = mandant.pk
    return mandant


def hauptbereich(request):
    treffer = getattr(request, "resolver_match", None)
    namensraum = treffer.namespace if treffer else ""
    return {"hauptbereich": BEREICH.get(namensraum, namensraum)}


def aktiver_mandant(request):
    """Für die Kopfzeile: unter welcher Firma gerade gearbeitet wird."""
    benutzer = getattr(request, "user", None)
    if benutzer is None or not benutzer.is_authenticated:
        return {}
    return {"aktiver_mandant": getattr(request, "mandant", None) or mandant_oder_none(request)}
