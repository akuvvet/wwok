"""
Teilübernahme aus einer Bestellung (AB -> Lieferschein, Rechnung, Proforma).

Fachregel: „Kunde hat 10 bestellt, Teillieferung 7, dann muss nicht 10 in der Bestellung sein, sondern nur 3 Stück.“
Die übernommene Menge WANDERT aus der Bestellung in den Folgebeleg; die Bestellung behält nur den Rest (Liste, Beleg, PDF,
Reservierung). Der unveränderliche Verlauf BelegUebernahme hält fest, was wann wohin ging und was zurückgebucht wurde.

- menge_verschieben: Übernahme (Teilmenge = neue Position, volle Menge = Position wird umgehängt)
- zurueckbuchen / nachziehen: Mitziehen, wenn im Entwurf des Folgebelegs die Menge sinkt oder steigt
- beleg_zurueckbuchen: Löschen des Entwurfs bzw. Storno der Proforma
- altfaelle_*: Umstellung alter Teilübernahmen (Kopien) auf den Rest (Management-Befehl rueckstand_umstellen)

Position.verschoben unterscheidet neue Verschiebungen von alten Kopien: Nur Kopien mit verschoben=False werden in der Formel
für die offene Menge noch von der Bestellmenge abgezogen (models.mit_offener_menge).
"""
from decimal import Decimal

from django.db import transaction
from django.db.models import Max, Sum

from .models import Beleg, BelegUebernahme, Belegart, Position, stornierte_proformas

NULL = Decimal("0")
DREI = Decimal("0.001")
FOLGEARTEN = (Belegart.LIEFERSCHEIN, Belegart.RECHNUNG, Belegart.PROFORMA)


def _benutzer():
    from apps.kern.middleware import aktueller_benutzer
    b = aktueller_benutzer()
    return b if b is not None and b.is_authenticated else None


def _menge_text(menge) -> str:
    return f"{Decimal(menge).normalize():f}".replace(".", ",")


def _eintrag(ab, ziel, richtung, menge, quellposition, zielposition, vorlage) -> BelegUebernahme:
    zusatz = {}
    if richtung == BelegUebernahme.UMGESTELLT and zielposition is not None and zielposition.angelegt_am:
        zusatz["am"] = zielposition.angelegt_am  # Zeitpunkt der alten Kopie, nicht der Umstellung (Einkauf ordnet danach ein)
    return BelegUebernahme.objects.create(
        **zusatz, mandant=ab.mandant, quelle=ab, quelle_art=ab.art, quelle_nummer=ab.nummer or "", ziel=ziel, ziel_art=ziel.art,
        quellposition=quellposition, zielposition=zielposition, artikel=vorlage.artikel,
        artikelnummer=(vorlage.artikelnummer or "")[:60], bezeichnung=vorlage.bezeichnung[:255], menge=menge,
        richtung=richtung, benutzer=_benutzer())


def _anteil(menge, teil, gesamt, name) -> Decimal:
    """Anteil einer Unterposition, wenn von der Gruppenmenge `gesamt` die Menge `teil` geteilt wird; nicht teilbar = Fehler."""
    wert = menge * teil / gesamt
    if wert != wert.quantize(DREI):
        raise ValueError(f"Die Menge {_menge_text(teil)} lässt sich nicht gleichmäßig auf „{name}“ (Unterposition) verteilen. "
                         "Bitte eine Menge wählen, bei der jede Unterposition aufgeht, oder die Unterpositionen einzeln ändern.")
    return wert


def _kopie(quelle, beleg, menge, pos, gruppe, verschoben=True, mit_quelle=True) -> Position:
    return Position.objects.create(
        beleg=beleg, gruppe=gruppe, pos=pos, typ=quelle.typ, artikel=quelle.artikel, bezeichnung=quelle.bezeichnung,
        preisliste_artikel=quelle.preisliste_artikel, menge=menge, einzelpreis=quelle.einzelpreis,
        rabatt_prozent=quelle.rabatt_prozent, steuer_prozent=quelle.steuer_prozent, zusatztext=quelle.zusatztext,
        lieferschein=quelle.lieferschein, quellposition=quelle if mit_quelle else None, preis_fest=quelle.preis_fest,
        verschoben=verschoben)


def _seriennummern_mitnehmen(quelle, ziel, menge):
    """Seriennummern der Bestellposition wandern mit: die ersten `menge` Stück gehen an die neue Position."""
    if not quelle.artikel_id:
        return
    for sn in list(quelle.seriennummern.order_by("nummer"))[:int(menge)]:
        sn.position = ziel
        sn.save(update_fields=["position"])


def _seriennummern_zurueck(von, nach, anzahl):
    """Seriennummern gehen mit der zurückgebuchten Menge zurück an die Bestellposition (die letzten `anzahl` Stück)."""
    if anzahl <= 0 or not von.artikel_id or nach is None:
        return
    for sn in list(von.seriennummern.order_by("-nummer"))[:int(anzahl)]:
        sn.position = nach
        sn.save(update_fields=["position"])


def _ueberzaehlig(p, rueck, ziel_anpassen) -> int:
    """Wie viele Seriennummern der Position nach dem Rückbuchen von `rueck` Stück zurück müssen."""
    anzahl = p.seriennummern.count() if p.artikel_id else 0
    if not ziel_anpassen:
        return min(anzahl, int(rueck))
    return max(anzahl - int(p.menge - rueck), 0)


def alte_kopien(ab) -> dict:
    """{Positions-ID: Menge alter Kopien (verschoben=False)}: Altdaten, die von der Bestellmenge noch abzuziehen sind."""
    zeilen = (Position.objects.filter(beleg__vorgaenger=ab, quellposition__beleg=ab, verschoben=False,
                                 beleg__art__in=FOLGEARTEN)
              .exclude(beleg__in=stornierte_proformas()).order_by().values("quellposition").annotate(s=Sum("menge")))
    return {z["quellposition"]: z["s"] for z in zeilen}


def erledigt_wenn_leer(ab) -> bool:
    """Eine Bestellung ohne Position ist erledigt (abgeschlossen), ohne abschliessen(): das fehlt bei leeren Belegen."""
    if ab.art == Belegart.AUFTRAG and not ab.abgeschlossen and not ab.positionen.exists():
        ab.abgeschlossen = ab.automatisch_erledigt = True
        ab.save(update_fields=["abgeschlossen", "automatisch_erledigt"])
        return True
    return False


def nebenkosten_mitgeben(ab, ziel):
    """Versandkosten, Verpackung und Versicherung laufen mit dem ersten Folgebeleg (stehen dort schon); die Bestellung
    behält sie nicht noch einmal, sonst würden sie bei jeder Teillieferung erneut berechnet."""
    if ab.versandkosten or ab.verpackung or ab.versicherung:
        ab.versandkosten = ab.verpackung = ab.versicherung = NULL
        ab.save(update_fields=["versandkosten", "verpackung", "versicherung"])
        ab.berechne()


# ---- Übernahme ----------------------------------------------------------------------------------------------------
@transaction.atomic
def menge_verschieben(ab, ziel, mengen) -> list:
    """Verschiebt die Mengen ({Positions-ID der Hauptposition: Menge}) aus der Bestellung in den neuen Folgebeleg.
    Teilmenge: neue Position im Folgebeleg, die Bestellposition sinkt (Unterpositionen anteilig). Volle Menge: die Position
    samt Unterpositionen wird umgehängt (save(), damit das Änderungsprotokoll greift). Seriennummern wandern mit.
    Die Mengen werden unter Sperre neu gelesen: eine zweite, gleichzeitige Übernahme zieht nichts doppelt ab.
    Rückgabe: die Verlaufseinträge. Ist die Bestellung danach leer, ist sie erledigt."""
    if ab.art != Belegart.AUFTRAG or ziel.art not in FOLGEARTEN:
        raise ValueError("Verschoben wird nur aus einer Bestellung in Lieferschein, Rechnung oder Proforma-Rechnung.")
    Beleg.objects.select_for_update().get(pk=ab.pk)
    alle = list(Position.objects.select_for_update().filter(beleg=ab).order_by("pos", "id"))
    unter = {}
    for p in alle:
        if p.gruppe_id:
            unter.setdefault(p.gruppe_id, []).append(p)
    alt = alte_kopien(ab)
    plan = []
    for p in alle:
        if p.gruppe_id:
            continue
        wunsch = mengen.get(p.pk, NULL)
        if wunsch <= 0:
            continue
        offen = max(p.menge - alt.get(p.pk, NULL), NULL)
        if wunsch > offen:
            raise ValueError(f"Die Bestellung hat sich inzwischen geändert: Von „{p.bezeichnung}“ sind nur noch "
                             f"{_menge_text(offen)} offen (verlangt {_menge_text(wunsch)}). Bitte den Beleg neu öffnen – es wurde nichts übernommen.")
        voll = wunsch == p.menge
        teile = {} if voll else {t.pk: _anteil(t.menge, wunsch, p.menge, t.bezeichnung) for t in unter.get(p.pk, [])}
        plan.append((p, wunsch, voll, teile))
    if not plan:
        raise ValueError("Es wird nichts übernommen: In der Bestellung ist nichts mehr offen.")
    nummer = Position.objects.filter(beleg=ziel).aggregate(m=Max("pos"))["m"] or 0
    eintraege = []
    for p, menge, voll, teile in plan:
        if voll:
            for x in [p, *unter.get(p.pk, [])]:
                nummer += 1
                x.beleg, x.verschoben, x.pos = ziel, True, nummer
                x.save()
            eintraege.append(_eintrag(ab, ziel, BelegUebernahme.UEBERNOMMEN, menge, p, p, p))
            continue
        nummer += 1
        kopf = _kopie(p, ziel, menge, nummer, None)
        _seriennummern_mitnehmen(p, kopf, menge)
        for t in unter.get(p.pk, []):
            nummer += 1
            anteil = teile[t.pk]
            neu_t = _kopie(t, ziel, anteil, nummer, kopf)
            _seriennummern_mitnehmen(t, neu_t, anteil)
            t.menge -= anteil
            t.save()
        p.menge -= menge
        p.save()
        eintraege.append(_eintrag(ab, ziel, BelegUebernahme.UEBERNOMMEN, menge, p, kopf, p))
    ab.berechne()  # Summen und lückenlose Positionsnummern des Rests
    erledigt_wenn_leer(ab)
    return eintraege


# ---- Rückbuchen und Mitziehen ----------------------------------------------------------------------------------------
def netto_uebernommen(zielposition) -> Decimal:
    """Wie viel der Menge dieser Position stammt (nach Verlauf) aus der Bestellung; der Rest ist Zusatzmenge."""
    summe = NULL
    for e in BelegUebernahme.objects.filter(zielposition=zielposition):
        summe += e.menge * e.vorzeichen
    return max(summe, NULL)


def _bestellung(p):
    """Die Bestellung, aus der die Position eines Folgebelegs stammt (None = nicht verschoben oder Bestellung gelöscht)."""
    if not p.pk or not p.verschoben or p.gruppe_id:
        return None
    ziel = p.beleg
    ab = ziel.vorgaenger if ziel.vorgaenger_id else None
    if ab is None or ab.art != Belegart.AUFTRAG or ab.mandant_id != ziel.mandant_id or ziel.art not in FOLGEARTEN:
        return None
    return ab


def _ab_position(p, ab):
    """Bestellposition, aus der die Position stammt und in die zurückgebucht wird (None = gibt es nicht mehr)."""
    if p.quellposition_id and p.quellposition.beleg_id == ab.pk:
        return p.quellposition
    return None


@transaction.atomic
def zurueckbuchen(p, menge, ziel_anpassen=True) -> Decimal:
    """Menge aus dem Folgebeleg zurück in die Bestellung (Menge im Entwurf gesenkt, Position gelöscht, Entwurf gelöscht,
    Proforma storniert). Zuerst sinkt die Zusatzmenge, die nie aus der Bestellung kam. Gibt es die Bestellposition nicht
    mehr (volle Menge war umgehängt), wird sie aus den Daten der Position neu angelegt. Eine erledigte Bestellung wird
    wieder geöffnet. ziel_anpassen=False: die Positionen des Folgebelegs bleiben unverändert (Storno).
    Rückgabe: die zurückgebuchte Menge (0 = nichts zu tun). p.quellposition wird im Speicher nachgeführt."""
    ab = _bestellung(p)
    if ab is None or menge <= 0:
        return NULL
    Beleg.objects.select_for_update().get(pk=ab.pk)
    netto = netto_uebernommen(p)
    zusatz = max(p.menge - netto, NULL)
    rueck = min(max(menge - zusatz, NULL), netto, p.menge)
    if rueck <= 0:
        return NULL
    kinder = list(p.unterpositionen.select_related("quellposition"))
    anteile = {t.pk: _anteil(t.menge, rueck, p.menge, t.bezeichnung) for t in kinder}  # erst prüfen, dann schreiben
    # Nur eine vom Programm erledigte Bestellung wird wieder geöffnet; eine beim Kunden versandte behält ihren Status
    war_erledigt = ab.abgeschlossen and ab.automatisch_erledigt and not ab.offene_mengen()
    kopf = _ab_position(p, ab)
    if kopf is not None:
        kopf = Position.objects.select_for_update().get(pk=kopf.pk)
        kopf.menge += rueck
        kopf.save()
    else:
        nummer = (Position.objects.filter(beleg=ab).aggregate(m=Max("pos"))["m"] or 0) + 1
        kopf = _kopie(p, ab, rueck, nummer, None, verschoben=False, mit_quelle=False)
        Position.objects.filter(pk=p.pk).update(quellposition=kopf)
        p.quellposition = kopf
    _seriennummern_zurueck(p, kopf, _ueberzaehlig(p, rueck, ziel_anpassen))
    for t in kinder:
        anteil = anteile[t.pk]
        ab_t = t.quellposition if t.quellposition_id and t.quellposition.beleg_id == ab.pk and t.quellposition.gruppe_id == kopf.pk else None
        if ab_t is not None:
            ab_t.menge += anteil
            ab_t.save()
        else:
            ab_t = _kopie(t, ab, anteil, kopf.pos, kopf, verschoben=False, mit_quelle=False)
            Position.objects.filter(pk=t.pk).update(quellposition=ab_t)
        _seriennummern_zurueck(t, ab_t, _ueberzaehlig(t, anteil, ziel_anpassen))
        if ziel_anpassen:
            t.menge -= anteil
            t.save()
    _eintrag(ab, p.beleg, BelegUebernahme.ZURUECK, rueck, kopf, p, p)
    ab.berechne()
    if war_erledigt:
        ab.abgeschlossen = ab.automatisch_erledigt = False
        ab.save(update_fields=["abgeschlossen", "automatisch_erledigt"])
    return rueck


@transaction.atomic
def nachziehen(p, menge) -> Decimal:
    """Menge im Entwurf des Folgebelegs erhöht: weitere Stück kommen aus dem Rest der Bestellung, höchstens bis Rest 0
    (alles darüber ist Zusatzmenge, die Bestellung bleibt unberührt). Rückgabe: aus der Bestellung genommene Menge."""
    ab = _bestellung(p)
    if ab is None or menge <= 0:
        return NULL
    Beleg.objects.select_for_update().get(pk=ab.pk)
    kopf = _ab_position(p, ab)
    if kopf is None:
        return NULL
    kopf = Position.objects.select_for_update().get(pk=kopf.pk)
    nimm = min(menge, max(kopf.menge - alte_kopien(ab).get(kopf.pk, NULL), NULL))
    if nimm <= 0:
        return NULL
    ab_kinder = list(kopf.unterpositionen.all())
    anteile = {t.pk: _anteil(t.menge, nimm, kopf.menge, t.bezeichnung) for t in ab_kinder}
    ziel_kinder = {t.quellposition_id: t for t in p.unterpositionen.all() if t.quellposition_id}
    alles = nimm == kopf.menge
    for s in ab_kinder:
        anteil = anteile[s.pk]
        t = ziel_kinder.get(s.pk)
        if t is not None:
            t.menge += anteil
            t.save()
        else:
            t = _kopie(s, p.beleg, anteil, p.pos, p, mit_quelle=not alles)
        _seriennummern_mitnehmen(s, t, anteil)
        if not alles:
            s.menge -= anteil
            s.save()
    ziel = p.beleg
    _seriennummern_mitnehmen(kopf, p, nimm)  # vor dem Löschen der Bestellposition: sonst bleiben die Nummern ohne Position
    _eintrag(ab, ziel, BelegUebernahme.UEBERNOMMEN, nimm, kopf, p, p)
    if alles:  # die Bestellposition ist aufgebraucht: sie verschwindet mit ihren Unterpositionen, die Position im Folgebeleg ist ganz
        kopf.delete()
        Position.objects.filter(beleg=p.beleg, gruppe=p).update(quellposition=None)
        p.quellposition = None
    else:
        kopf.menge -= nimm
        kopf.save()
    ab.berechne()
    erledigt_wenn_leer(ab)
    return nimm


@transaction.atomic
def beleg_zurueckbuchen(ziel, ziel_anpassen=True) -> list:
    """Alles, was aus der Bestellung kam, geht zurück: Löschen eines Entwurfs, Storno einer Proforma. Versandkosten,
    die mit dem Folgebeleg liefen, gehen mit zurück, wenn die Bestellung keine eigenen mehr hat."""
    if ziel.art not in FOLGEARTEN or not ziel.vorgaenger_id or ziel.vorgaenger.art != Belegart.AUFTRAG:
        return []
    ergebnis = []
    for p in list(ziel.positionen.filter(gruppe__isnull=True, verschoben=True)):
        rueck = zurueckbuchen(p, p.menge, ziel_anpassen=ziel_anpassen)
        if rueck:
            ergebnis.append((p, rueck))
    ab = ziel.vorgaenger
    if ergebnis and (ziel.versandkosten or ziel.verpackung or ziel.versicherung) \
            and not (ab.versandkosten or ab.verpackung or ab.versicherung):
        ab.versandkosten, ab.verpackung, ab.versicherung = ziel.versandkosten, ziel.verpackung, ziel.versicherung
        ab.save(update_fields=["versandkosten", "verpackung", "versicherung"])
        ab.berechne()
    return ergebnis


def menge_anpassen(p, neue_menge) -> dict:
    """Mitziehen beim Ändern der Menge einer übernommenen Position im Entwurf (LS, RE, PF): sinkt sie, geht die Differenz
    zurück in die Bestellung; steigt sie, kommen weitere Stück aus dem Rest der Bestellung. Vor dem Setzen der neuen Menge
    aufrufen. Rückgabe: {"zurueck": Menge, "nachgezogen": Menge, "zusatz": Menge darüber hinaus}."""
    erg = {"zurueck": NULL, "nachgezogen": NULL, "zusatz": NULL}
    if neue_menge < p.menge:
        erg["zurueck"] = zurueckbuchen(p, p.menge - neue_menge)
    elif neue_menge > p.menge:
        erg["nachgezogen"] = nachziehen(p, neue_menge - p.menge)
        if _bestellung(p) is not None:
            erg["zusatz"] = neue_menge - p.menge - erg["nachgezogen"]
    return erg


# ---- Anzeige --------------------------------------------------------------------------------------------------------
def verlauf_aus(ab) -> list:
    """Verlauf der Übernahmen einer Bestellung (älteste zuerst)."""
    return list(BelegUebernahme.objects.filter(quelle=ab).select_related("zielposition__beleg", "ziel"))


def verlauf_in(beleg) -> list:
    """Einträge, deren Position heute in diesem Folgebeleg liegt (für „aus Bestellung …“)."""
    from django.db.models import Q
    return list(BelegUebernahme.objects.filter(Q(zielposition__beleg=beleg) | Q(zielposition__isnull=True, ziel=beleg))
                .select_related("quelle", "zielposition__beleg", "ziel"))


def uebernommen_je_ziel(eintraege) -> list:
    """Fasst Einträge zusammen: [(Folgebeleg oder None, Art, Menge netto)], nur mit Menge > 0."""
    summen, reihenfolge = {}, []
    for e in eintraege:
        beleg = e.ziel_beleg
        schluessel = beleg.pk if beleg is not None else ("weg", e.ziel_art)
        if schluessel not in summen:
            summen[schluessel] = [beleg, e.ziel_art, NULL]
            reihenfolge.append(schluessel)
        summen[schluessel][2] += e.menge * e.vorzeichen
    return [tuple(summen[k]) for k in reihenfolge if summen[k][2] > 0]


def pdf_zeilen(ab) -> list:
    """Zeilen für den Block „bereits geliefert/berechnet“ unter den Positionen der Bestellung, z. B.
    „7 × M1 Maus mit Lieferschein LS2026-00001“ (netto nach Rückbuchungen, nur Mengen > 0)."""
    summen, reihenfolge = {}, []
    for e in BelegUebernahme.objects.filter(quelle=ab).select_related("zielposition__beleg", "ziel"):
        beleg = e.ziel_beleg
        schluessel = (beleg.pk if beleg is not None else e.ziel_art, e.artikel_id, e.artikelnummer, e.bezeichnung)
        if schluessel not in summen:
            summen[schluessel] = [e, NULL]
            reihenfolge.append(schluessel)
        summen[schluessel][1] += e.menge * e.vorzeichen
    zeilen = []
    for k in reihenfolge:
        e, menge = summen[k]
        if menge <= 0:
            continue
        beleg = e.ziel_beleg
        wohin = (f"{beleg.get_art_display()} {beleg.nummer}" if beleg is not None and beleg.nummer
                 else f"{beleg.get_art_display() if beleg is not None else Belegart(e.ziel_art).label} (in Vorbereitung)")
        zeilen.append(f"{_menge_text(menge)} × {(e.artikelnummer + ' ') if e.artikelnummer else ''}{e.bezeichnung} mit {wohin}")
    return zeilen


def rueckstand_text(ab, positionen) -> dict:
    """{Positions-ID: Text} für die Bestellansicht, z. B. „7 übernommen in Rechnung RE… (Entwurf), 3 offen“. Der Text gilt je
    Bestellposition (nach Verlauf); Positionen ohne Übernahme haben keinen Eintrag."""
    je_position = {}
    for e in BelegUebernahme.objects.filter(quelle=ab, quellposition__isnull=False).select_related("zielposition__beleg", "ziel"):
        je_position.setdefault(e.quellposition_id, []).append(e)
    erg = {}
    for p in positionen:
        zeilen = uebernommen_je_ziel(je_position.get(p.pk, []))
        if not zeilen:
            continue
        teile = [f"{_menge_text(menge)} übernommen in {(beleg.get_art_display() + ' ' + (beleg.nummer or '(Entwurf)')) if beleg else Belegart(art).label + ' (gelöscht)'}"
                 for beleg, art, menge in zeilen]
        erg[p.pk] = ", ".join(teile) + (f", {_menge_text(p.offen_menge)} offen" if p.offen_menge > 0 else ", nichts mehr offen")
    return erg


# ---- Altdaten --------------------------------------------------------------------------------------------------------
def altfaelle(mandant=None) -> list:
    """Bestellungen mit alten Teilübernahmen (Kopien mit verschoben=False, ohne stornierte Proforma):
    [{"beleg", "status": "umstellen" | "proforma"}]. Auch beim Kunden versandte (abgeschlossene) Bestellungen werden
    umgestellt; ihr Status bleibt."""
    kopien = (Position.objects.filter(beleg__vorgaenger__art=Belegart.AUFTRAG, beleg__art__in=FOLGEARTEN, verschoben=False,
                                      quellposition__isnull=False)
              .exclude(beleg__in=stornierte_proformas()).values_list("beleg__vorgaenger_id", "quellposition__beleg_id"))
    ab_ids = {vorgaenger for vorgaenger, quelle in kopien if vorgaenger == quelle}
    belege = Beleg.objects.filter(pk__in=ab_ids, art=Belegart.AUFTRAG)
    if mandant is not None:
        belege = belege.filter(mandant=mandant)
    ergebnis = []
    for ab in belege.order_by("mandant_id", "pk"):
        aktive_pf = ab.folgebelege.filter(art=Belegart.PROFORMA).exclude(folgebelege__art=Belegart.PROFORMA_STORNO).exists()
        status = "proforma" if aktive_pf else "umstellen"
        ergebnis.append({"beleg": ab, "status": status})
    return ergebnis


def altfall_plan(ab) -> list:
    """Was die Umstellung ändert: [{"position", "alt", "uebernommen", "neu", "kopien": [Positionen im Folgebeleg]}] je Hauptposition."""
    zeilen = []
    for p in Position.objects.filter(beleg=ab, gruppe__isnull=True).order_by("pos", "id"):
        kopien = list(Position.objects.filter(quellposition=p, beleg__vorgaenger=ab, beleg__art__in=FOLGEARTEN, verschoben=False)
                      .exclude(beleg__in=stornierte_proformas()).select_related("beleg").order_by("pk"))
        if not kopien:
            continue
        uebernommen = min(sum((k.menge for k in kopien), NULL), p.menge)
        if p.menge:  # Probelauf und Ausführung melden dasselbe: nicht teilbare Unterpositionen sind ein Fehler
            for t in Position.objects.filter(beleg=ab, gruppe=p):
                _anteil(t.menge, p.menge - uebernommen, p.menge, t.bezeichnung)
        zeilen.append({"position": p, "alt": p.menge, "uebernommen": uebernommen, "neu": p.menge - uebernommen, "kopien": kopien})
    return zeilen


@transaction.atomic
def altfall_umstellen(ab) -> list:
    """Stellt eine Bestellung (auch eine beim Kunden versandte) mit alten Teilübernahmen auf den Rest um: Hauptposition und Unterpositionen sinken
    (anteilig), die alten Kopien werden als verschoben markiert, je Kopie entsteht ein Verlaufseintrag „umgestellt“.
    Positionen mit Rest 0 bleiben mit Menge 0 stehen; ist alles 0, ist die Bestellung erledigt. Nichts wird gelöscht.
    Die Kopien liegen oft in abgeschlossenen (gesperrten) Belegen: nur das Kennzeichen wird dort per update() gesetzt."""
    Beleg.objects.select_for_update().get(pk=ab.pk)
    plan = altfall_plan(ab)
    unter = {}
    for t in Position.objects.filter(beleg=ab, gruppe__isnull=False):
        unter.setdefault(t.gruppe_id, []).append(t)
    neue_t = {}
    for z in plan:  # erst prüfen, dann schreiben
        p = z["position"]
        for t in unter.get(p.pk, []):
            neue_t[t.pk] = _anteil(t.menge, z["neu"], p.menge, t.bezeichnung) if p.menge else t.menge
    for z in plan:
        p = z["position"]
        rest = z["uebernommen"]
        for k in z["kopien"]:
            teil = min(k.menge, rest)
            rest -= teil
            if teil > 0:
                _eintrag(ab, k.beleg, BelegUebernahme.UMGESTELLT, teil, p, k, p)
        ids = [k.pk for k in z["kopien"]]
        ids += list(Position.objects.filter(quellposition__in=unter.get(p.pk, []), beleg__vorgaenger=ab,
                                           beleg__art__in=FOLGEARTEN, verschoben=False)
                    .exclude(beleg__in=stornierte_proformas()).values_list("pk", flat=True))
        Position.objects.filter(pk__in=ids).update(verschoben=True)
        for t in unter.get(p.pk, []):
            t.menge = neue_t[t.pk]
            t.save()
        p.menge = z["neu"]
        p.save()
    ab.berechne()
    if not any(p.menge > 0 for p in ab.positionen.filter(gruppe__isnull=True)) and not ab.abgeschlossen:
        ab.abgeschlossen = ab.automatisch_erledigt = True
        ab.save(update_fields=["abgeschlossen", "automatisch_erledigt"])
    return plan
