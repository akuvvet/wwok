from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

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
    return (beleg.anzahlungen.filter(bei_abschluss=True, storno_zu__isnull=True).aggregate(
        s=models.Sum("betrag"))["s"] or Decimal("0")) + anzahlung_bei_umwandlung(beleg)[0]


def anzahlung_bei_umwandlung(beleg) -> tuple:
    """Rechnung, die aus einer Bestellung umgewandelt wurde: (Betrag, Datum der letzten Zahlung, Umwandlung) der Anzahlungen,
    die bis zur Umwandlung auf die Bestellung gebucht waren. Sie hängen am selben Datensatz und stehen im Ausdruck und in der
    E-Rechnung als erhaltene Zahlung, höchstens bis zum Rechnungsbetrag (der Rest ist Guthaben). Spätere Zahlungen und Stornos
    ändern den ausgestellten Beleg nicht (GoBD). Ohne Umwandlung oder ohne Anzahlung: (0, None, None)."""
    leer = (Decimal("0"), None, None)
    if beleg.art != Belegart.RECHNUNG or not beleg.pk:
        return leer
    umwandlung = beleg.umwandlungen.filter(von_art=Belegart.AUFTRAG, nach_art=Belegart.RECHNUNG).order_by("-pk").first()
    if umwandlung is None:
        return leer
    vorher = Zahlung.objects.filter(beleg=beleg, erfasst_am__lte=umwandlung.am)
    summe = min(vorher.aggregate(s=models.Sum("betrag"))["s"] or Decimal("0"), beleg.summe_brutto)
    if summe <= 0:
        return leer
    return summe, vorher.filter(betrag__gt=0).aggregate(d=models.Max("datum"))["d"], umwandlung


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
    """Noch freier Betrag einer Gutschrift: weder mit Rechnungen verrechnet noch ausgezahlt. Auszahlungen sind negative Zahlungen
    auf der Gutschrift (Entscheidung des Nutzers 19.11.2026), ihr Storno eine positive Gegenbuchung."""
    verrechnet = gutschrift.verrechnungen.aggregate(s=models.Sum("betrag"))["s"] or Decimal("0")
    return gutschrift.summe_brutto - verrechnet + bezahlt(gutschrift)


def offen(beleg) -> Decimal:
    """Offener Betrag; Rechnung: abzüglich Zahlungen, verrechneter Gutschriften und Anzahlungen.
    Proforma: siehe anzahlung.proforma_offen (negativ = Guthaben des Kunden)."""
    if beleg.art == Belegart.PROFORMA:
        from .anzahlung import proforma_offen
        return proforma_offen(beleg)
    if beleg.art == Belegart.RECHNUNG:  # + abgegeben: verrechnete Überzahlung ist nicht mehr Guthaben dieser Rechnung
        return beleg.summe_brutto - bezahlt(beleg) - gutgeschrieben(beleg) - angerechnet(beleg) + abgegeben(beleg)
    return beleg.summe_brutto - bezahlt(beleg) - gutgeschrieben(beleg)


def skonto(beleg):
    """Skonto der Zahlungsbedingung: (Betrag brutto, letzter Tag der Skontofrist) oder None."""
    zb = beleg.zahlungsbedingung
    if beleg.art != Belegart.RECHNUNG or zb is None or zb.skonto_prozent <= 0:
        return None
    betrag = (beleg.summe_brutto * zb.skonto_prozent / 100).quantize(CENT, rounding=ROUND_HALF_UP)
    return (betrag, beleg.belegdatum + timedelta(days=zb.skonto_tage)) if betrag > 0 else None


SKONTO_KLEINBETRAG = Decimal("1.00")  # Kleinstdifferenz (Rundung, Bankspesen): darf ohne Zahlungsbedingung als Skonto weg


def skonto_grenze(beleg, datum=None) -> Decimal:
    """Höchstes Skonto ohne ausdrückliche Bestätigung (Wunsch des Nutzers 24.11.2026: 1.800 € wurden versehentlich als Skonto
    erlassen, weil das Feld den ganzen Rest vorschlug): der Skontobetrag der Zahlungsbedingung, solange die Frist am Zahltag läuft,
    mindestens SKONTO_KLEINBETRAG."""
    datum = datum or timezone.localdate()
    bedingung = skonto(beleg)
    laut_bedingung = bedingung[0] if bedingung and datum <= bedingung[1] else Decimal("0")
    return max(laut_bedingung, SKONTO_KLEINBETRAG)


def aktive_skonti(beleg):
    return beleg.zahlungen.filter(zahlart=Zahlart.SKONTO, storno_zu__isnull=True, storniert_durch__isnull=True)


def skonto_gewaehren(beleg, zahlung, benutzer=None):
    """Nach einem Zahlungseingang: Liegt er in der Skontofrist und ist danach höchstens der Skontobetrag offen, wird der Rest
    als Skonto ausgebucht (eigene Buchung, Zahlart Skonto). Zieht der Kunde mehr ab oder zahlt er zu spät, bleibt der Rest offen."""
    bedingung = skonto(beleg)
    if bedingung is None or zahlung.datum > bedingung[1] or aktive_skonti(beleg).exists():
        return None
    rest = offen(beleg)
    if rest < CENT or rest > bedingung[0]:
        return None
    prozent = f"{beleg.zahlungsbedingung.skonto_prozent.normalize():f}".replace(".", ",")
    return zahlung_buchen(beleg, rest, zahlung.datum, Zahlart.SKONTO, f"Skonto {prozent} % bei Zahlung bis {bedingung[1]:%d.%m.%Y}", benutzer)


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
                  offen_bisher=None, aus_bankumsatz=None, auto_skonto=True) -> Zahlung:
    """offen_bisher: offener Betrag, den die Maske gezeigt hat; weicht er ab, wurde schon gebucht (Doppelklick).
    auto_skonto=False: kein automatisches Skonto nach der Skontofrist-Regel (die Maske bucht das Skonto-Feld selbst)."""
    if beleg.vorlaeufig or Beleg.objects.filter(pk=beleg.pk, vorlaeufig=True).exists():
        raise ValueError("Zahlungen gehen erst nach „Speichern: abschließen & Nummer vergeben“.")
    if offen_bisher is not None:
        Beleg.objects.select_for_update().get(pk=beleg.pk)  # Sperre gegen gleichzeitiges Doppelabsenden
        if offen_bisher != offen(beleg):
            raise ValueError("Der offene Betrag hat sich inzwischen geändert (evtl. schon gebucht). Bitte prüfen und neu buchen.")
    if beleg.art == Belegart.GUTSCHRIFT:  # nur Auszahlung des freien Betrags (negativ) und ihr Storno
        if not beleg.abgeschlossen:
            raise ValueError("Ausgezahlt wird erst nach „Speichern: abschließen & Nummer vergeben“.")
        if betrag > 0 and storno_zu is None:
            raise ValueError("Auf eine Gutschrift wird nur ausgezahlt (Betrag an den Kunden), kein Zahlungseingang gebucht.")
        if zahlart == Zahlart.SKONTO:
            raise ValueError("Eine Auszahlung aus der Gutschrift ist ein Geldfluss – Skonto ist hier nicht möglich.")
        if betrag < 0 and storno_zu is None:
            Beleg.objects.select_for_update().get(pk=beleg.pk)  # Sperre: nicht gleichzeitig verrechnen und auszahlen
            frei = gutschrift_rest(beleg)
            if -betrag > frei + CENT / 2:
                raise ValueError(f"Von {beleg} sind nur noch {frei:.2f} € frei.".replace(".", ",", 1))
    elif beleg.art not in (Belegart.RECHNUNG, Belegart.AUFTRAG, Belegart.PROFORMA) \
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
    zahlung.skonto = None  # automatisch gewährtes Skonto zu diesem Zahlungseingang (für die Meldung in der Maske)
    if auto_skonto and betrag > 0 and storno_zu is None and zahlart != Zahlart.SKONTO and beleg.art == Belegart.RECHNUNG:
        zahlung.skonto = skonto_gewaehren(beleg, zahlung, benutzer)
    return zahlung


@transaction.atomic
def zahlung_mit_skonto(rechnung, betrag, skonto, datum, zahlart, bemerkung="", benutzer=None, offen_bisher=None,
                       bestaetigt=False) -> tuple:
    """Maske „Zahlung buchen“ (Wunsch des Nutzers 19.11.2026): Zahlungseingang plus von Hand gewährtes Skonto. Das Feld Skonto
    schlägt den Rest vor (100 € offen, 80 € gezahlt → 20 € Skonto, Rechnung ausgeglichen); auf 0 gesetzt bleibt der Rest offen.
    Mehr als skonto_grenze() (Zahlungsbedingung in der Frist, sonst 1 €) nur mit bestaetigt=True (Häkchen in der Maske).
    Rückgabe (Zahlung, Skonto-Buchung oder None)."""
    skonto = skonto or Decimal("0")
    if rechnung.art != Belegart.RECHNUNG:
        raise ValueError("Skonto gibt es nur bei Rechnungen.")
    if skonto < 0:
        raise ValueError("Das Skonto darf nicht negativ sein.")
    if skonto and betrag <= 0:
        raise ValueError("Skonto nur zusammen mit einem Zahlungseingang (Betrag größer als 0).")
    if skonto and zahlart == Zahlart.SKONTO:
        raise ValueError("Für Skonto das Feld „Skonto“ nutzen und als Zahlart den Zahlungsweg des Kunden wählen.")
    Beleg.objects.select_for_update().get(pk=rechnung.pk)
    vorher = offen(rechnung)
    if skonto and betrag + skonto > vorher + CENT / 2:
        euro = lambda x: f"{x:.2f}".replace(".", ",")
        raise ValueError(f"Zahlung und Skonto ({euro(betrag + skonto)} €) sind mehr als offen ({euro(vorher)} €). Skonto verringern.")
    grenze = skonto_grenze(rechnung, datum)
    if skonto > grenze + CENT / 2 and not bestaetigt:
        euro = lambda x: f"{x:.2f}".replace(".", ",")
        raise ValueError(f"Nichts gebucht: {euro(skonto)} € Skonto sind mehr als erlaubt ({euro(grenze)} € – laut Zahlungsbedingung bzw. "
                         f"Kleinbetrag). Soll der Rest offen bleiben, Skonto auf 0 setzen; sonst „Höheres Skonto bestätigt“ ankreuzen.")
    zahlung = zahlung_buchen(rechnung, betrag, datum, zahlart, bemerkung, benutzer, offen_bisher=offen_bisher, auto_skonto=False)
    gewaehrt = None
    if skonto:
        gewaehrt = zahlung_buchen(rechnung, skonto, datum, Zahlart.SKONTO, "Skonto gewährt (von Hand bei der Zahlung)", benutzer)
    return zahlung, gewaehrt


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
    if zahlung.zahlart != Zahlart.SKONTO and zahlung.betrag > 0:  # ohne die Zahlung gibt es auch kein Skonto mehr
        for s in list(aktive_skonti(zahlung.beleg)):
            zahlung_stornieren(s, f"Zahlung storniert: {grund}", benutzer, datum)
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
def lastschrift_aufheben(lastschrift, grund, benutzer=None, datum=None) -> Lastschrift:
    """Lastschrift aufheben (Rücklastschrift, Datei nicht eingereicht). Ist der Einzug schon als Zahlung gebucht, wird sie
    storniert: Die Rechnung ist wieder offen und kann erneut eingezogen oder gemahnt werden."""
    from .models import Bankumsatz
    grund = (grund or "").strip()
    if not grund:
        raise ValueError("Bitte einen Grund angeben (z. B. Rücklastschrift, Datei nicht eingereicht).")
    ls = Lastschrift.objects.select_for_update().get(pk=lastschrift.pk)
    if ls.aufgehoben:
        raise ValueError("Die Lastschrift ist bereits aufgehoben.")
    z = ls.zahlung
    if z is not None and not Zahlung.objects.filter(storno_zu=z).exists():
        zahlung_stornieren(z, grund, benutzer, datum)
        if z.aus_bankumsatz_id:  # die Gutschrift der Bank bleibt erledigt, sonst könnte sie ein zweites Mal gebucht werden
            Bankumsatz.objects.filter(pk=z.aus_bankumsatz_id, status=Bankumsatz.OFFEN).update(status=Bankumsatz.ZUGEORDNET)
    ls.aufgehoben, ls.aufgehoben_grund = True, grund[:300]
    ls.aufgehoben_von, ls.aufgehoben_am = benutzer, timezone.now()
    ls.save()
    return ls


@transaction.atomic
def kasse_buchen(mandant, art, betrag, text, datum=None, beleg=None, benutzer=None) -> Kassenbuch:
    """Fortlaufend nummerierter, unveränderlicher Kassenbucheintrag."""
    if art not in (Kassenbuch.EINNAHME, Kassenbuch.AUSGABE):
        raise ValueError("Kassenbuchungen sind Einnahme oder Ausgabe.")
    Mandant.objects.select_for_update().get(pk=mandant.pk)  # Sperre, damit Nummern lückenlos bleiben
    datum = datum or timezone.localdate()
    if datum > timezone.localdate():
        raise ValueError("Kassenbuchungen dürfen nicht in der Zukunft liegen.")
    letzte_buchung = Kassenbuch.objects.filter(mandant=mandant).aggregate(m=models.Max("datum"))["m"]
    if letzte_buchung is not None and datum < letzte_buchung:
        raise ValueError(f"Kassenbuchung nicht möglich: Das Datum liegt vor der letzten Buchung im Kassenbuch ({letzte_buchung:%d.%m.%Y}). "
                         "Das Kassenbuch ist gesperrt, rückwirkend wird nicht gebucht.")
    if art == Kassenbuch.AUSGABE and betrag > kassenstand(mandant):
        stand, summe = f"{kassenstand(mandant):.2f}".replace(".", ","), f"{betrag:.2f}".replace(".", ",")
        raise ValueError(f"Die Kasse darf nicht ins Minus laufen: Kassenstand {stand} €, gebucht werden sollen {summe} €. "
                         "Bitte zuerst eine Einlage buchen.")
    letzte = Kassenbuch.objects.filter(mandant=mandant).aggregate(m=models.Max("nummer"))["m"] or 0
    return Kassenbuch.objects.create(mandant=mandant, nummer=letzte + 1, art=art, betrag=betrag, text=text,
                                     datum=datum, beleg=beleg, benutzer=benutzer)


def kassenstand(mandant) -> Decimal:
    dez = models.DecimalField(max_digits=12, decimal_places=2)
    vorzeichen = models.Case(models.When(art=Kassenbuch.EINNAHME, then=models.F("betrag")),
                             default=-models.F("betrag"), output_field=dez)
    return Kassenbuch.objects.filter(mandant=mandant).aggregate(s=models.Sum(vorzeichen, output_field=dez))["s"] or Decimal("0")


def zeitraum(zeilen, von=None, bis=None):
    """Kontoauszug auf einen Zeitraum begrenzen (Wunsch des Nutzers 20.11.2026): Buchungen vor „von“ fließen in den
    Anfangssaldo (erste Zeile), Buchungen nach „bis“ entfallen. Rückgabe (zeilen, Saldo am Ende des Zeitraums).
    Gilt für Kunden- und Lieferantenkontoauszug (beide nach Datum sortiert, mit laufendem Saldo)."""
    anfang, auswahl = Decimal("0"), []
    for z in zeilen:
        if von and z["datum"] < von:
            anfang = z["saldo"]
        elif bis and z["datum"] > bis:
            break
        else:
            auswahl.append(z)
    if von:
        auswahl.insert(0, {"datum": von, "text": "Anfangssaldo", "betrag": None, "info": None, "saldo": anfang, "anfang": True})
    return auswahl, (auswahl[-1]["saldo"] if auswahl else anfang)


def zeitraum_aus(get):
    """(von, bis, Text) aus ?von=JJJJ-MM-TT&bis=…; ungültige Angaben werden ignoriert, „von“ nach „bis“ getauscht."""
    from datetime import date

    def tag(name):
        try:
            return date.fromisoformat(get.get(name, ""))
        except ValueError:
            return None
    von, bis = tag("von"), tag("bis")
    if von and bis and von > bis:
        von, bis = bis, von
    if von and bis:
        text = f"{von:%d.%m.%Y} – {bis:%d.%m.%Y}"
    elif von:
        text = f"ab {von:%d.%m.%Y}"
    elif bis:
        text = f"bis {bis:%d.%m.%Y}"
    else:
        text = "alle Buchungen"
    return von, bis, text


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


def zahlungsweg(beleg):
    """Lückenloser Weg des Geldes zu einer Rechnung und ihren Gutschriften, zeitlich geordnet: Rechnung, Zahlungseingänge,
    Gutschriften, Verrechnungen, Auszahlungen an den Kunden, jeweils mit Zahlart, Herkunft (Kontoauszug, Kassenbuch,
    Lastschriftdatei), Benutzer und Grund. „stand“ ist der offene Betrag der Rechnung nach dem Schritt.
    Rückgabe None (Belegart ohne Weg) oder {rechnung, zeilen, gutschriften, verrechnet, frei, eingang, ausgezahlt, stand, ueber}."""
    if beleg.art == Belegart.RECHNUNG and beleg.abgeschlossen:
        rechnung = beleg
    elif beleg.art == Belegart.GUTSCHRIFT and beleg.abgeschlossen:
        rechnung = beleg.vorgaenger if beleg.vorgaenger_id and beleg.vorgaenger.art == Belegart.RECHNUNG else None
    else:
        return None
    gutschriften = (list(rechnung.folgebelege.filter(art=Belegart.GUTSCHRIFT, abgeschlossen=True).select_related("ueber_rechnung_von"))
                    if rechnung else [beleg])
    eigene_gs = {g.pk for g in gutschriften}
    zeilen = []

    def zeile(datum, rang, zeit, vorgang, weg="", betrag=None, wirkung=None, benutzer=None, text="", ziel=None, art=""):
        zeilen.append({"datum": datum, "rang": rang, "zeit": zeit, "vorgang": vorgang, "weg": weg, "betrag": betrag, "wirkung": wirkung,
                       "benutzer": benutzer, "text": text, "ziel": ziel, "art": art, "stand": None})

    eingang = ausgezahlt = gs_ausgezahlt = Decimal("0")
    if rechnung:
        zeile(rechnung.belegdatum, 0, None, f"Rechnung {rechnung.nummer} ausgestellt", betrag=rechnung.summe_brutto,
              wirkung=rechnung.summe_brutto, art="beleg")
        kasse = list(Kassenbuch.objects.filter(mandant=rechnung.mandant, beleg=rechnung).order_by("nummer"))
        zahlungen = rechnung.zahlungen.select_related("benutzer", "aus_bankumsatz").prefetch_related("lastschriften").order_by("erfasst_am", "pk")
        for z in zahlungen:
            weg = z.get_zahlart_display()
            umsatz = z.aus_bankumsatz or getattr(z, "bankumsatz", None)
            if umsatz is not None:
                weg += f" – Kontoauszug {umsatz.buchungsdatum:%d.%m.%Y}" + (f", {umsatz.name}" if umsatz.name else "")
            elif z.zahlart == Zahlart.BAR:
                art = Kassenbuch.EINNAHME if z.betrag > 0 else Kassenbuch.AUSGABE
                eintrag = next((k for k in kasse if k.art == art and k.betrag == abs(z.betrag) and k.datum == z.datum), None)
                if eintrag is not None:
                    kasse.remove(eintrag)
                    weg += f" – Kassenbuch Nr. {eintrag.nummer}"
            for ls in z.lastschriften.all():
                weg += f" – Lastschriftdatei {ls.datei}, Mandat {ls.mandatsreferenz}"
            ist_eingang = (z.betrag > 0) != bool(z.storno_zu_id)  # das Storno eines Eingangs mindert den Eingang, nicht die Auszahlung
            if z.storno_zu_id:
                vorgang = "Storno eines Zahlungseingangs" if ist_eingang else "Storno einer Auszahlung"
            elif z.zahlart == Zahlart.SKONTO:
                vorgang = "Skonto gewährt" if z.betrag > 0 else "Skonto zurückgenommen"
            else:
                vorgang = "Zahlungseingang" if z.betrag > 0 else "Auszahlung an den Kunden"
            if z.zahlart != Zahlart.SKONTO:
                if ist_eingang:
                    eingang += z.betrag
                else:
                    ausgezahlt -= z.betrag
            zeile(z.datum, 1, z.erfasst_am, vorgang, weg, z.betrag, -z.betrag, z.benutzer, z.storno_grund or z.bemerkung,
                  art="aus" if z.betrag < 0 and not z.storno_zu_id else "zahlung")
        for a in rechnung.anzahlungen.select_related("quelle", "benutzer"):
            zeile(a.datum, 1, a.erfasst_am,
                  ("Verrechnung aufgehoben: " if a.storno_zu_id else "Guthaben verrechnet: ") + f"{a.quelle} → Rechnung {rechnung.nummer}",
                  "Verrechnung (kein Geldfluss)", a.betrag, -a.betrag, a.benutzer, a.storno_grund, a.quelle, "verrechnung")
        for a in rechnung.anzahlung_verrechnungen.select_related("rechnung", "benutzer"):
            zeile(a.datum, 1, a.erfasst_am,
                  ("Verrechnung aufgehoben: " if a.storno_zu_id else "Überzahlung weiterverrechnet: ") + f"Rechnung {rechnung.nummer} → {a.rechnung}",
                  "Verrechnung (kein Geldfluss)", a.betrag, a.betrag, a.benutzer, a.storno_grund, a.rechnung, "verrechnung")
    for g in gutschriften:
        text = ""
        if g.ueber_rechnung_grund:
            text = f"Über dem Rechnungsbetrag – Begründung: {g.ueber_rechnung_grund}" + (f" ({g.ueber_rechnung_von})" if g.ueber_rechnung_von_id else "")
        angelegt = g.angelegt_am if timezone.localtime(g.angelegt_am).date() == g.belegdatum else None  # am selben Tag: Uhrzeit ordnet ein
        zeile(g.belegdatum, 2, angelegt, f"Gutschrift {g.nummer} ausgestellt", "noch kein Geldfluss: Guthaben des Kunden", g.summe_brutto,
              None, None, text, g if g.pk != beleg.pk else None, "beleg")
        for z in g.zahlungen.select_related("benutzer").order_by("erfasst_am", "pk"):  # Auszahlung direkt aus der Gutschrift
            vorgang = f"Storno einer Auszahlung aus Gutschrift {g.nummer}" if z.storno_zu_id else f"Auszahlung aus Gutschrift {g.nummer}"
            gs_ausgezahlt -= z.betrag
            zeile(z.datum, 3, z.erfasst_am, vorgang, z.get_zahlart_display(), z.betrag, None, z.benutzer, z.storno_grund or z.bemerkung,
                  g if g.pk != beleg.pk else None, "aus" if z.betrag < 0 and not z.storno_zu_id else "zahlung")
    bedingung = models.Q(gutschrift__in=gutschriften)
    if rechnung:
        bedingung |= models.Q(rechnung=rechnung)
    verrechnet = Decimal("0")
    for v in Gutschriftverrechnung.objects.filter(bedingung).select_related("gutschrift", "rechnung", "benutzer"):
        hier = rechnung is not None and v.rechnung_id == rechnung.pk
        if v.gutschrift_id in eigene_gs:
            verrechnet += v.betrag
        ziel = v.rechnung if not hier else (v.gutschrift if v.gutschrift_id not in eigene_gs else None)  # fremder Beleg: Verweis auf dessen Zahlungsseite
        zeile(v.datum, 3, v.erfasst_am,
              ("Verrechnung aufgehoben: " if v.storno_zu_id else "Gutschrift verrechnet: ") + f"{v.gutschrift.nummer} → Rechnung {v.rechnung.nummer}",
              "Verrechnung (kein Geldfluss)", v.betrag, -v.betrag if hier else None, v.benutzer, v.storno_grund, ziel, "verrechnung")
    frueh = timezone.now().replace(year=1971)
    zeilen.sort(key=lambda z: (z["datum"], z["zeit"] or frueh, z["rang"]))
    stand = Decimal("0")
    for z in zeilen:
        if z["wirkung"] is not None:
            stand += z["wirkung"]
            z["stand"] = stand
    summe = sum((g.summe_brutto for g in gutschriften), Decimal("0"))
    return {"rechnung": rechnung, "zeilen": zeilen, "gutschriften": summe, "verrechnet": verrechnet, "frei": summe - verrechnet - gs_ausgezahlt,
            "eingang": eingang, "ausgezahlt": ausgezahlt + gs_ausgezahlt, "gs_ausgezahlt": gs_ausgezahlt, "stand": stand if rechnung else None,
            "ueber": summe - rechnung.summe_brutto if rechnung and summe > rechnung.summe_brutto + CENT else None}
