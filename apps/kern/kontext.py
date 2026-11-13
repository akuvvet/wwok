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
    if treffer and not namensraum and treffer.url_name == "dashboard":
        namensraum = "dashboard"  # Startseite ohne Namensraum
    return {"hauptbereich": BEREICH.get(namensraum, namensraum)}


# Pfad im Kopfbereich: Bereich › Menüpunkt (wie in der Seitenleiste). Je Namensraum der Bereich und die Menüpunkte mit den
# URL-Namen, die zu ihnen gehören („name*“ = Präfix, „*“ = alle übrigen). Reihenfolge zählt: der erste Treffer gewinnt.
PFAD = {
    "verkauf": ("Verkauf", [("Alle Belege", "verkauf:belege", ["belege"]),
                            ("Wiederkehrende Rechnungen", "verkauf:abos", ["abo*"]),
                            ("Kunde", "verkauf:start", ["*"])]),
    "shop": ("Verkauf", [("Shops & Online-Bestellungen", "shop:uebersicht", ["*"])]),
    "einkauf": ("Einkauf", [("Lieferant", "einkauf:lieferanten", ["lieferant*"]),
                            ("Bestellvorschlag", "einkauf:vorschlag", ["vorschlag"]),
                            ("Bestellungen", "einkauf:bestellungen", ["bestell*", "aus_beleg"]),
                            ("Wareneingänge", "einkauf:wareneingaenge", ["wareneingang*"]),
                            ("Lieferantenrechnungen", "einkauf:rechnungen", ["rechnung*", "sepa"]),
                            ("Preislistenartikel", "einkauf:preislistenartikel", ["preislistenartikel"]),
                            ("Lieferantenvergleich", "einkauf:preisvergleich", ["preisvergleich*"]),
                            ("Günstigster EK", "einkauf:bestpreise", ["bestpreise"]),
                            ("RMA", "einkauf:rma_liste", ["rma*"]),
                            ("Preislisten & EK-Import", "einkauf:preislisten", ["preisliste*", "artikelpreise", "preispruefliste"])]),
    "lager": ("Artikel", [("Inventur / Zählliste", "lager:inventur_liste", ["inventur_liste"]),
                          ("Seriennummern", "lager:seriennummern", ["seriennummer*", "position_sn"]),
                          ("Artikel & Bestand", "lager:uebersicht", ["*"])]),
    "esd": ("Artikel", [("ESD-Keys", "esd:uebersicht", ["*"])]),
    "werkstatt": ("Werkstatt", [("Annahme", "werkstatt:annahme", ["annahme"]),
                                ("Reparaturen", "werkstatt:liste", ["*"])]),
    "finanzen": ("Finanzen", [("Mahnwesen", "finanzen:mahnwesen", ["mahn*"]),
                              ("Bankabgleich", "finanzen:bank", ["bank*"]),
                              ("Kassenbuch", "finanzen:kassenbuch", ["kassenbuch"]),
                              ("Lastschrift-Einzug", "finanzen:lastschrift", ["lastschrift*"]),
                              ("DATEV-Export", "finanzen:datev", ["datev"]),
                              ("Offene Posten", "finanzen:offene_posten", ["*"])]),
}


def _passt(name: str, muster: list) -> bool:
    return any(m == "*" or m == name or (m.endswith("*") and name.startswith(m[:-1])) for m in muster)


def seitenpfad(request):
    """Pfad für den Kopfbereich: [(Text, URL oder None), …]. Die aktuelle Seite hängt die Vorlage aus dem Seitentitel an."""
    from django.urls import NoReverseMatch, reverse
    treffer = getattr(request, "resolver_match", None)
    benutzer = getattr(request, "user", None)
    if treffer is None or benutzer is None or not benutzer.is_authenticated:
        return {}
    if not treffer.namespace and treffer.url_name == "dashboard":
        return {"seitenpfad": [("Dashboard", None)]}
    eintrag = PFAD.get(treffer.namespace)
    if eintrag is None:
        return {}
    bereich, punkte = eintrag
    for text, ziel, muster in punkte:
        if _passt(treffer.url_name or "", muster):
            try:
                url = reverse(ziel)
            except NoReverseMatch:
                url = None
            return {"seitenpfad": [(bereich, None), (text, None if url == request.path else url)]}
    return {"seitenpfad": [(bereich, None)]}


def aktiver_mandant(request):
    """Für die Kopfzeile: unter welcher Firma gerade gearbeitet wird."""
    benutzer = getattr(request, "user", None)
    if benutzer is None or not benutzer.is_authenticated:
        return {}
    return {"aktiver_mandant": getattr(request, "mandant", None) or mandant_oder_none(request),
            "alle_mandanten": Mandant.objects.filter(aktiv=True)}
