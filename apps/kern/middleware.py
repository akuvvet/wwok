"""Merkt sich den angemeldeten Benutzer pro Anfrage, damit Modelle ihn beim Speichern eintragen können."""
import threading

_lokal = threading.local()


def aktueller_benutzer():
    return getattr(_lokal, "benutzer", None)


class AktuellerBenutzerMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        _lokal.benutzer = getattr(request, "user", None)
        try:
            return self.get_response(request)
        finally:
            _lokal.benutzer = None
