"""Versand von Belegen per E-Mail (PDF im Anhang) mit Protokoll."""
import os
import re
from string import Formatter

from django.core.mail import EmailMessage, get_connection
from django.utils import timezone

from apps.kern.models import Mandant

from .models import Belegart, Mailart, MailVersand, Mailvorlage
from .pdf import beleg_pdf

ARTIKEL = {Belegart.ANGEBOT: "unser Angebot", Belegart.AUFTRAG: "die Bestellbestätigung",
           Belegart.LIEFERSCHEIN: "den Lieferschein", Belegart.RECHNUNG: "die Rechnung",
           Belegart.GUTSCHRIFT: "die Gutschrift", Belegart.NACHLIEFERSCHEIN: "den Nachlieferschein",
           Belegart.PROFORMA: "die Proforma-Rechnung", Belegart.PROFORMA_STORNO: "den Storno zur Proforma-Rechnung"}
GRUSS = "Mit freundlichen Grüßen\n{firma}"
BELEG_TEXT = "{anrede},\n\nanbei erhalten Sie %s {nummer}.\n{zahlungstext}\n\nBei Fragen melden Sie sich gerne.\n\n" + GRUSS

# Standardtexte: Erstbefüllung der Mailvorlagen und Rückfall bei fehlender Zeile oder fehlerhaften Klammern
STANDARD = {
    **{art: ("{art} {nummer} von {firma}", BELEG_TEXT % text) for art, text in ARTIKEL.items()},
    Mailart.LIEFERANT: ("Bestellung {nummer} von {firma}",
                        "{anrede},\n\nanbei unsere Bestellung {nummer}. Bitte bestätigen Sie den Eingang und den "
                        "voraussichtlichen Liefertermin.\n\n" + GRUSS),
    Mailart.ESD: ("Ihre Lizenzschlüssel zu {art} {nummer}",
                  "{anrede},\n\nanbei Ihre Lizenzschlüssel zu {art} {nummer}:\n\n{keys}\n\n" + GRUSS),
    Mailart.VERSAND: ("Ihre Bestellung wurde versendet – {firma}",
                      "{anrede},\n\nIhre Ware zu {art} {nummer} wurde am {versanddatum} mit {dienst} versendet.\n"
                      "{sendung}\n\n" + GRUSS),
    Mailart.MAHNUNG: ("{bezeichnung} zu Rechnung {nummer}", "{mahntext}"),
    Mailart.REPARATUR_ANGENOMMEN: ("Reparatur {nummer}: {status}",
                                   "{anrede},\n\nwir haben Ihr Gerät {geraet} unter der Auftragsnummer {nummer} "
                                   "angenommen und kümmern uns darum.\n\n" + GRUSS),
    Mailart.REPARATUR_LAEUFT: ("Reparatur {nummer}: {status}",
                               "{anrede},\n\nIhr Gerät {geraet} (Auftrag {nummer}) ist jetzt in Reparatur.\n\n" + GRUSS),
    Mailart.REPARATUR_FERTIG: ("Reparatur {nummer}: {status}",
                               "{anrede},\n\nIhr Gerät {geraet} (Auftrag {nummer}) ist fertig und kann bei uns "
                               "abgeholt werden.\n\n" + GRUSS),
}
BELEG_WERTE = ("anrede", "art", "art_mit_artikel", "nummer", "firma", "kunde", "datum", "faelligkeit", "zahlungstext", "betrag", "bezahlt")
REPARATUR_WERTE = ("anrede", "geraet", "nummer", "status", "firma", "kunde")
PLATZHALTER = {
    **{art: BELEG_WERTE for art in Belegart},
    Mailart.LIEFERANT: ("anrede", "nummer", "firma", "lieferant"),
    Mailart.ESD: BELEG_WERTE + ("keys",),
    Mailart.VERSAND: BELEG_WERTE + ("versanddatum", "dienst", "sendung"),
    Mailart.MAHNUNG: ("anrede", "kunde", "nummer", "datum", "faelligkeit", "offen", "gebuehr", "gesamt", "frist", "firma",
                      "bezeichnung", "mahntext"),
    Mailart.REPARATUR_ANGENOMMEN: REPARATUR_WERTE,
    Mailart.REPARATUR_LAEUFT: REPARATUR_WERTE,
    Mailart.REPARATUR_FERTIG: REPARATUR_WERTE,
}
PFLICHT = {Mailart.ESD: ("keys",)}  # ohne diesen Platzhalter wäre die Mail nutzlos
FORMATFEHLER = (ValueError, IndexError, KeyError, AttributeError, TypeError)


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
        "bezahlt": f"{bezahlt_betrag(beleg):.2f}".replace(".", ","),
    }


def bezahlt_betrag(beleg):
    """Erhaltene Zahlungen: auf Rechnungen inklusive verrechneter Vorkasse/Anzahlung."""
    from apps.finanzen.services import angerechnet, bezahlt
    if not beleg.pk:
        return 0
    return bezahlt(beleg) + (angerechnet(beleg) if beleg.art == Belegart.RECHNUNG else 0)


def ausfuellen(mandant, art, werte: dict):
    """Betreff und Text aus der Mailvorlage des Mandanten (oder dem Standard) mit eingesetzten Platzhaltern."""
    vorlage = Mailvorlage.objects.filter(mandant=mandant, art=art).first()
    betreff, text = (vorlage.betreff, vorlage.text) if vorlage else STANDARD[art]
    try:
        betreff, text = betreff.format_map(_Leer(werte)), text.format_map(_Leer(werte))
    except FORMATFEHLER:  # fehlerhafte Klammern in der Mailvorlage
        betreff, text = (teil.format_map(_Leer(werte)) for teil in STANDARD[art])
    return " ".join(betreff.split())[:200], re.sub(r"\n{3,}", "\n\n", text.replace("\r\n", "\n"))


def vorlagen_anlegen():
    """Legt fehlende Mailvorlagen je Mandant mit dem Standardtext an; vorhandene bleiben unverändert."""
    vorhanden = set(Mailvorlage.objects.values_list("mandant_id", "art"))
    for m in Mandant.objects.all():
        for art, (betreff, text) in STANDARD.items():
            if (m.pk, art) not in vorhanden:  # einzeln, damit das Änderungsprotokoll den Eintrag sieht
                Mailvorlage.objects.get_or_create(mandant=m, art=art, defaults={"betreff": betreff, "text": text})


def _platzhalter_in(teil: str) -> set:
    benutzt = set()
    for _, name, format_angabe, umwandlung in Formatter().parse(teil):
        if name is not None:
            benutzt.add(name + (f":{format_angabe}" if format_angabe else "") + (f"!{umwandlung}" if umwandlung else ""))
    return benutzt


def vorlage_fehler(art, betreff: str, text: str) -> str:
    """Grund, warum Betreff oder Text nicht einsetzbar sind: Klammern, unbekannte oder fehlende Platzhalter."""
    try:
        im_betreff, im_text = _platzhalter_in(betreff), _platzhalter_in(text)
    except ValueError:
        return "Die geschweiften Klammern passen nicht zusammen."
    unbekannt = sorted((im_betreff | im_text) - set(PLATZHALTER[art]))
    if unbekannt:
        return "Unbekannter Platzhalter: " + " ".join("{" + name + "}" for name in unbekannt)
    fehlend = [name for name in PFLICHT.get(art, ()) if name not in im_text]  # der Betreff wird gekürzt
    if fehlend:
        return "Dieser Platzhalter muss im Text vorkommen: " + " ".join("{" + name + "}" for name in fehlend)
    return ""


def vorschlag(beleg):
    """Empfänger, Betreff und Text aus Vorlage (oder Standard) mit eingesetzten Platzhaltern."""
    betreff, text = ausfuellen(beleg.mandant, beleg.art, platzhalter(beleg))
    ap = beleg.ansprechpartner
    empfaenger = [(ap.email if ap and ap.email else beleg.kunde.email) or ""]
    if beleg.art in (Belegart.RECHNUNG, Belegart.GUTSCHRIFT):
        empfaenger += adressen(beleg.kunde.email_weitere)
    an = ", ".join(dict.fromkeys(a for a in empfaenger if a))  # ohne Dubletten, Reihenfolge bleibt
    return an, betreff, text


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
    from apps.lager.services import druckhindernis
    hindernis = druckhindernis(beleg)
    if hindernis:
        raise MailFehler(hindernis)
    from .zugferd import beleg_pdf_mit_xml  # abgeschlossene Rechnungen/Gutschriften gehen als ZUGFeRD-PDF raus
    pdf, _ = beleg_pdf_mit_xml(beleg)
    return senden(beleg, an, betreff, text, pdf, f"{beleg.nummer}.pdf", benutzer)
