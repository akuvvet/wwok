"""
Flächenfarben je Mandant: Kopfbereich, Seitenleiste und Karten getrennt einfärben.

Aus der gewählten Flächenfarbe werden Schrift-, Akzent- und Randfarbe so abgeleitet, dass alles lesbar bleibt
(Kontrast nach WCAG). Für den dunklen Modus wird jede Fläche abgedunkelt. Das Ergebnis ist fertiges CSS,
das nur aus geprüften Farbwerten (#RRGGBB) und festen Selektoren besteht.
"""
import re

from django.utils.safestring import mark_safe

HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
STANDARD_AKZENT = "#0b5cad"
TEXT_DUNKEL = "#171b22"
TEXT_HELL = "#ffffff"
DUNKEL_BASIS = "#181c22"  # Kartenfarbe des dunklen Modus
SELEKTOREN = {"kopf": ".kopfbereich header", "leiste": ".seitenleiste", "karte": ".karte"}
# Knöpfe mit Akzentfläche: Schrift muss zur (evtl. geänderten) Akzentfarbe passen
KNOEPFE = (
    "{s} button:not(.leise):not(.gefahr):not(.tab):not(.symbol):not(.toast-zu):not(.leistenknopf):not(.palettenknopf),"
    "{s} .knopf:not(.leise),{s} .symbol.voll,{s} .symbol:not(.gefahr):hover,{s} .symbol:not(.gefahr):focus-visible"
)


# Zurücksetzen: Hausfarbe je Mandant (Anfang des Kurznamens), Flächen neutral wie die Windows-Standardfarben
HAUSFARBEN = {"okay": "#2b72b4", "zone": "#83bb2a"}


def hausfarbe(mandant):
    """Standard-Layoutfarbe des Mandanten; leer = Standardblau."""
    kurz = (mandant.kurzname or "").lower()
    return next((farbe for anfang, farbe in HAUSFARBEN.items() if kurz.startswith(anfang)), "")


def aussehen_zuruecksetzen(mandant):
    """Setzt Layoutfarbe auf die Hausfarbe und alle Flächen auf neutral (Standardfarben)."""
    mandant.farbe = hausfarbe(mandant)
    mandant.flaechenmodus = "standard"
    mandant.farbe_kopf = mandant.farbe_leiste = mandant.farbe_karte = ""
    mandant.save(update_fields=["farbe", "flaechenmodus", "farbe_kopf", "farbe_leiste", "farbe_karte", "geaendert_am", "geaendert_von"])


def gueltig(farbe):
    return bool(farbe) and bool(HEX.match(farbe))


def _rgb(farbe):
    return tuple(int(farbe[i:i + 2], 16) for i in (1, 3, 5))


def mische(a, b, anteil):
    """Mischt `anteil` (0–1) von Farbe a mit dem Rest von Farbe b."""
    return "#%02x%02x%02x" % tuple(round(x * anteil + y * (1 - anteil)) for x, y in zip(_rgb(a), _rgb(b)))


def helligkeit(farbe):
    """Relative Helligkeit nach WCAG (0 = schwarz, 1 = weiß)."""
    teile = []
    for wert in _rgb(farbe):
        w = wert / 255
        teile.append(w / 12.92 if w <= 0.04045 else ((w + 0.055) / 1.055) ** 2.4)
    return 0.2126 * teile[0] + 0.7152 * teile[1] + 0.0722 * teile[2]


def kontrast(a, b):
    ha, hb = helligkeit(a), helligkeit(b)
    return (max(ha, hb) + 0.05) / (min(ha, hb) + 0.05)


def bereich(flaeche, akzent):
    """Lesbare Farben für eine Fläche: Schrift, Akzent (Links, aktive Zeile, Knöpfe) und Knopfschrift."""
    text = TEXT_HELL if kontrast(TEXT_HELL, flaeche) >= kontrast(TEXT_DUNKEL, flaeche) else TEXT_DUNKEL
    dunkel = text == TEXT_HELL
    if kontrast(text, flaeche) < 4.5 and not dunkel:  # mittlere Töne: reines Schwarz reicht gerade noch
        text = "#000000"
    ersatz = mische(akzent, "#ffffff", 0.45) if dunkel else mische(akzent, "#000000", 0.6)
    akzent_hier = next((f for f in (akzent, ersatz) if kontrast(f, flaeche) >= 3), text)
    knopftext = TEXT_HELL if kontrast(TEXT_HELL, akzent_hier) >= 3 else TEXT_DUNKEL
    return {"flaeche": flaeche, "text": text, "akzent": akzent_hier, "knopftext": knopftext, "dunkel": dunkel}


def dunkelvariante(farbe):
    """Fläche für den dunklen Modus: schon dunkle Farben bleiben, helle werden stark abgedunkelt."""
    return farbe if helligkeit(farbe) < 0.08 else mische(farbe, DUNKEL_BASIS, 0.3)


def flaechenfarben(mandant):
    """Je Bereich (kopf, leiste, karte) ein Paar (hell, dunkel) aus Flächenfarben; leer = neutral wie bisher."""
    akzent = mandant.farbe if gueltig(mandant.farbe) else STANDARD_AKZENT
    if mandant.flaechenmodus == "ton":
        return {
            "kopf": (akzent, mische(akzent, DUNKEL_BASIS, 0.5)),
            "leiste": (mische(akzent, "#ffffff", 0.16), mische(akzent, DUNKEL_BASIS, 0.2)),
            "karte": (mische(akzent, "#ffffff", 0.05), mische(akzent, DUNKEL_BASIS, 0.08)),
        }
    if mandant.flaechenmodus == "eigen":
        gewaehlt = {"kopf": mandant.farbe_kopf, "leiste": mandant.farbe_leiste, "karte": mandant.farbe_karte}
        return {name: (f, dunkelvariante(f)) for name, f in gewaehlt.items() if gueltig(f)}
    return {}


def _regel(selektor, b):
    f, t, a = b["flaeche"], b["text"], b["akzent"]
    feld = f"color-mix(in srgb,{t} 12%,{f})" if b["dunkel"] else "#fff"
    status = "--warn:#f97066;--ok:#47cd89;--hinweis:#f6c453;" if b["dunkel"] else "--warn:#b42318;--ok:#067647;--hinweis:#b7791f;"
    return (
        f"{selektor}{{--karte:{f};--text:{t};--akzent:{a};--knopftext:{b['knopftext']};"
        f"--rand:color-mix(in srgb,{t} 18%,{f});--leise:color-mix(in srgb,{t} 68%,{f});--grund:color-mix(in srgb,{t} 7%,{f});"
        f"--feld:{feld};--feldrand:color-mix(in srgb,{t} 42%,{f});--toenung:color-mix(in srgb,{a} 14%,transparent);"
        f"{status}color:{t};}}"
        f"{KNOEPFE.format(s=selektor)}{{color:var(--knopftext);}}"
    )


def _verwaltung(b):
    """Kopfzeile der Verwaltung (Django-Admin) in der Kopffarbe."""
    return (f":root{{--header-bg:{b['flaeche']};--header-color:{b['text']};--header-link-color:{b['text']};"
            f"--header-branding-color:{b['text']};}}")


def flaechen_css(mandant):
    """Fertiges CSS für die Flächenfarben des Mandanten; leer, wenn nichts eingestellt ist."""
    farben = flaechenfarben(mandant)
    if not farben:
        return ""
    akzent = mandant.farbe if gueltig(mandant.farbe) else STANDARD_AKZENT
    akzent_dunkel = mische(akzent, "#ffffff", 0.6)
    hell, dunkel = [], []
    for name, (f_hell, f_dunkel) in farben.items():
        b_hell, b_dunkel = bereich(f_hell, akzent), bereich(f_dunkel, akzent_dunkel)
        hell.append(_regel(SELEKTOREN[name], b_hell))
        dunkel.append(_regel(SELEKTOREN[name], b_dunkel))
        if name == "kopf":
            hell.append(_verwaltung(b_hell))
            dunkel.append(_verwaltung(b_dunkel))
    return mark_safe("".join(hell) + "@media (prefers-color-scheme: dark){" + "".join(dunkel) + "}")
