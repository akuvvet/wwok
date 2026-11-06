"""Status als Symbol (Issue 62): eine zentrale Tabelle für alle Bereiche, gleiche Bedeutung = gleiches Symbol.

In Listen nur das Symbol, der Text steht als Tooltip (auch per Tippen/Fokus) und als aria-label; mit mit_text=True
(Detailansichten) Symbol und Text. Beim Drucken erscheint immer der Text. Die Bedeutung hängt nie nur an der Farbe.
"""
from datetime import timedelta

from django import template
from django.utils import timezone
from django.utils.html import format_html, format_html_join

register = template.Library()

# Töne: ok (erledigt), hinweis (Handlung nötig), warn (dringend/abgelehnt), info (läuft), neutral
STATUS = {
    "lieferantenrechnung": {
        "offen": ("uhr", "hinweis", "offen"),
        "exportiert": ("export", "info", "zur Zahlung exportiert"),
        "bezahlt": ("haken", "ok", "bezahlt"),
    },
    "bestellung": {
        "entwurf": ("stift", "neutral", "Entwurf"),
        "bestellt": ("lkw", "info", "bestellt"),
        "teil": ("halb", "hinweis", "teilweise geliefert"),
        "geliefert": ("haken", "ok", "vollständig geliefert"),
        "storniert": ("storno", "neutral", "storniert"),
    },
    "zahlung": {  # Verkauf: Belegstatus und Zahlstatus (Rechnung, Proforma)
        "entwurf": ("stift", "neutral", "Entwurf"),
        "abgeschlossen": ("beleg", "neutral", "abgeschlossen"),
        "offen": ("uhr", "hinweis", "offen"),
        "teilbezahlt": ("halb", "hinweis", "teilbezahlt"),
        "bezahlt": ("haken", "ok", "bezahlt"),
        "ueberzahlt": ("plus", "info", "überzahlt (Guthaben)"),
        "umgewandelt": ("pfeil", "ok", "in Rechnung umgewandelt"),
        "guthaben": ("plus", "hinweis", "umgewandelt, Guthaben offen"),
        "erstattet": ("zurueck", "neutral", "erstattet"),
        "storniert": ("storno", "neutral", "storniert"),
    },
    "rma": {
        "angelegt": ("stift", "neutral", "angelegt"),
        "eingesendet": ("lkw", "info", "an Lieferanten eingesendet"),
        "ersatz": ("haken", "ok", "Ersatz erhalten"),
        "gutschrift": ("plus", "ok", "Gutschrift erhalten"),
        "abgelehnt": ("storno", "warn", "abgelehnt/zurück"),
        "abgeschlossen": ("haken", "neutral", "abgeschlossen"),
    },
    "werkstatt": {
        "angenommen": ("eingang", "info", "angenommen"),
        "in_reparatur": ("werkstatt", "info", "in Reparatur"),
        "wartet": ("sanduhr", "hinweis", "wartet auf Teile/Rückmeldung"),
        "fertig": ("haken", "ok", "fertig – abholbereit"),
        "abgeholt": ("haken", "neutral", "abgeholt/ausgeliefert"),
        "rma": ("rma", "hinweis", "in Lieferanten-RMA"),
        "storniert": ("storno", "neutral", "storniert"),
    },
    "seriennummer": {
        "lager": ("artikel", "ok", "im Lager"),
        "verkauft": ("verkauf", "neutral", "verkauft"),
    },
    "esd": {
        "aktiv": ("schluessel", "ok", "vergeben"),
        "freigegeben": ("zurueck", "neutral", "zurück – Key freigegeben"),
        "gesperrt": ("storno", "warn", "zurück – Key gesperrt"),
    },
}
OFFENE = {"offen", "teilbezahlt"}
SKONTO_TAGE = 2  # Skontofrist läuft in so vielen Tagen ab: Hinweis „%“


def _symbol(icon, ton, text, mit_text=False, zusatz=""):
    tip = "" if mit_text else text
    return format_html(
        '<span class="status-symbol ton-{}{}" role="img" tabindex="0" aria-label="{}"{}>'
        '<svg viewBox="0 0 24 24" aria-hidden="true"><use href="#i-s-{}"/></svg>{}'
        '<span class="status-text">{}</span></span>',
        ton, " mit-text" if mit_text else "", text,
        format_html(' data-tip="{}"', tip) if tip else "", icon,
        format_html('<span class="status-zusatz" aria-hidden="true">{}</span>', zusatz) if zusatz else "", text)


def status_daten(bereich, code, text="", faellig=None, skonto_bis=None, heute=None):
    """(Symbol, Ton, Text, Zusatz) oder None, wenn der Status unbekannt ist."""
    eintrag = STATUS.get(bereich, {}).get(code)
    if eintrag is None:
        return None
    icon, ton, standard = eintrag
    text, zusatz = str(text or standard), ""
    heute = heute or timezone.localdate()
    if code in OFFENE and faellig and faellig < heute:  # Vorschlag A: überfällig
        icon, ton = "alarm", "warn"
        text += f" – überfällig seit {faellig:%d.%m.%Y}"
    if code in OFFENE and skonto_bis and heute <= skonto_bis <= heute + timedelta(days=SKONTO_TAGE):
        zusatz = "%"
        text += f" – Skonto nur noch bis {skonto_bis:%d.%m.%Y}"
    return icon, ton, text, zusatz


@register.simple_tag
def status_symbol(bereich, code, text="", mit_text=False, faellig=None, skonto_bis=None):
    daten = status_daten(bereich, code, text, faellig, skonto_bis)
    if daten is None:  # unbekannter Status: Text wie bisher
        return format_html("{}", text or code or "")
    icon, ton, text, zusatz = daten
    return _symbol(icon, ton, text, mit_text, zusatz)


@register.simple_tag
def status_legende(bereich, *zusatz):
    """Vorschlag B: ⓘ im Tabellenkopf mit allen Symbolen des Bereichs. zusatz: weitere Codes nur für die Legende
    (z. B. "ueberfaellig")."""
    zeilen = [(icon, ton, text) for icon, ton, text in STATUS.get(bereich, {}).values()]
    if "ueberfaellig" in zusatz:
        zeilen.append(("alarm", "warn", "überfällig"))
    if "skonto" in zusatz:
        zeilen.append(("uhr", "hinweis", "Skontofrist läuft bald ab (%)"))
    inhalt = format_html_join("", "<li>{} {}</li>", (
        (_symbol(i, t, x, zusatz="%" if "Skonto" in x else ""), x) for i, t, x in zeilen))
    return format_html('<details class="status-legende"><summary aria-label="Legende der Status-Symbole" title="Legende">ⓘ</summary>'
                       '<ul>{}</ul></details>', inhalt)
