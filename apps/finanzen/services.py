from datetime import timedelta
from decimal import Decimal

from django.db import models, transaction
from django.utils import timezone

from apps.verkauf.models import Beleg, Belegart

from apps.kern.models import Mandant

from .models import Kassenbuch, Mahnstufe, Mahnung, Zahlart, Zahlung

CENT = Decimal("0.01")
STANDARD_STUFEN = [
    (1, "Zahlungserinnerung", 0, 7, Decimal("0"),
     "{anrede},\n\nbei der Durchsicht unserer Buchhaltung ist uns aufgefallen, dass die Rechnung {nummer} vom {datum} "
     "über {offen} € noch nicht beglichen ist (fällig am {faelligkeit}).\n\n"
     "Bitte überweisen Sie den Betrag bis zum {frist}. Sollte sich Ihre Zahlung mit diesem Schreiben überschnitten "
     "haben, betrachten Sie es bitte als gegenstandslos.\n\nMit freundlichen Grüßen\n{firma}"),
    (2, "1. Mahnung", 7, 7, Decimal("5.00"),
     "{anrede},\n\nleider konnten wir zu unserer Rechnung {nummer} vom {datum} trotz Erinnerung keinen Zahlungseingang "
     "feststellen. Offen sind {offen} € zuzüglich {gebuehr} € Mahngebühr, insgesamt {gesamt} €.\n\n"
     "Bitte zahlen Sie bis zum {frist}.\n\nMit freundlichen Grüßen\n{firma}"),
    (3, "2. Mahnung", 7, 7, Decimal("10.00"),
     "{anrede},\n\nauf unsere bisherigen Schreiben zur Rechnung {nummer} vom {datum} haben wir keine Zahlung erhalten. "
     "Offen sind {offen} € zuzüglich {gebuehr} € Mahngebühr, insgesamt {gesamt} €.\n\n"
     "Wir fordern Sie auf, den Gesamtbetrag bis zum {frist} zu zahlen. Andernfalls behalten wir uns weitere Schritte vor.\n\n"
     "Mit freundlichen Grüßen\n{firma}"),
]


def stufen(mandant) -> dict:
    """Mahnstufen des Mandanten; fehlen sie, werden die Standardwerte angelegt."""
    if not Mahnstufe.objects.filter(mandant=mandant).exists():
        for stufe, name, tage, frist, gebuehr, text in STANDARD_STUFEN:
            Mahnstufe.objects.create(mandant=mandant, stufe=stufe, bezeichnung=name, tage_seit_vorheriger=tage,
                                     zahlungsfrist_tage=frist, gebuehr=gebuehr, text=text)
    return {s.stufe: s for s in Mahnstufe.objects.filter(mandant=mandant)}


def bezahlt(beleg) -> Decimal:
    return beleg.zahlungen.aggregate(s=models.Sum("betrag"))["s"] or Decimal("0")


def gutgeschrieben(beleg) -> Decimal:
    return Beleg.objects.filter(vorgaenger=beleg, art=Belegart.GUTSCHRIFT, abgeschlossen=True).aggregate(
        s=models.Sum("summe_brutto"))["s"] or Decimal("0")


def offen(beleg) -> Decimal:
    return beleg.summe_brutto - bezahlt(beleg) - gutgeschrieben(beleg)


def faellig_am(beleg):
    return beleg.faellig_am or beleg.belegdatum


def mahnfaellig_ab(beleg):
    """Belegdatum + Tage 'Mahnung fällig' der Zahlungsbedingung (ohne Bedingung: Fälligkeit)."""
    zb = beleg.zahlungsbedingung
    return beleg.belegdatum + timedelta(days=zb.tage_mahnung) if zb else faellig_am(beleg)


def offene_rechnungen(mandant, kunde=None):
    """Abgeschlossene Rechnungen mit offenem Betrag, als Liste von Zeilen (älteste zuerst)."""
    belege = Beleg.objects.filter(mandant=mandant, art=Belegart.RECHNUNG, abgeschlossen=True) \
        .select_related("kunde", "zahlungsbedingung").order_by("belegdatum", "id")
    if kunde is not None:
        belege = belege.filter(kunde=kunde)
    heute = timezone.localdate()
    zeilen = []
    for b in belege:
        rest = offen(b)
        if abs(rest) < CENT:
            continue
        faellig = faellig_am(b)
        letzte = b.mahnungen.order_by("-stufe").first()
        zeilen.append({"beleg": b, "bezahlt": bezahlt(b), "gutschrift": gutgeschrieben(b), "offen": rest,
                       "faellig": faellig, "ueberfaellig": max((heute - faellig).days, 0), "mahnstufe": letzte.stufe if letzte else 0})
    return zeilen


def kunde_offen(mandant, kunde) -> Decimal:
    return sum((z["offen"] for z in offene_rechnungen(mandant, kunde)), Decimal("0"))


def mahnvorschlag(mandant, heute=None):
    """Rechnungen, für die jetzt die nächste Mahnstufe fällig ist."""
    heute = heute or timezone.localdate()
    alle = stufen(mandant)
    vorschlag = []
    for z in offene_rechnungen(mandant):
        if z["offen"] <= 0:
            continue
        b = z["beleg"]
        naechste = z["mahnstufe"] + 1
        if naechste not in alle:
            continue
        if naechste == 1:
            faellig = heute > mahnfaellig_ab(b)
        else:
            letzte = b.mahnungen.get(stufe=z["mahnstufe"])
            faellig = heute >= letzte.datum + timedelta(days=alle[naechste].tage_seit_vorheriger)
        if faellig:
            vorschlag.append({**z, "stufe": alle[naechste]})
    return vorschlag


@transaction.atomic
def mahnung_erstellen(beleg, stufe_nr, benutzer=None, heute=None) -> Mahnung:
    heute = heute or timezone.localdate()
    stufe = stufen(beleg.mandant)[stufe_nr]
    rest = offen(beleg)
    if rest <= 0:
        raise ValueError(f"{beleg} ist nicht offen.")
    return Mahnung.objects.create(
        beleg=beleg, stufe=stufe.stufe, bezeichnung=stufe.bezeichnung, datum=heute, offener_betrag=rest,
        gebuehr=stufe.gebuehr, frist=heute + timedelta(days=stufe.zahlungsfrist_tage), benutzer=benutzer)


@transaction.atomic
def zahlung_buchen(beleg, betrag, datum, zahlart, bemerkung="", benutzer=None, storno_zu=None, storno_grund="") -> Zahlung:
    if beleg.art not in (Belegart.RECHNUNG, Belegart.AUFTRAG) or (beleg.art == Belegart.RECHNUNG and not beleg.abgeschlossen):
        raise ValueError("Zahlungen können nur auf abgeschlossene Rechnungen oder Aufträge gebucht werden.")
    if betrag == 0:
        raise ValueError("Betrag darf nicht 0 sein.")
    zahlung = Zahlung.objects.create(mandant=beleg.mandant, beleg=beleg, betrag=betrag, datum=datum, zahlart=zahlart,
                                     bemerkung=bemerkung, benutzer=benutzer, storno_zu=storno_zu, storno_grund=storno_grund)
    if zahlart == Zahlart.BAR:  # Barzahlung läuft ins Kassenbuch
        kasse_buchen(beleg.mandant, Kassenbuch.EINNAHME if betrag > 0 else Kassenbuch.AUSGABE, abs(betrag),
                     (f"Storno Barzahlung {beleg.nummer}: {storno_grund}" if storno_zu and betrag < 0 else
                      f"Storno Barrückzahlung {beleg.nummer}: {storno_grund}" if storno_zu else
                      f"{'Barzahlung' if betrag > 0 else 'Barrückzahlung'} {beleg.nummer}"), datum, beleg, benutzer)
    return zahlung


@transaction.atomic
def zahlung_stornieren(zahlung, grund, benutzer=None, datum=None) -> Zahlung:
    """Storno eines Zahlungseingangs (oder -ausgangs) per Gegenbuchung; der Grund ist Pflicht.
    Bar-Zahlungen laufen automatisch ins Kassenbuch zurück, ein zugeordneter Bankumsatz wird wieder frei."""
    grund = (grund or "").strip()
    if not grund:
        raise ValueError("Bitte einen Stornogrund angeben.")
    Zahlung.objects.select_for_update().get(pk=zahlung.pk)  # Sperre, damit ein Doppelstorno als Meldung statt als Fehler endet
    if zahlung.storno_zu_id:
        raise ValueError("Eine Stornobuchung kann nicht erneut storniert werden.")
    if Zahlung.objects.filter(storno_zu=zahlung).exists():
        raise ValueError("Diese Zahlung ist bereits storniert.")
    gegen = zahlung_buchen(zahlung.beleg, -zahlung.betrag, datum or timezone.localdate(), zahlung.zahlart,
                           f"Storno: {grund}"[:200], benutzer, storno_zu=zahlung, storno_grund=grund[:300])
    from .models import Bankumsatz
    for u in Bankumsatz.objects.filter(zahlung=zahlung):
        u.zahlung, u.status = None, Bankumsatz.OFFEN  # Umsatz kann neu zugeordnet werden
        u.save(update_fields=["zahlung", "status"])
    return gegen


@transaction.atomic
def kasse_buchen(mandant, art, betrag, text, datum=None, beleg=None, benutzer=None) -> Kassenbuch:
    """Fortlaufend nummerierter, unveränderlicher Kassenbucheintrag."""
    Mandant.objects.select_for_update().get(pk=mandant.pk)  # Sperre, damit Nummern lückenlos bleiben
    letzte = Kassenbuch.objects.filter(mandant=mandant).aggregate(m=models.Max("nummer"))["m"] or 0
    return Kassenbuch.objects.create(mandant=mandant, nummer=letzte + 1, art=art, betrag=betrag, text=text,
                                     datum=datum or timezone.localdate(), beleg=beleg, benutzer=benutzer)


def kassenstand(mandant) -> Decimal:
    return sum((k.signierter_betrag for k in Kassenbuch.objects.filter(mandant=mandant)), Decimal("0"))


def kontoauszug(mandant, kunde):
    """Buchungen eines Kunden (Rechnungen +, Gutschriften und Zahlungen −) mit laufendem Saldo."""
    posten = []
    for b in Beleg.objects.filter(mandant=mandant, kunde=kunde, abgeschlossen=True,
                                  art__in=[Belegart.RECHNUNG, Belegart.GUTSCHRIFT]):
        vorzeichen = 1 if b.art == Belegart.RECHNUNG else -1
        posten.append((b.belegdatum, 0, b.id, str(b), vorzeichen * b.summe_brutto, b))
    for z in Zahlung.objects.filter(mandant=mandant, beleg__kunde=kunde).select_related("beleg"):
        posten.append((z.datum, 1, z.id, f"Zahlung {z.get_zahlart_display()} zu {z.beleg}", -z.betrag, z.beleg))
    posten.sort(key=lambda p: p[:3])
    saldo, zeilen = Decimal("0"), []
    for datum, _, _, text, betrag, beleg in posten:
        saldo += betrag
        zeilen.append({"datum": datum, "text": text, "betrag": betrag, "saldo": saldo, "beleg": beleg})
    return zeilen, saldo
