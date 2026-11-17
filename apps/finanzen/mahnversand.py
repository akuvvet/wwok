"""Mahnungen versenden und drucken: einzeln oder mehrere auf einmal, mit Vermerk an der Mahnung."""
from io import BytesIO

from django.utils import timezone
from pypdf import PdfWriter

from apps.verkauf.mail import MailFehler, adressen, senden

from .mahntext import mail_fuer
from .pdf import mahnung_pdf


def empfaenger(mahnung) -> str:
    """Adresse des Ansprechpartners (sonst des Kunden) und die weiteren Rechnungsadressen, ohne Dubletten."""
    beleg = mahnung.beleg
    ap = beleg.ansprechpartner
    erste = ap.email if ap and ap.email else beleg.kunde.email
    return ", ".join(dict.fromkeys(a for a in [erste] + adressen(beleg.kunde.email_weitere) if a))


def mahnung_senden(mahnung, an=None, betreff=None, text=None, benutzer=None):
    """Verschickt die Mahnung als PDF und vermerkt Erfolg oder Fehler an der Mahnung. Ohne Angaben: Standardtext."""
    if betreff is None or text is None:
        betreff, text = mail_fuer(mahnung)
    an = empfaenger(mahnung) if an is None else an
    beleg = mahnung.beleg
    try:
        protokoll = senden(beleg, an, betreff, text, mahnung_pdf(mahnung), f"Mahnung-{beleg.nummer}-{mahnung.stufe}.pdf", benutzer)
    except MailFehler as fehler:
        mahnung.versand_fehler = str(fehler)[:500]
        mahnung.save(update_fields=["versand_fehler"])
        raise
    mahnung.gesendet_am, mahnung.gesendet_an, mahnung.versand_fehler = protokoll.zeitpunkt, protokoll.an[:500], ""
    mahnung.save(update_fields=["gesendet_am", "gesendet_an", "versand_fehler"])
    return protokoll


def sammel_pdf(mahnungen) -> bytes:
    """Ein PDF mit allen Mahnungen hintereinander (jede beginnt auf einer neuen Seite); vermerkt den Druck."""
    gesamt = PdfWriter()
    jetzt = timezone.now()
    for mahnung in mahnungen:
        gesamt.append(BytesIO(mahnung_pdf(mahnung)))
        mahnung.gedruckt_am = jetzt
        mahnung.save(update_fields=["gedruckt_am"])
    puffer = BytesIO()
    gesamt.write(puffer)
    return puffer.getvalue()
