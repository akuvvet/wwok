"""Gemeinsame Template-Variablen."""

# Bereiche ohne eigenes Hauptmenü gehören zu einem anderen: Shop zu Verkauf, ESD-Keys zu Artikel
BEREICH = {"shop": "verkauf", "esd": "lager"}


def hauptbereich(request):
    treffer = getattr(request, "resolver_match", None)
    namensraum = treffer.namespace if treffer else ""
    return {"hauptbereich": BEREICH.get(namensraum, namensraum)}
