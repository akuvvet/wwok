"""Versand von Belegen per E-Mail (PDF im Anhang) mit Protokoll."""
import os
import re

from django.core.mail import EmailMessage, get_connection
from django.utils import timezone

from .models import Belegart, MailVersand, Mailvorlage
from .pdf import beleg_pdf

STANDARD_BETREFF = "{art} {nummer} von {firma}"
STANDARD_TEXT = (
    "{anrede},\n\n"
    "anbei erhalten Sie {art_mit_artikel} {nummer}.\n"
    "{zahlungstext}\n\n"
    "Bei Fragen melden Sie sich gerne.\n\n"
    "Mit freundlichen Grüßen\n{firma}"
)
ARTIKEL = {Belegart.ANGEBOT: "unser Angebot", Belegart.AUFTRAG: "die Auftragsbestätigung",
           Belegart.LIEFERSCHEIN: "den Lieferschein", Belegart.RECHNUNG: "die Rechnung",
           Belegart.GUTSCHRIFT: "die Gutschrift", Belegart.NACHLIEFERSCHEIN: "den Nachlieferschein"}


class MailFehler(Exception):
    pass


def anrede(beleg) -> str:
    ap = beleg.ansprechpartner
    if ap and ap.nachname and ap.anrede == "Herr":
        return f"Sehr geehrter Herr {ap.nachname}"
    if ap and ap.nachname and ap.anrede == "Frau":
        return f"Sehr geehrte Frau {ap.nachname}"
    return "Sehr geehrte Damen und Herren"


def anrede_text(kunde) -> str:
    """Anrede für Mails ohne Beleg: über den Hauptkontakt des Kunden."""
    ap = kunde.ansprechpartner.filter(hauptkontakt=True).first()
    if ap and ap.nachname and ap.anrede == "Herr":
        return f"Sehr geehrter Herr {ap.nachname}"
    if ap and ap.nachname and ap.anrede == "Frau":
        return f"Sehr geehrte Frau {ap.nachname}"
    return "Sehr geehrte Damen und Herren"


def platzhalter(beleg) -> dict:
    return {
        "anrede": anrede(beleg), "art": beleg.get_art_display(), "art_mit_artikel": ARTIKEL.get(beleg.art, "den Beleg"),
        "nummer": beleg.nummer or "", "firma": beleg.mandant.firmenname, "kunde": beleg.kunde.name1,
        "datum": beleg.belegdatum.strftime("%d.%m.%Y"),
        "faelligkeit": beleg.faellig_am.strftime("%d.%m.%Y") if beleg.faellig_am else "",
        "zahlungstext": beleg.zahlungstext, "betrag": f"{beleg.summe_brutto:.2f}".replace(".", ","),
    }


def vorschlag(beleg):
    """Empfänger, Betreff und Text aus Vorlage (oder Standard) mit eingesetzten Platzhaltern."""
    vorlage = Mailvorlage.objects.filter(mandant=beleg.mandant, art=beleg.art).first()
    werte = platzhalter(beleg)
    betreff = (vorlage.betreff if vorlage else STANDARD_BETREFF).format_map(_Leer(werte))
    text = (vorlage.text if vorlage else STANDARD_TEXT).format_map(_Leer(werte))
    ap = beleg.ansprechpartner
    empfaenger = [(ap.email if ap and ap.email else beleg.kunde.email) or ""]
    if beleg.art in (Belegart.RECHNUNG, Belegart.GUTSCHRIFT):
        empfaenger += adressen(beleg.kunde.email_weitere)
    an = ", ".join(dict.fromkeys(a for a in empfaenger if a))  # ohne Dubletten, Reihenfolge bleibt
    return an, betreff, re.sub(r"\n{3,}", "\n\n", text)


class _Leer(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def verbindung(mandant):
    if not (mandant.smtp_host and mandant.absender_email):
        raise MailFehler(f"Für {mandant.kurzname} fehlen SMTP-Server oder Absenderadresse (Verwaltung → Mandanten).")
    passwort = os.environ.get(f"SMTP_PASSWORD_{mandant.kurzname.upper()}", "")
    return get_connection(
        backend="django.core.mail.backends.smtp.EmailBackend", host=mandant.smtp_host, port=mandant.smtp_port,
        username=mandant.smtp_benutzer or None, password=passwort or None,
        use_tls=mandant.smtp_sicherheit == "starttls", use_ssl=mandant.smtp_sicherheit == "ssl", timeout=20)


def adressen(text: str) -> list:
    return [a.strip() for a in re.split(r"[,;\s]+", text or "") if a.strip()]


def senden_an(mandant, an: str, betreff: str, text: str, pdf: bytes = None, dateiname: str = "", beleg=None,
              bezug: str = "", benutzer=None) -> MailVersand:
    """Verschickt eine Mail (optional mit PDF-Anhang) für einen Mandanten und protokolliert sie in jedem Fall."""
    empfaenger = adressen(an)
    if not empfaenger:
        raise MailFehler("Keine Empfängeradresse angegeben.")
    protokoll = MailVersand(beleg=beleg, mandant=mandant, bezug=bezug, an=", ".join(empfaenger), betreff=betreff,
                            text=text, benutzer=benutzer)
    try:
        nachricht = EmailMessage(
            subject=betreff, body=text, to=empfaenger, bcc=[mandant.kopie_an] if mandant.kopie_an else [],
            from_email=f"{mandant.absender_name or mandant.firmenname} <{mandant.absender_email}>",
            connection=verbindung(mandant))
        if pdf:
            nachricht.attach(dateiname, pdf, "application/pdf")
        nachricht.send()
        protokoll.erfolgreich = True
    except Exception as fehler:  # Protokoll auch bei Fehlern, Meldung an die Maske
        protokoll.fehler = str(fehler)[:500]
    protokoll.zeitpunkt = timezone.now()
    protokoll.save()
    if not protokoll.erfolgreich:
        raise MailFehler(f"Versand fehlgeschlagen: {protokoll.fehler}")
    return protokoll


def senden(beleg, an: str, betreff: str, text: str, pdf: bytes, dateiname: str, benutzer=None) -> MailVersand:
    """Verschickt eine Mail mit PDF-Anhang und legt einen Protokolleintrag zum Beleg an."""
    return senden_an(beleg.mandant, an, betreff, text, pdf, dateiname, beleg=beleg, benutzer=benutzer)


def beleg_senden(beleg, an: str, betreff: str, text: str, benutzer=None) -> MailVersand:
    """Verschickt den Beleg als PDF."""
    if not beleg.nummer:
        raise MailFehler("Entwürfe ohne Belegnummer können nicht versendet werden.")
    if not beleg.positionen.exists():
        raise MailFehler("Ein Beleg ohne Position kann nicht versendet werden.")
    from .zugferd import beleg_pdf_mit_xml  # abgeschlossene Rechnungen/Gutschriften gehen als ZUGFeRD-PDF raus
    pdf, _ = beleg_pdf_mit_xml(beleg)
    return senden(beleg, an, betreff, text, pdf, f"{beleg.nummer}.pdf", benutzer)
