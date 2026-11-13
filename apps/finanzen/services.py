from datetime import timedelta
from decimal import Decimal

from django.db import models, transaction
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.verkauf.models import Beleg, Belegart

from apps.kern.models import Mandant

from .models import Anzahlungsverrechnung, Gutschriftverrechnung, Kassenbuch, Lastschrift, Mahnstufe, Mahnung, Zahlart, Zahlung

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
    """Auf die Rechnung verrechnete Gutschriften; eine Gutschrift allein mindert die Rechnung nicht."""
    return beleg.gutschrift_verrechnungen.aggregate(s=models.Sum("betrag"))["s"] or Decimal("0")


def angerechnet(beleg) -> Decimal:
    """Auf die Rechnung verrechnete Guthaben (Zahlungen auf Proforma/Bestellung, Überzahlung anderer Rechnungen)."""
    return beleg.anzahlungen.aggregate(s=models.Sum("betrag"))["s"] or Decimal("0")


def abgegeben(beleg) -> Decimal:
    """Von diesem Beleg (Überzahlung, Proforma, Bestellung) an andere Rechnungen verrechnetes Guthaben."""
    return beleg.anzahlung_verrechnungen.aggregate(s=models.Sum("betrag"))["s"] or Decimal("0")


def angerechnet_im_druck(beleg) -> Decimal:
    """Anzahlungen, die beim Abschluss der Rechnung verrechnet wurden: Sie stehen unveränderlich im Ausdruck und in der
    E-Rechnung. Spätere Verrechnungen oder Aufhebungen ändern den ausgestellten Beleg nicht (wie Gutschriftverrechnungen)."""
    return beleg.anzahlungen.filter(bei_abschluss=True, storno_zu__isnull=True).aggregate(
        s=models.Sum("betrag"))["s"] or Decimal("0")


def summen_je_beleg(belege) -> dict:
    """Zahlungen, verrechnete Gutschriften (auf Rechnungen) und Verrechnungen (von Gutschriften) für viele Belege in je einer Abfrage.
    Rückgabe: {"bezahlt": {pk: Betrag}, "gutgeschrieben": {pk: Betrag}, "verrechnet": {pk: Betrag},
    "angerechnet": {pk: Betrag} (Anzahlungen auf Rechnungen), "abgegeben": {pk: Betrag} (von Proforma/Bestellung verrechnet)};
    fehlende Schlüssel = 0."""
    ids = [b.pk for b in belege]
    def summe(qs, feld):
        return {z[feld]: z["s"] for z in qs.order_by().values(feld).annotate(s=models.Sum("betrag"))}
    return {"bezahlt": summe(Zahlung.objects.filter(beleg_id__in=ids), "beleg_id"),
            "gutgeschrieben": summe(Gutschriftverrechnung.objects.filter(rechnung_id__in=ids), "rechnung_id"),
            "verrechnet": summe(Gutschriftverrechnung.objects.filter(gutschrift_id__in=ids), "gutschrift_id"),
            "angerechnet": summe(Anzahlungsverrechnung.objects.filter(rechnung_id__in=ids), "rechnung_id"),
            "abgegeben": summe(Anzahlungsverrechnung.objects.filter(quelle_id__in=ids), "quelle_id")}


def gutschrift_rest(gutschrift) -> Decimal:
    """Noch nicht verrechneter Betrag einer Gutschrift."""
    return gutschrift.summe_brutto - (gutschrift.verrechnungen.aggregate(s=models.Sum("betrag"))["s"] or Decimal("0"))


def offen(beleg) -> Decimal:
    """Offener Betrag; Rechnung: abzüglich Zahlungen, verrechneter Gutschriften und Anzahlungen.
    Proforma: siehe anzahlung.proforma_offen (negativ = Guthaben des Kunden)."""
    if beleg.art == Belegart.PROFORMA:
        from .anzahlung import proforma_offen
        return proforma_offen(beleg)
    if beleg.art == Belegart.RECHNUNG:  # + abgegeben: verrechnete Überzahlung ist nicht mehr Guthaben dieser Rechnung
        return beleg.summe_brutto - bezahlt(beleg) - gutgeschrieben(beleg) - angerechnet(beleg) + abgegeben(beleg)
    return beleg.summe_brutto - bezahlt(beleg) - gutgeschrieben(beleg)


def faellig_am(beleg):
    return beleg.faellig_am or beleg.belegdatum


def mahnfaellig_ab(beleg):
    """Belegdatum + Tage 'Mahnung fällig' der Zahlungsbedingung (ohne Bedingung: Fälligkeit)."""
    zb = beleg.zahlungsbedingung
    return beleg.belegdatum + timedelta(days=max(zb.tage_mahnung, zb.tage_netto)) if zb else faellig_am(beleg)  # nie vor der Fälligkeit


def offene_rechnungen(mandant, kunde=None):
    """Abgeschlossene Rechnungen mit offenem Betrag, als Liste von Zeilen (älteste zuerst)."""
    return offene_rechnungen_zeilen(_rechnungen_mit_summen(mandant, kunde))


def offene_rechnungen_zeilen(belege):
    """Zeilen aus einem Queryset von _rechnungen_mit_summen; Rechnungen unter einem Cent Rest entfallen."""
    heute = timezone.localdate()
    zeilen = []
    for b in belege:
        # Anzahlungen zählen als bezahlt, weiterverrechnete Überzahlung nicht mehr
        bez, gut = b.summe_bezahlt + b.summe_angerechnet - b.summe_abgegeben, b.summe_gutgeschrieben
        rest = b.summe_brutto - bez - gut
        if abs(rest) < CENT:
            continue
        faellig = faellig_am(b)
        zeilen.append({"beleg": b, "bezahlt": bez, "gutschrift": gut, "erledigt": bez + gut, "offen": rest,
                       "faellig": faellig, "ueberfaellig": max((heute - faellig).days, 0) if rest > 0 else 0,  # Überzahlung ist nie überfällig
                       "mahnstufe": b.letzte_mahnstufe or 0})
    return zeilen


def _rechnungen_mit_summen(mandant, kunde=None):
    """Abgeschlossene Rechnungen mit Zahlungs-/Gutschriftsummen und letzter Mahnstufe in einer einzigen Abfrage.
    Vollständig bezahlte Rechnungen werden schon in der Datenbank ausgefiltert (grob; die Centgrenze prüft der Aufrufer
    exakt in Decimal, damit Rundungsfehler der Datenbank nichts verfälschen)."""
    null = models.Value(Decimal("0"), output_field=models.DecimalField(max_digits=14, decimal_places=2))

    def summe(modell, feld):
        return models.Subquery(
            modell.objects.filter(**{feld: models.OuterRef("pk")}).order_by().values(feld)
            .annotate(s=models.Sum("betrag")).values("s"), output_field=models.DecimalField(max_digits=14, decimal_places=2))

    letzte = Mahnung.objects.filter(beleg=models.OuterRef("pk")).order_by("-stufe")
    belege = Beleg.objects.filter(mandant=mandant, art=Belegart.RECHNUNG, abgeschlossen=True) \
        .select_related("kunde", "zahlungsbedingung") \
        .annotate(summe_bezahlt=Coalesce(summe(Zahlung, "beleg"), null),
                  summe_gutgeschrieben=Coalesce(summe(Gutschriftverrechnung, "rechnung"), null),
                  summe_angerechnet=Coalesce(summe(Anzahlungsverrechnung, "rechnung"), null),
                  summe_abgegeben=Coalesce(summe(Anzahlungsverrechnung, "quelle"), null),
                  letzte_mahnstufe=models.Subquery(letzte.values("stufe")[:1]),
                  letzte_mahnung_datum=models.Subquery(letzte.values("datum")[:1])) \
        .annotate(rest_db=models.F("summe_brutto") - models.F("summe_bezahlt") - models.F("summe_gutgeschrieben")
                  - models.F("summe_angerechnet") + models.F("summe_abgegeben")) \
        .filter(models.Q(rest_db__gt=Decimal("0.005")) | models.Q(rest_db__lt=Decimal("-0.005"))) \
        .order_by("belegdatum", "id")
    if kunde is not None:
        belege = belege.filter(kunde=kunde)
    return belege


def offene_gutschriften(mandant, kunde=None):
    """Abgeschlossene Gutschriften mit unverrechnetem Betrag als Minusposten, in der Form von offene_rechnungen."""
    belege = Beleg.objects.filter(mandant=mandant, art=Belegart.GUTSCHRIFT, abgeschlossen=True) \
        .select_related("kunde").order_by("belegdatum", "id")
    if kunde is not None:
        belege = belege.filter(kunde=kunde)
    zeilen = []
    for g in belege:
        rest = gutschrift_rest(g)
        if rest >= CENT:
            zeilen.append({"beleg": g, "ist_gutschrift": True, "bezahlt": Decimal("0"), "gutschrift": Decimal("0"),
                           "erledigt": g.summe_brutto - rest, "offen": -rest, "faellig": None, "ueberfaellig": 0, "mahnstufe": 0})
    return zeilen


def kunde_offen(mandant, kunde) -> Decimal:
    return sum((z["offen"] for z in offene_rechnungen(mandant, kunde) + offene_gutschriften(mandant, kunde)), Decimal("0"))


@transaction.atomic
def gutschrift_verrechnen(gutschrift, rechnung, betrag, benutzer=None, offen_bisher=None) -> Gutschriftverrechnung:
    """Verrechnet einen Teil der Gutschrift. Die Rechnung darf dabei ins Minus gehen: Hat der Kunde schon bezahlt,
    wird die Überzahlung danach als Rückzahlung gebucht. offen_bisher ist der offene Betrag, den die Maske gezeigt
    hat; weicht er ab, wurde schon gebucht (Doppelklick, veraltete Seite) und es passiert nichts."""
    Beleg.objects.select_for_update().get(pk=gutschrift.pk)  # Sperre, damit nichts doppelt verrechnet wird
    if offen_bisher is not None and offen_bisher != offen(rechnung):
        raise ValueError("Der offene Betrag hat sich inzwischen geändert. Bitte prüfen und neu verrechnen.")
    if gutschrift.art != Belegart.GUTSCHRIFT or not gutschrift.abgeschlossen:
        raise ValueError("Verrechnet werden nur abgeschlossene Gutschriften.")
    if rechnung.art != Belegart.RECHNUNG or not rechnung.abgeschlossen:
        raise ValueError("Verrechnet wird nur mit abgeschlossenen Rechnungen.")
    if (gutschrift.mandant_id, gutschrift.kunde_id) != (rechnung.mandant_id, rechnung.kunde_id):
        raise ValueError("Gutschrift und Rechnung gehören nicht zum selben Kunden.")
    if betrag <= 0:
        raise ValueError("Der Betrag muss größer als 0 sein.")
    if betrag > gutschrift_rest(gutschrift):
        raise ValueError(f"Von {gutschrift} sind nur noch {gutschrift_rest(gutschrift):.2f} € offen.".replace(".", ",", 1))
    return Gutschriftverrechnung.objects.create(mandant=rechnung.mandant, gutschrift=gutschrift, rechnung=rechnung,
                                                betrag=betrag, benutzer=benutzer)


@transaction.atomic
def verrechnung_aufheben(verrechnung, grund, benutzer=None) -> Gutschriftverrechnung:
    """Hebt eine Verrechnung per Gegenbuchung auf; der Grund ist Pflicht."""
    grund = (grund or "").strip()
    if not grund:
        raise ValueError("Bitte einen Grund angeben.")
    Gutschriftverrechnung.objects.select_for_update().get(pk=verrechnung.pk)  # Sperre gegen doppeltes Aufheben
    if verrechnung.storno_zu_id:
        raise ValueError("Eine Aufhebung kann nicht erneut aufgehoben werden.")
    if Gutschriftverrechnung.objects.filter(storno_zu=verrechnung).exists():
        raise ValueError("Diese Verrechnung ist bereits aufgehoben.")
    return Gutschriftverrechnung.objects.create(
        mandant=verrechnung.mandant, gutschrift=verrechnung.gutschrift, rechnung=verrechnung.rechnung,
        betrag=-verrechnung.betrag, benutzer=benutzer, storno_zu=verrechnung, storno_grund=grund[:300])


def mahnvorschlag(mandant, heute=None):
    """Rechnungen, für die jetzt die nächste Mahnstufe fällig ist."""
    heute = heute or timezone.localdate()
    alle = stufen(mandant)
    vorschlag = []
    eingezogen = Lastschrift.objects.filter(beleg=models.OuterRef("pk"), aufgehoben=False)
    for z in offene_rechnungen_zeilen(_rechnungen_mit_summen(mandant).exclude(models.Exists(eingezogen))):  # per Lastschrift eingezogen: keine Mahnung
        if z["offen"] <= 0:
            continue
        b = z["beleg"]
        naechste = z["mahnstufe"] + 1
        if naechste not in alle:
            continue
        if naechste == 1:
            faellig = heute > mahnfaellig_ab(b)
        else:
            faellig = heute >= b.letzte_mahnung_datum + timedelta(days=alle[naechste].tage_seit_vorheriger)
        if faellig:
            vorschlag.append({**z, "stufe": alle[naechste]})
    return vorschlag


@transaction.atomic
def mahnung_erstellen(beleg, stufe_nr, benutzer=None, heute=None) -> Mahnung:
    heute = heute or timezone.localdate()
    if beleg.art != Belegart.RECHNUNG or not beleg.abgeschlossen:
        raise ValueError("Gemahnt werden nur abgeschlossene Rechnungen.")
    bisher = beleg.mahnungen.aggregate(m=models.Max("stufe"))["m"] or 0
    if stufe_nr != bisher + 1:
        raise ValueError(f"Als Nächstes ist Mahnstufe {bisher + 1} dran, nicht {stufe_nr}.")
    stufe = stufen(beleg.mandant)[stufe_nr]
    rest = offen(beleg)
    if rest <= 0:
        raise ValueError(f"{beleg} ist nicht offen.")
    return Mahnung.objects.create(
        beleg=beleg, stufe=stufe.stufe, bezeichnung=stufe.bezeichnung, datum=heute, offener_betrag=rest,
        gebuehr=stufe.gebuehr, frist=heute + timedelta(days=stufe.zahlungsfrist_tage), benutzer=benutzer)


@transaction.atomic
def zahlung_buchen(beleg, betrag, datum, zahlart, bemerkung="", benutzer=None, storno_zu=None, storno_grund="",
                  offen_bisher=None, aus_bankumsatz=None) -> Zahlung:
    """offen_bisher: offener Betrag, den die Maske gezeigt hat; weicht er ab, wurde schon gebucht (Doppelklick)."""
    if beleg.vorlaeufig or Beleg.objects.filter(pk=beleg.pk, vorlaeufig=True).exists():
        raise ValueError("Zahlungen gehen erst nach „Speichern: abschließen & Nummer vergeben“.")
    if offen_bisher is not None:
        Beleg.objects.select_for_update().get(pk=beleg.pk)  # Sperre gegen gleichzeitiges Doppelabsenden
        if offen_bisher != offen(beleg):
            raise ValueError("Der offene Betrag hat sich inzwischen geändert (evtl. schon gebucht). Bitte prüfen und neu buchen.")
    if beleg.art not in (Belegart.RECHNUNG, Belegart.AUFTRAG, Belegart.PROFORMA) \
            or (beleg.art in (Belegart.RECHNUNG, Belegart.PROFORMA) and not beleg.abgeschlossen):
        raise ValueError("Zahlungen können nur auf abgeschlossene Rechnungen, Proforma-Rechnungen oder Bestellungen gebucht werden.")
    if beleg.art == Belegart.PROFORMA and beleg.proforma_storno and storno_zu is None:
        raise ValueError(f"{beleg} ist storniert – keine weiteren Zahlungen.")
    if betrag == 0:
        raise ValueError("Betrag darf nicht 0 sein.")
    if betrag < 0 and beleg.art == Belegart.RECHNUNG and beleg.anzahlung_verrechnungen.exists():
        # Rückzahlung einer Überzahlung, die schon teils verrechnet ist: höchstens das freie Guthaben
        Beleg.objects.select_for_update().get(pk=beleg.pk)
        frei = max(-offen(beleg), Decimal("0"))
        if -betrag > frei + CENT / 2:
            raise ValueError(f"Frei sind nur {frei:.2f} € Guthaben; der Rest ist mit anderen Rechnungen verrechnet.".replace(".", ",", 1)
                             + " Bitte zuerst die Verrechnung aufheben.")
    if betrag < 0 and beleg.art in (Belegart.PROFORMA, Belegart.AUFTRAG):
        # Rückzahlung/Storno: was schon mit einer Rechnung verrechnet ist, muss an der Proforma/Bestellung bleiben
        Beleg.objects.select_for_update().get(pk=beleg.pk)
        from .anzahlung import abgegeben
        verrechnet = abgegeben(beleg)
        if bezahlt(beleg) + betrag < verrechnet - CENT:
            raise ValueError(f"{verrechnet:.2f} € dieser Zahlungen sind mit einer Rechnung verrechnet.".replace(".", ",", 1)
                             + " Bitte zuerst die Verrechnung aufheben (Zahlungen der Rechnung), dann zurückzahlen oder stornieren.")
    zahlung = Zahlung.objects.create(mandant=beleg.mandant, beleg=beleg, betrag=betrag, datum=datum, zahlart=zahlart,
                                     bemerkung=bemerkung, benutzer=benutzer, storno_zu=storno_zu, storno_grund=storno_grund,
                                     aus_bankumsatz=aus_bankumsatz)
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
    umsaetze = {u.pk: u for u in Bankumsatz.objects.filter(zahlung=zahlung)}
    if zahlung.aus_bankumsatz_id:
        umsaetze.setdefault(zahlung.aus_bankumsatz_id, Bankumsatz.objects.get(pk=zahlung.aus_bankumsatz_id))
    for u in umsaetze.values():
        # Aufgeteilter Umsatz: frei erst, wenn keine seiner Zahlungen mehr gilt (sonst würde er doppelt gebucht)
        uebrig = Zahlung.objects.filter(aus_bankumsatz=u, storno_zu__isnull=True, storniert_durch__isnull=True).exclude(pk=zahlung.pk)
        if uebrig.exists():
            if u.zahlung_id == zahlung.pk:
                u.zahlung = uebrig.order_by("pk").first()
                u.save(update_fields=["zahlung"])
            continue
        u.zahlung, u.status = None, Bankumsatz.OFFEN  # Umsatz kann neu zugeordnet werden
        u.save(update_fields=["zahlung", "status"])
    return gegen


@transaction.atomic
def kasse_buchen(mandant, art, betrag, text, datum=None, beleg=None, benutzer=None) -> Kassenbuch:
    """Fortlaufend nummerierter, unveränderlicher Kassenbucheintrag."""
    if art not in (Kassenbuch.EINNAHME, Kassenbuch.AUSGABE):
        raise ValueError("Kassenbuchungen sind Einnahme oder Ausgabe.")
    Mandant.objects.select_for_update().get(pk=mandant.pk)  # Sperre, damit Nummern lückenlos bleiben
    letzte = Kassenbuch.objects.filter(mandant=mandant).aggregate(m=models.Max("nummer"))["m"] or 0
    return Kassenbuch.objects.create(mandant=mandant, nummer=letzte + 1, art=art, betrag=betrag, text=text,
                                     datum=datum or timezone.localdate(), beleg=beleg, benutzer=benutzer)


def kassenstand(mandant) -> Decimal:
    dez = models.DecimalField(max_digits=12, decimal_places=2)
    vorzeichen = models.Case(models.When(art=Kassenbuch.EINNAHME, then=models.F("betrag")),
                             default=-models.F("betrag"), output_field=dez)
    return Kassenbuch.objects.filter(mandant=mandant).aggregate(s=models.Sum(vorzeichen, output_field=dez))["s"] or Decimal("0")


def kontoauszug(mandant, kunde):
    """Buchungen eines Kunden (Rechnungen +, Gutschriften und Zahlungen −) mit laufendem Saldo."""
    posten = []
    for b in Beleg.objects.filter(mandant=mandant, kunde=kunde, abgeschlossen=True,
                                  art__in=[Belegart.RECHNUNG, Belegart.GUTSCHRIFT]):
        vorzeichen = 1 if b.art == Belegart.RECHNUNG else -1
        posten.append((b.belegdatum, 0, b.id, str(b), vorzeichen * b.summe_brutto, b))
    for z in Zahlung.objects.filter(mandant=mandant, beleg__kunde=kunde).select_related("beleg"):
        posten.append((z.datum, 1, z.id, f"Zahlung {z.get_zahlart_display()} zu {z.beleg}", -z.betrag, z.beleg))
    for v in Anzahlungsverrechnung.objects.filter(mandant=mandant, rechnung__kunde=kunde).select_related("quelle", "rechnung"):
        text = (f"Verrechnung aufgehoben: {v.quelle} → {v.rechnung} ({v.storno_grund})" if v.storno_zu_id
                else f"Verrechnet: Guthaben {v.quelle} → {v.rechnung}")
        posten.append((v.datum, 2, v.id, text, None, v.rechnung, v.betrag))
    posten.sort(key=lambda p: p[:3])
    saldo, zeilen = Decimal("0"), []
    for datum, _, _, text, betrag, beleg, *info in posten:
        if betrag is None:  # nur zur Nachvollziehbarkeit: ändert den Saldo nicht
            zeilen.append({"datum": datum, "text": text, "betrag": None, "info": info[0], "saldo": saldo, "beleg": beleg})
            continue
        saldo += betrag
        zeilen.append({"datum": datum, "text": text, "betrag": betrag, "saldo": saldo, "beleg": beleg})
    return zeilen, saldo
