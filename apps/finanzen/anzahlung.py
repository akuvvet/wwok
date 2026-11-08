"""
Vorkasse und Anzahlungen (README Abschnitt 18, Proforma-Rechnung).

Die Proforma-Rechnung (PF) entsteht aus einer Bestellung, hat immer die Zahlungsbedingung Vorkasse und nimmt Zahlungen an.
Erst wenn sie vollständig bezahlt ist, wird sie in eine Rechnung umgewandelt; beim Abschluss der Rechnung wird die Zahlung
mit der Rechnung verrechnet (Anzahlungsverrechnung), nicht umgebucht. Datum, Zahlart, Bank und Kasse bleiben an der Proforma.
Zahlungen auf eine Bestellung (Shop/PayPal) werden genauso mit der späteren Rechnung verrechnet.

Offen (Proforma) = Brutto, solange sie auf die Umwandlung wartet, + bereits verrechnet − Zahlungen; negativ = Guthaben.
Kauft der Kunde nicht, wird zurückgezahlt (negative Zahlung) und die Proforma mit einem eigenen Storno-Beleg storniert.
"""
from decimal import Decimal

from django.db import models, transaction
from django.utils import timezone

from apps.verkauf.models import Beleg, Belegart, kopiere_positionen, zahlungsbedingung_bezahlt

from .models import Anzahlungsverrechnung, Zahlung

CENT = Decimal("0.01")
NULL = Decimal("0")
QUELLARTEN = (Belegart.PROFORMA, Belegart.AUFTRAG)
GUTHABENARTEN = QUELLARTEN + (Belegart.RECHNUNG,)  # Überzahlte Rechnung gibt ihr Guthaben ebenfalls ab

STATUS_TEXT = {
    "entwurf": "Entwurf", "offen": "offen", "teilbezahlt": "teilbezahlt", "bezahlt": "bezahlt",
    "ueberzahlt": "überzahlt (Guthaben)", "umgewandelt": "in Rechnung umgewandelt", "guthaben": "umgewandelt, Guthaben offen",
    "erstattet": "erstattet", "storniert": "storniert",
}


def _summe(qs) -> Decimal:
    return qs.aggregate(s=models.Sum("betrag"))["s"] or NULL


def bezahlt(beleg) -> Decimal:
    return _summe(Zahlung.objects.filter(beleg=beleg))


def abgegeben(quelle) -> Decimal:
    """Von dieser Proforma/Bestellung an Rechnungen verrechnete Zahlungen."""
    return _summe(Anzahlungsverrechnung.objects.filter(quelle=quelle))


def wartet(pf) -> bool:
    """Proforma, die noch auf Zahlung und Umwandlung wartet (weder umgewandelt noch storniert)."""
    return not Beleg.objects.filter(vorgaenger=pf, art__in=(Belegart.RECHNUNG, Belegart.PROFORMA_STORNO)).exists()


def bestellung_guthaben(pf) -> Decimal:
    """Freie Anzahlung auf der Bestellung, aus der die Proforma stammt (z. B. Shop/PayPal): zählt für die Vollzahlung mit."""
    vorg = pf.vorgaenger
    if vorg is None or vorg.art != Belegart.AUFTRAG:
        return NULL
    return max(bezahlt(vorg) - abgegeben(vorg), NULL)


def proforma_offen(pf, bez=None, abg=None, wartend=None, vorab=None) -> Decimal:
    """Offen = Brutto (solange sie wartet) − freie Anzahlung der Bestellung + verrechnet − Zahlungen; negativ = Guthaben."""
    bez = bezahlt(pf) if bez is None else bez
    abg = abgegeben(pf) if abg is None else abg
    wartend = wartet(pf) if wartend is None else wartend
    if not wartend:
        return abg - bez
    vorab = bestellung_guthaben(pf) if vorab is None else vorab
    return max(pf.summe_brutto - vorab, NULL) + abg - bez


def guthaben(quelle) -> Decimal:
    """Betrag, der aus der Proforma/Bestellung noch verrechnet oder zurückgezahlt werden kann (≥ 0)."""
    if quelle.art == Belegart.PROFORMA:  # nur eigene Zahlungen, die Anzahlung der Bestellung bleibt dort
        return max(-proforma_offen(quelle, vorab=NULL), NULL)
    if quelle.art == Belegart.AUFTRAG:
        return max(bezahlt(quelle) - abgegeben(quelle), NULL)
    if quelle.art == Belegart.RECHNUNG and quelle.abgeschlossen:  # Überzahlung
        from .services import offen
        return max(-offen(quelle), NULL)
    return NULL


def kunden_guthaben(mandant, kunde, ausser=None) -> list:
    """Freie Guthaben eines Kunden, die mit einer Rechnung verrechnet werden können (Vorschlag beim Zahlung buchen):
    überzahlte Rechnungen, Proformas mit Guthaben und Bestellungen, deren Anzahlung nach der Lieferung übrig ist.
    Gutschriften laufen weiter über die Gutschriftverrechnung. Rückgabe: [{"beleg", "guthaben"}], älteste zuerst."""
    from .services import _rechnungen_mit_summen, offene_rechnungen_zeilen
    zeilen = [{"beleg": z["beleg"], "guthaben": -z["offen"]}
              for z in offene_rechnungen_zeilen(_rechnungen_mit_summen(mandant, kunde)) if z["offen"] <= -CENT]
    hat_zahlung = Zahlung.objects.filter(beleg=models.OuterRef("pk"))
    for q in Beleg.objects.filter(mandant=mandant, kunde=kunde, art__in=QUELLARTEN).filter(models.Exists(hat_zahlung)) \
            .order_by("belegdatum", "id"):
        if q.art == Belegart.AUFTRAG and q.offene_mengen():
            continue  # Anzahlung für eine noch offene Lieferung: gehört zur eigenen Rechnung
        g = guthaben(q)
        if g >= CENT:
            zeilen.append({"beleg": q, "guthaben": g})
    zeilen = [z for z in zeilen if ausser is None or z["beleg"].pk != ausser.pk]
    return sorted(zeilen, key=lambda z: (z["beleg"].belegdatum, z["beleg"].pk))


def _status(pf, bez, abg, hat_zahlung, folgearten, vorab=NULL) -> dict:
    if Belegart.PROFORMA_STORNO in folgearten:
        code = "storniert"
    elif not pf.abgeschlossen:
        code = "entwurf"
    else:
        rest = proforma_offen(pf, bez, abg, Belegart.RECHNUNG not in folgearten, vorab)
        if Belegart.RECHNUNG in folgearten:
            code = "guthaben" if rest <= -CENT else "umgewandelt"
        elif abs(bez) < CENT and rest >= CENT:
            code = "erstattet" if hat_zahlung else "offen"
        elif rest >= CENT:
            code = "teilbezahlt"
        elif rest <= -CENT:
            code = "ueberzahlt"
        else:
            code = "bezahlt"
        return {"code": code, "text": STATUS_TEXT[code], "offen": rest, "bezahlt": bez}
    return {"code": code, "text": STATUS_TEXT[code], "offen": NULL, "bezahlt": bez}


def proforma_status(pf) -> dict:
    """Zahlstatus einer Proforma: {"code", "text", "offen", "bezahlt"}."""
    folgearten = set(Beleg.objects.filter(vorgaenger=pf).values_list("art", flat=True))
    return _status(pf, bezahlt(pf), abgegeben(pf), Zahlung.objects.filter(beleg=pf).exists(), folgearten, bestellung_guthaben(pf))


def status_je_proforma(belege) -> dict:
    """{pk: Status} für alle Proformas einer Liste mit vier Abfragen."""
    pfs = [b for b in belege if b.art == Belegart.PROFORMA]
    ids = [b.pk for b in pfs]
    if not ids:
        return {}

    def summe(qs, feld):
        return {z[feld]: z["s"] for z in qs.order_by().values(feld).annotate(s=models.Sum("betrag"))}
    bez = summe(Zahlung.objects.filter(beleg_id__in=ids), "beleg_id")
    hat = set(Zahlung.objects.filter(beleg_id__in=ids).values_list("beleg_id", flat=True))
    abg = summe(Anzahlungsverrechnung.objects.filter(quelle_id__in=ids), "quelle_id")
    folge = {}
    for vorg, art in Beleg.objects.filter(vorgaenger_id__in=ids).values_list("vorgaenger_id", "art"):
        folge.setdefault(vorg, set()).add(art)
    ab_ids = set(Beleg.objects.filter(pk__in=[b.vorgaenger_id for b in pfs if b.vorgaenger_id], art=Belegart.AUFTRAG)
                 .values_list("pk", flat=True))
    ab_bez = summe(Zahlung.objects.filter(beleg_id__in=ab_ids), "beleg_id")
    ab_abg = summe(Anzahlungsverrechnung.objects.filter(quelle_id__in=ab_ids), "quelle_id")

    def vorab(b):
        if b.vorgaenger_id not in ab_ids:
            return NULL
        return max(ab_bez.get(b.vorgaenger_id, NULL) - ab_abg.get(b.vorgaenger_id, NULL), NULL)
    return {b.pk: _status(b, bez.get(b.pk, NULL), abg.get(b.pk, NULL), b.pk in hat, folge.get(b.pk, set()), vorab(b))
            for b in pfs}


def offene_proformas(mandant, kunde=None) -> list:
    """Abgeschlossene Proformas, die auf Zahlung warten, und Proformas mit Guthaben: eigener Abschnitt der Offenen Posten.
    Sie sind keine Forderung und zählen nicht in die Summe der Offenen Posten, ins Mahnwesen oder ins Kreditlimit."""
    belege = Beleg.objects.filter(mandant=mandant, art=Belegart.PROFORMA, abgeschlossen=True).select_related("kunde") \
        .exclude(folgebelege__art=Belegart.PROFORMA_STORNO).order_by("belegdatum", "id")
    if kunde is not None:
        belege = belege.filter(kunde=kunde)
    belege = list(belege)
    status = status_je_proforma(belege)
    zeilen = []
    for b in belege:
        s = status[b.pk]
        if s["code"] not in ("offen", "teilbezahlt", "bezahlt", "ueberzahlt", "guthaben"):
            continue  # umgewandelt ohne Rest, erstattet, storniert
        zeilen.append({"beleg": b, "status": s, "offen": s["offen"]})
    return zeilen


def _kette(beleg):
    """Proforma-Rechnungen und Bestellungen in der Belegkette über dem Beleg (ohne Doppelte)."""
    gefunden, tiefe = [], 0
    while beleg is not None and tiefe < 10:
        if beleg.art in QUELLARTEN:
            gefunden.append(beleg)
        beleg, tiefe = beleg.vorgaenger, tiefe + 1
    return gefunden


def quellen(rechnung) -> list:
    """Belege mit Anzahlungen, die zu dieser Rechnung gehören: Proforma zuerst, dann Bestellungen (auch über Lieferscheine)."""
    alle = _kette(rechnung.vorgaenger)
    for ls in rechnung.lieferscheine.all():
        alle += _kette(ls)
    eindeutig = list({b.pk: b for b in alle}.values())
    return sorted(eindeutig, key=lambda b: (b.art != Belegart.PROFORMA, b.belegdatum, b.pk))


def anzahlungen_verrechnen(rechnung, benutzer=None) -> list:
    """Beim Abschluss einer Rechnung: Zahlungen auf Proforma/Bestellung derselben Kette bis zum Rechnungsbetrag verrechnen."""
    from .services import offen
    neu = []
    if rechnung.art != Belegart.RECHNUNG or not rechnung.abgeschlossen:
        return neu
    for q in quellen(rechnung):
        if q.kunde_id != rechnung.kunde_id or q.mandant_id != rechnung.mandant_id:
            continue
        Beleg.objects.select_for_update().get(pk=q.pk)
        rest = offen(rechnung)
        if rest < CENT:
            break
        betrag = min(guthaben(q), rest)
        if betrag >= CENT:
            neu.append(Anzahlungsverrechnung.objects.create(
                mandant=rechnung.mandant, quelle=q, rechnung=rechnung, betrag=betrag, benutzer=benutzer,
                datum=rechnung.belegdatum, bei_abschluss=True))
    return neu


@transaction.atomic
def anzahlung_verrechnen(quelle, rechnung, betrag, benutzer=None, offen_bisher=None) -> Anzahlungsverrechnung:
    """Guthaben einer Proforma/Bestellung von Hand mit einer anderen offenen Rechnung desselben Kunden verrechnen."""
    from .services import offen
    for pk in sorted({quelle.pk, rechnung.pk}):  # beide sperren, immer in derselben Reihenfolge (keine Verklemmung)
        Beleg.objects.select_for_update().get(pk=pk)
    if quelle.art not in GUTHABENARTEN or quelle.pk == rechnung.pk:
        raise ValueError("Verrechnet wird Guthaben aus Proforma-Rechnungen, Bestellungen oder überzahlten Rechnungen mit einer anderen Rechnung.")
    if rechnung.art != Belegart.RECHNUNG or not rechnung.abgeschlossen:
        raise ValueError("Verrechnet wird nur mit abgeschlossenen Rechnungen.")
    if (quelle.mandant_id, quelle.kunde_id) != (rechnung.mandant_id, rechnung.kunde_id):
        raise ValueError("Beide Belege müssen zum selben Kunden gehören.")
    if offen_bisher is not None and offen_bisher != guthaben(quelle):
        raise ValueError("Das Guthaben hat sich inzwischen geändert. Bitte prüfen und neu verrechnen.")
    if betrag <= 0:
        raise ValueError("Der Betrag muss größer als 0 sein.")
    if betrag > guthaben(quelle):
        raise ValueError(f"Von {quelle} sind nur {guthaben(quelle):.2f} € Guthaben frei.".replace(".", ",", 1))
    if betrag > offen(rechnung):
        raise ValueError(f"{rechnung} ist nur noch mit {offen(rechnung):.2f} € offen.".replace(".", ",", 1))
    return Anzahlungsverrechnung.objects.create(mandant=rechnung.mandant, quelle=quelle, rechnung=rechnung,
                                                betrag=betrag, benutzer=benutzer)


@transaction.atomic
def anzahlung_aufheben(verrechnung, grund, benutzer=None) -> Anzahlungsverrechnung:
    """Hebt eine Anzahlungsverrechnung per Gegenbuchung auf; der Grund ist Pflicht."""
    grund = (grund or "").strip()
    if not grund:
        raise ValueError("Bitte einen Grund angeben.")
    Anzahlungsverrechnung.objects.select_for_update().get(pk=verrechnung.pk)
    if verrechnung.storno_zu_id:
        raise ValueError("Eine Aufhebung kann nicht erneut aufgehoben werden.")
    if Anzahlungsverrechnung.objects.filter(storno_zu=verrechnung).exists():
        raise ValueError("Diese Verrechnung ist bereits aufgehoben.")
    return Anzahlungsverrechnung.objects.create(
        mandant=verrechnung.mandant, quelle=verrechnung.quelle, rechnung=verrechnung.rechnung,
        betrag=-verrechnung.betrag, benutzer=benutzer, storno_zu=verrechnung, storno_grund=grund[:300])


def _kopf(pf, art, **extra) -> Beleg:
    return Beleg.objects.create(
        mandant=pf.mandant, art=art, kunde=pf.kunde, ansprechpartner=pf.ansprechpartner,
        rechnungsanschrift=pf.rechnungsanschrift, versandanschrift=pf.versandanschrift, versandart=pf.versandart,
        versandkosten=pf.versandkosten, verpackung=pf.verpackung, versicherung=pf.versicherung,
        rabatt_prozent=pf.rabatt_prozent, referenz=pf.referenz, bezugnahme=pf.bezugnahme, vorgaenger=pf, **extra)


@transaction.atomic
def proforma_umwandeln(pf, benutzer=None) -> Beleg:
    """Vollständig bezahlte Proforma → abgeschlossene Rechnung mit denselben Positionen und Beträgen,
    Zahlungsbedingung „Betrag dankend erhalten“; die Zahlung wird beim Abschluss verrechnet."""
    pf = Beleg.objects.select_for_update().get(pk=pf.pk)  # Doppelklick/zweiter Tab: nur eine Rechnung
    if pf.art != Belegart.PROFORMA or not pf.abgeschlossen:
        raise ValueError("Umgewandelt wird nur eine abgeschlossene Proforma-Rechnung.")
    if pf.proforma_storno:
        raise ValueError(f"{pf} ist storniert.")
    if pf.proforma_rechnung:
        raise ValueError(f"{pf} ist schon in {pf.proforma_rechnung} umgewandelt.")
    rest = proforma_offen(pf)
    if rest >= CENT:
        raise ValueError(f"{pf} ist noch nicht vollständig bezahlt: offen {rest:.2f} €.".replace(".", ",", 1)
                         + " Die Rechnung entsteht erst nach vollständiger Zahlung.")
    rechnung = _kopf(pf, Belegart.RECHNUNG, zahlungsbedingung=zahlungsbedingung_bezahlt(), liefertermin=pf.liefertermin,
                     vortext=pf.vortext, schlusstext=pf.schlusstext, bearbeiter=benutzer or pf.bearbeiter,
                     bemerkung=f"Aus {pf} (Vorkasse)" + (f"\n{pf.bemerkung}" if pf.bemerkung else ""))
    kopiere_positionen(pf, rechnung)
    rechnung.berechne()
    if rechnung.summe_brutto != pf.summe_brutto:
        raise ValueError(f"Rechnungsbetrag {rechnung.summe_brutto} € weicht von der Proforma ({pf.summe_brutto} €) ab.")
    rechnung.abschliessen()  # bucht das Lager ab und verrechnet die Zahlung (anzahlungen_verrechnen)
    return rechnung


@transaction.atomic
def proforma_stornieren(pf, grund, benutzer=None) -> Beleg:
    """Kunde kauft nicht: nach der Rückzahlung wird die Proforma mit einem eigenen, abgeschlossenen Storno-Beleg storniert."""
    grund = (grund or "").strip()
    if not grund:
        raise ValueError("Bitte einen Stornogrund angeben.")
    pf = Beleg.objects.select_for_update().get(pk=pf.pk)
    if pf.art != Belegart.PROFORMA:
        raise ValueError("Storniert wird hier nur eine Proforma-Rechnung.")
    if pf.proforma_storno:
        raise ValueError(f"{pf} ist bereits storniert ({pf.proforma_storno}).")
    if pf.proforma_rechnung:
        raise ValueError(f"Aus {pf} ist schon {pf.proforma_rechnung} entstanden – Korrektur nur per Gutschrift zur Rechnung.")
    if not pf.positionen.exists():
        raise ValueError(f"{pf} hat keine Position – bitte löschen statt stornieren.")
    rest = bezahlt(pf) - abgegeben(pf)
    if abs(rest) >= CENT:
        raise ValueError(f"Auf {pf} sind noch {rest:.2f} € gezahlt.".replace(".", ",", 1)
                         + " Bitte zuerst den Betrag zurückzahlen (negative Zahlung), dann stornieren.")
    from apps.verkauf.uebernahme import beleg_zurueckbuchen
    beleg_zurueckbuchen(pf, ziel_anpassen=False)  # die Menge geht zurück in die Bestellung (Verlauf: zurückgebucht); die Proforma bleibt, wie sie war
    if not pf.abgeschlossen:  # Entwurf wird mit dem Storno festgeschrieben
        pf.berechne()
        Beleg.objects.filter(pk=pf.pk).update(abgeschlossen=True)
    storno = _kopf(pf, Belegart.PROFORMA_STORNO, zahlungsbedingung=pf.zahlungsbedingung, bearbeiter=benutzer or pf.bearbeiter,
                   belegdatum=timezone.localdate(),
                   vortext=f"Storno zur Proforma-Rechnung {pf.nummer} vom {pf.belegdatum:%d.%m.%Y}.",
                   bemerkung=f"Storno zu {pf}: {grund}"[:1000])
    kopiere_positionen(pf, storno)
    storno.berechne()
    storno.abschliessen()
    return storno
