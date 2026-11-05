from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.lager.models import Bewegungsart, Lagerbewegung
from apps.verkauf.mail import MailFehler, ausfuellen, senden_an
from apps.verkauf.models import Beleg, Belegart, Mailart, Nummernkreis, Position

from .models import STATUS_MIT_MAIL, Reparatur, ReparaturPosition, Status

MAILART = {
    Status.ANGENOMMEN: Mailart.REPARATUR_ANGENOMMEN,
    Status.IN_REPARATUR: Mailart.REPARATUR_LAEUFT,
    Status.FERTIG: Mailart.REPARATUR_FERTIG,
}


@transaction.atomic
def annehmen(mandant, kunde, geraet, fehler, seriennummer="", zubehoer="", artikel=None, garantie=False, benutzer=None) -> Reparatur:
    nummer = Nummernkreis.naechste(mandant, "WS", timezone.localdate().year)
    return Reparatur.objects.create(
        mandant=mandant, nummer=nummer, kunde=kunde, geraet=geraet, fehler=fehler, seriennummer=seriennummer,
        zubehoer=zubehoer, artikel=artikel, garantie=garantie, angenommen_von=benutzer)


def statusmail(reparatur, benutzer=None):
    """Statusmail an den Kunden; gibt eine Meldung zurück (oder None, wenn keine Mail nötig/möglich war)."""
    art = MAILART.get(reparatur.status)
    kunde = reparatur.kunde
    if art is None or not kunde.email:
        return None
    from apps.verkauf.mail import anrede_text
    m = reparatur.mandant
    betreff, text = ausfuellen(m, art, {
        "anrede": anrede_text(kunde), "geraet": reparatur.geraet, "nummer": reparatur.nummer,
        "status": reparatur.get_status_display(), "firma": m.firmenname, "kunde": kunde.name1})
    try:
        senden_an(m, kunde.email, betreff, text, bezug=reparatur.nummer, benutzer=benutzer)
        return f"Statusmail an {kunde.email} gesendet."
    except MailFehler as fehler:
        return f"Statusmail nicht gesendet: {fehler}"


@transaction.atomic
def status_setzen(reparatur, status, benutzer=None, mail=True) -> str:
    if status not in Status.values:
        raise ValueError("Unbekannter Status.")
    reparatur.status = status
    heute = timezone.localdate()
    if status == Status.FERTIG:
        reparatur.fertig_am = heute
    elif status == Status.ABGEHOLT:
        reparatur.abgeholt_am = heute
        reparatur.fertig_am = reparatur.fertig_am or heute
    reparatur.save()
    return statusmail(reparatur, benutzer) if mail and status in STATUS_MIT_MAIL else None


def position_hinzufuegen(reparatur, art, bezeichnung, menge, preis, artikel=None) -> ReparaturPosition:
    steuer = artikel.steuersatz.prozent if artikel else Decimal("19")
    return ReparaturPosition.objects.create(reparatur=reparatur, art=art, artikel=artikel, bezeichnung=bezeichnung,
                                            menge=menge, einzelpreis=preis, steuer_prozent=steuer)


@transaction.atomic
def rechnung_erzeugen(reparatur, benutzer=None) -> Beleg:
    """Rechnung (Entwurf) aus Teilen und Arbeitszeit; Teile werden erst beim Abschluss der Rechnung abgebucht."""
    if reparatur.garantie:
        raise ValueError("Garantiefälle werden nicht berechnet.")
    if reparatur.rechnung_id:
        raise ValueError("Es gibt schon eine Rechnung zu diesem Auftrag.")
    if not reparatur.positionen.exists():
        raise ValueError("Keine Positionen erfasst.")
    k = reparatur.kunde
    beleg = Beleg.objects.create(
        mandant=reparatur.mandant, art=Belegart.RECHNUNG, kunde=k, zahlungsbedingung=k.zahlungsbedingung,
        bemerkung=f"Reparatur {reparatur.nummer}: {reparatur.geraet}" + (f" (SN {reparatur.seriennummer})" if reparatur.seriennummer else ""),
        rechnungsanschrift="\n".join(t for t in (k.name1, k.name2, k.strasse, f"{k.plz} {k.ort}") if t))
    for p in reparatur.positionen.all():
        Position.objects.create(
            beleg=beleg, typ=p.artikel.typ if p.artikel_id else "L", artikel=p.artikel, bezeichnung=p.bezeichnung,
            menge=p.menge, einzelpreis=p.einzelpreis, steuer_prozent=p.steuer_prozent)
    beleg.berechne()
    reparatur.rechnung = beleg
    reparatur.save()
    return beleg


@transaction.atomic
def garantieteile_buchen(reparatur, benutzer=None) -> int:
    """Garantiefall: verbaute Lagerteile als Werkstattverbrauch vom Bestand abbuchen (einmalig)."""
    if not reparatur.garantie:
        raise ValueError("Nur bei Garantiefällen; sonst werden die Teile über die Rechnung abgebucht.")
    if reparatur.teile_gebucht:
        raise ValueError("Teile sind schon gebucht.")
    anzahl = 0
    for p in reparatur.positionen.filter(art=ReparaturPosition.TEIL, artikel__typ="A").select_related("artikel"):
        Lagerbewegung.objects.create(mandant=reparatur.mandant, artikel=p.artikel, art=Bewegungsart.WERKSTATT,
                                     menge=-p.menge, bemerkung=f"Garantie {reparatur.nummer}", benutzer=benutzer)
        anzahl += 1
    reparatur.teile_gebucht = True
    reparatur.save()
    return anzahl
