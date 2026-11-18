from collections import defaultdict
from decimal import Decimal

from django.db import transaction
from django.db.models import Q

from apps.verkauf.models import Belegart, Ruecknahme

from .models import (BestandReichtNicht, Bewegungsart, Lagerbewegung, Reparaturart, Reparaturbewegung, Seriennummer, bestand,
                     reservierende_positionen)

LAGERFUEHREND = "A"  # nur Typ A (Lagerartikel) wird gebucht


def bucht_bestand(beleg) -> bool:
    """Lieferschein bucht immer ab; eine Rechnung nur, wenn kein Lieferschein in der Belegkette liegt."""
    if beleg.art == Belegart.LIEFERSCHEIN:
        return True
    if beleg.art == Belegart.NACHLIEFERSCHEIN:  # nur bei Ersatzlieferung (z. B. defektes Gerät), sonst ist die Ware schon abgebucht
        return beleg.nl_lager_abbuchen is True
    if beleg.art != Belegart.RECHNUNG:
        return False
    if beleg.pk and beleg.lieferscheine.exists():  # Sammelrechnung aus Lieferscheinen: schon abgebucht
        return False
    vorher = beleg.vorgaenger
    while vorher is not None:
        if vorher.art == Belegart.LIEFERSCHEIN:
            return False
        vorher = vorher.vorgaenger
    return True


def offene_lieferscheine(beleg):
    """Lieferscheine in der Belegkette einer Rechnung, die noch nicht abgeschlossen (= nicht abgebucht) sind."""
    liste = list(beleg.lieferscheine.filter(abgeschlossen=False)) if beleg.pk else []
    vorher = beleg.vorgaenger
    while vorher is not None:
        if vorher.art == Belegart.LIEFERSCHEIN and not vorher.abgeschlossen:
            liste.append(vorher)
        vorher = vorher.vorgaenger
    return liste


def _lagermengen(beleg) -> tuple:
    mengen, artikel = defaultdict(Decimal), {}
    for p in beleg.positionen.select_related("artikel"):
        if p.artikel_id and p.artikel.typ == LAGERFUEHREND:
            mengen[p.artikel_id] += p.menge
            artikel[p.artikel_id] = p.artikel
    return mengen, artikel


def lieferscheine_der_rechnung(beleg) -> list:
    """Alle Lieferscheine, über die die Ware einer Rechnung schon abgebucht wurde (Sammelrechnung und Belegkette)."""
    liste = list(beleg.lieferscheine.all()) if beleg.pk else []
    vorher = beleg.vorgaenger
    while vorher is not None:
        if vorher.art == Belegart.LIEFERSCHEIN and vorher not in liste:
            liste.append(vorher)
        vorher = vorher.vorgaenger
    return liste


def buchmengen(beleg) -> tuple:
    """({Artikel-ID: Menge}, {Artikel-ID: Artikel}), die dieser Beleg beim Abschluss vom Lager abbucht.
    Lieferschein (und Rechnung ohne Lieferschein): alle Lagerartikel. Rechnung aus Lieferschein: nur, was über die gelieferte
    Menge hinausgeht (in der Rechnung ergänzter Artikel, erhöhte Menge) – der Rest ist über den Lieferschein schon abgebucht.
    Andere abgeschlossene Rechnungen zu denselben Lieferscheinen zählen mit, damit nichts doppelt gebucht wird."""
    if bucht_bestand(beleg):
        return _lagermengen(beleg)
    if beleg.art != Belegart.RECHNUNG:
        return {}, {}
    scheine = lieferscheine_der_rechnung(beleg)
    if not scheine:
        return {}, {}
    mengen, artikel = _lagermengen(beleg)
    if not mengen:
        return {}, {}
    geliefert = defaultdict(Decimal)
    for ls in scheine:
        for artikel_id, menge in _lagermengen(ls)[0].items():
            geliefert[artikel_id] += menge
    ids = [ls.pk for ls in scheine]
    andere = type(beleg).objects.filter(art=Belegart.RECHNUNG, abgeschlossen=True, mandant=beleg.mandant).filter(
        Q(vorgaenger_id__in=ids) | Q(lieferscheine__in=ids)).exclude(pk=beleg.pk).distinct()
    for r in andere:
        for artikel_id, menge in _lagermengen(r)[0].items():
            geliefert[artikel_id] -= menge
    mehr = {a: m - max(geliefert[a], Decimal("0")) for a, m in mengen.items()}
    mehr = {a: m for a, m in mehr.items() if m > 0}
    return mehr, {a: artikel[a] for a in mehr}


def vorgang_ids(beleg) -> set:
    """IDs aller Belege desselben Vorgangs: Vorgänger, Folgebelege und Belege, die über eine Teilübernahme verbunden sind."""
    from apps.verkauf.models import Beleg, BelegUebernahme
    if not beleg.pk:
        return {beleg.vorgaenger_id} if beleg.vorgaenger_id else set()
    ids, neu = {beleg.pk}, {beleg.pk}
    while neu:
        nachbarn = set(Beleg.objects.filter(pk__in=neu, vorgaenger__isnull=False).values_list("vorgaenger_id", flat=True))
        nachbarn |= set(Beleg.objects.filter(vorgaenger_id__in=neu).values_list("pk", flat=True))
        for quelle, ziel in BelegUebernahme.objects.filter(Q(quelle_id__in=neu) | Q(ziel_id__in=neu)).values_list("quelle_id", "ziel_id"):
            nachbarn |= {quelle, ziel}
        neu = nachbarn - ids - {None}
        ids |= neu
    return ids


def fremd_reserviert(beleg, artikel_ids) -> dict:
    """{Artikel-ID: Menge}, die für ANDERE Kundenbestellungen reserviert ist. Die Reservierung des eigenen Vorgangs (Rest der
    Bestellung, Proforma) zählt nicht mit: sonst würde eine Bestellung ihre eigene Lieferung blockieren."""
    from django.db.models import Sum

    from apps.verkauf.models import mit_offener_menge
    offen = mit_offener_menge(reservierende_positionen(beleg.mandant).filter(artikel_id__in=list(artikel_ids))
                              .exclude(beleg_id__in=vorgang_ids(beleg)))
    return {z["artikel_id"]: z["s"] for z in offen.filter(offen__gt=0).order_by().values("artikel_id").annotate(s=Sum("offen"))}


def fehlbestand(beleg) -> list:
    """Texte zu Lagerartikeln, deren verfügbarer Bestand für Lieferschein/Rechnung nicht reicht (leer = alles lieferbar).
    Verfügbar = Bestand minus Menge, die für andere Kundenbestellungen reserviert ist (Entscheidung des Nutzers, 15.11.2026:
    reservierte Ware ist wie „verfügbar 0“)."""
    mengen, artikel = buchmengen(beleg)
    reserviert = fremd_reserviert(beleg, mengen.keys()) if mengen else {}
    knapp = []
    for artikel_id, menge in mengen.items():
        vorhanden = bestand(beleg.mandant, artikel[artikel_id])
        fremd = reserviert.get(artikel_id) or Decimal("0")
        if vorhanden - fremd < menge:
            zusatz = f", davon {fremd:f} für andere Bestellungen reserviert" if fremd > 0 else ""
            knapp.append(f"{artikel[artikel_id].nummer} {artikel[artikel_id].bezeichnung[:40]} "
                         f"(benötigt {menge:f}, vorhanden {vorhanden:f}{zusatz})")
    return knapp


def druckhindernis(beleg) -> str:
    """Grund, warum ein Lieferschein/eine Rechnung (noch) nicht gedruckt oder versendet werden darf; leer = erlaubt."""
    if beleg.art in (Belegart.AUFTRAG, Belegart.LIEFERSCHEIN, Belegart.RECHNUNG) and not beleg.abgeschlossen:
        hinweis = beleg.p_hinweis
        if hinweis:
            return hinweis
    if beleg.art == Belegart.NACHLIEFERSCHEIN and not beleg.abgeschlossen and beleg.nl_lager_abbuchen is None:
        return "Nachlieferschein erst abschließen (Speichern-Symbol): dabei wird gefragt, ob die Ware vom Lager abgebucht wird."
    if beleg.art not in (Belegart.LIEFERSCHEIN, Belegart.RECHNUNG, Belegart.NACHLIEFERSCHEIN) or beleg.abgeschlossen:
        return ""
    if beleg.art == Belegart.RECHNUNG:
        offen = offene_lieferscheine(beleg)
        if offen:
            return "Erst den Lieferschein " + ", ".join(l.nummer or "(nicht abgeschlossen)" for l in offen) + " abschließen (bucht das Lager ab)."
    knapp = fehlbestand(beleg)
    if knapp:
        return "Lagerbestand reicht nicht für " + beleg.get_art_display() + ": " + "; ".join(knapp) + ". Druck nicht möglich."
    return ""


@transaction.atomic
def buche_beleg(beleg, benutzer=None) -> int:
    """Bucht die Lagerartikel des Belegs ab (einmal je Beleg). Rückgabe: Zahl der Bewegungen."""
    if Lagerbewegung.objects.filter(beleg=beleg, art=Bewegungsart.VERKAUF).exists():
        return 0
    mengen, artikel = buchmengen(beleg)
    if not mengen:
        return 0
    knapp = fehlbestand(beleg)  # Lieferschein/Rechnung nur mit ausreichendem Lagerbestand: sonst würde er unter 0 fallen
    if knapp:
        raise BestandReichtNicht("Lagerbestand reicht nicht für " + beleg.get_art_display() + ": " + "; ".join(knapp) +
                                 ". Erst Wareneingang buchen oder die Menge ändern.")
    for artikel_id, menge in mengen.items():
        Lagerbewegung.objects.create(
            mandant=beleg.mandant, artikel=artikel[artikel_id], art=Bewegungsart.VERKAUF, menge=-menge,
            beleg=beleg, bemerkung=str(beleg), benutzer=benutzer)
    return len(mengen)


def teilung(beleg):
    """Teilt die Hauptpositionen eines Belegs nach Lagerbestand: (lieferbar, knapp), je eine Liste von Positionen.
    Eine Gruppe zählt als Einheit (alle ihre Lagerartikel müssen reichen); Artikel in mehreren Positionen teilen sich den Bestand."""
    positionen = list(beleg.positionen.select_related("artikel").order_by("pos", "id"))
    unter = defaultdict(list)
    for p in positionen:
        if p.gruppe_id:
            unter[p.gruppe_id].append(p)
    rest, lieferbar, knapp = {}, [], []
    for p in positionen:
        if p.gruppe_id:
            continue
        bedarf = defaultdict(Decimal)
        for q in [p] + unter.get(p.pk, []):
            if q.artikel_id and q.artikel.typ == LAGERFUEHREND:
                bedarf[q.artikel_id] += q.menge
                rest.setdefault(q.artikel_id, bestand(beleg.mandant, q.artikel))
        if all(rest[a] >= m for a, m in bedarf.items()):
            for a, m in bedarf.items():
                rest[a] -= m
            lieferbar.append(p)
        else:
            knapp.append(p)
    return lieferbar, knapp


EK_ZUGANG = (Bewegungsart.WARENEINGANG, Bewegungsart.ANFANGSBESTAND)


def _durchschnitts_ek_fortschreiben(mandant, artikel, bestand_alt, menge, preis):
    """Ø-EK im Mandanten fortschreiben (gleiche Rechnung wie beim Wareneingang), ohne den letzten Wareneingangs-EK anzufassen.
    Aufruf vor der Lagerbuchung. Nur Lager- und Lieferantenartikel haben einen Ø-EK."""
    from apps.stammdaten.ekkalk import neuer_durchschnitts_ek
    from apps.stammdaten.models import ArtikelMandant, Artikeltyp
    if artikel.typ not in (Artikeltyp.A, Artikeltyp.P):
        return
    am, _ = ArtikelMandant.objects.get_or_create(artikel=artikel, mandant=mandant)
    am.ek_avg = neuer_durchschnitts_ek(bestand_alt, am.ek_avg, menge, preis)
    am.save(update_fields=["ek_avg"])


@transaction.atomic
def buche_manuell(mandant, artikel, art, menge, bemerkung="", benutzer=None, ek=None):
    """Manuelle Lagerbuchung. ek (netto je Stück) nur bei einem Zugang (Wareneingang, Anfangsbestand): schreibt den Ø-EK fort."""
    from apps.stammdaten.models import Artikel
    if art in (Bewegungsart.EIGENVERBRAUCH, Bewegungsart.VERKAUF):
        menge = -abs(menge)
    elif art in (Bewegungsart.WARENEINGANG, Bewegungsart.RETOURE, Bewegungsart.ANFANGSBESTAND):
        menge = abs(menge)
    if menge == 0:
        raise ValueError("Menge darf nicht 0 sein.")
    if ek is not None:
        if ek < 0:
            raise ValueError("Der EK darf nicht negativ sein.")
        if art not in EK_ZUGANG or menge < 0:
            raise ValueError("Ein EK-Preis ist nur bei einem Zugang (Wareneingang, Anfangsbestand) möglich.")
        list(Artikel.objects.select_for_update().filter(pk=artikel.pk).values_list("pk", flat=True))
        _durchschnitts_ek_fortschreiben(mandant, artikel, bestand(mandant, artikel), menge, ek)
    return Lagerbewegung.objects.create(mandant=mandant, artikel=artikel, art=art, menge=menge,
                                        bemerkung=bemerkung, benutzer=benutzer)


@transaction.atomic
def inventur(mandant, artikel, gezaehlt: Decimal, benutzer=None, soll=None):
    """Differenzbuchung auf den gezählten Bestand. soll = der in der Zählliste angezeigte Bestand: Weicht er vom jetzigen ab,
    wurde während des Zählens gebucht – dann ist unklar, ob die Zählung davor oder danach lag, und es wird nichts gebucht."""
    from apps.stammdaten.models import Artikel

    from .models import bestand
    # Artikel sperren, damit zwei gleichzeitige Buchungen (Doppelklick) die Differenz nicht zweimal auf denselben Bestand rechnen
    list(Artikel.objects.select_for_update().filter(pk=artikel.pk).values_list("pk", flat=True))
    ist = bestand(mandant, artikel)
    if soll is not None and soll != ist:
        raise ValueError(f"Der Bestand hat sich seit dem Öffnen der Zählliste geändert (war {soll.normalize():f}, ist jetzt {ist.normalize():f}) "
                         "– nicht gebucht, bitte neu zählen.")
    differenz = gezaehlt - ist
    if differenz == 0:
        return None
    return Lagerbewegung.objects.create(
        mandant=mandant, artikel=artikel, art=Bewegungsart.INVENTUR, menge=differenz,
        bemerkung=f"Inventur: gezählt {gezaehlt}", benutzer=benutzer)


def zeilen(text: str) -> list:
    return list(dict.fromkeys(z.strip() for z in (text or "").replace(",", chr(10)).splitlines() if z.strip()))


@transaction.atomic
def seriennummern_einlagern(mandant, artikel, text, bemerkung="", wareneingang=None):
    """Mit ``wareneingang`` bleibt nachvollziehbar, von welchem Lieferanten das Stück stammt (Reklamation)."""
    neu = []
    for nr in zeilen(text):
        sn, angelegt = Seriennummer.objects.get_or_create(
            mandant=mandant, artikel=artikel, nummer=nr, defaults={"eingang_bemerkung": bemerkung, "wareneingang": wareneingang})
        if not angelegt:
            if sn.status == Seriennummer.IM_LAGER:
                raise ValueError(f"Seriennummer {nr} ist bereits im Lager.")
            sn.status, sn.position = Seriennummer.IM_LAGER, None  # Retoure: wieder eingelagert
            sn.eingang_bemerkung = bemerkung
            if wareneingang is not None:  # Retoure ohne Wareneingang: die ursprüngliche Herkunft bleibt stehen
                sn.wareneingang = wareneingang
            sn.save()
        neu.append(sn)
    return neu


@transaction.atomic
def seriennummern_zuordnen(position, text):
    """Ordnet einer Belegposition Seriennummern aus dem Lager zu (nur Lagerartikel, nicht mehr als die Menge)."""
    beleg = position.beleg
    nummern = zeilen(text)
    if len(nummern) > position.menge:
        raise ValueError(f"Mehr Seriennummern ({len(nummern)}) als Menge ({position.menge:f}).")
    for sn in position.seriennummern.all():  # vorherige Zuordnung dieser Position lösen
        sn.status, sn.position = Seriennummer.IM_LAGER, None
        sn.save()
    for nr in nummern:
        sn = Seriennummer.objects.filter(mandant=beleg.mandant, artikel=position.artikel, nummer=nr).first()
        if sn is None or sn.status != Seriennummer.IM_LAGER:
            raise ValueError(f"Seriennummer {nr} ist für {position.artikel.nummer} nicht im Lager.")
        sn.status, sn.position = Seriennummer.VERKAUFT, position
        sn.save()


def seriennummer_wieder_einlagern(seriennummer):
    """Nach einer Gutschrift: Nur die Nummer kommt zurück, den Bestand bucht die Gutschrift selbst."""
    position = seriennummer.position
    if seriennummer.status != Seriennummer.VERKAUFT or (position is not None and not position.beleg.abgeschlossen):
        raise ValueError(f"Seriennummer {seriennummer.nummer} lässt sich nicht wieder einlagern: Sie gehört zu keinem abgeschlossenen Verkauf.")
    seriennummer.status, seriennummer.position = Seriennummer.IM_LAGER, None
    seriennummer.eingang_bemerkung = f"Wieder eingelagert, zuvor {position.beleg}" if position else "Wieder eingelagert"
    seriennummer.save()


@transaction.atomic
def umbuchen(von_mandant, nach_mandant, artikel, menge, benutzer=None, seriennummern=None):
    """Umbuchung zwischen Mandanten. Seriennummern gehören einem Mandanten: Die umgebuchten wandern mit. Bleiben im Quellmandanten
    mehr Nummern im Lager als Bestand, müssen die umgebuchten Nummern angegeben werden (sonst verwaisen sie dort)."""
    from apps.stammdaten.models import Artikel
    if von_mandant == nach_mandant:
        raise ValueError("Quell- und Zielmandant sind gleich.")
    if menge <= 0:
        raise ValueError("Menge muss positiv sein.")
    list(Artikel.objects.select_for_update().filter(pk=artikel.pk).values_list("pk", flat=True))  # wie bei Abgängen sperren
    nummern = list(dict.fromkeys(seriennummern or []))
    if len(nummern) > menge:
        raise ValueError(f"Mehr Seriennummern ({len(nummern)}) als Menge ({menge:f}).")
    text = f"Umbuchung {von_mandant.kurzname} → {nach_mandant.kurzname}"
    from apps.stammdaten.models import ArtikelMandant
    quelle_ek = ArtikelMandant.objects.filter(artikel=artikel, mandant=von_mandant).values_list("ek_avg", flat=True).first()
    ziel_bestand = bestand(nach_mandant, artikel)  # vor der Buchung, wie beim Wareneingang
    Lagerbewegung.objects.create(mandant=von_mandant, artikel=artikel, art=Bewegungsart.UMBUCHUNG, menge=-menge,
                                 bemerkung=text, benutzer=benutzer)  # prüft den Bestand
    Lagerbewegung.objects.create(mandant=nach_mandant, artikel=artikel, art=Bewegungsart.UMBUCHUNG, menge=menge,
                                 bemerkung=text, benutzer=benutzer)
    if quelle_ek is not None:  # Ziel übernimmt den Ø-EK der Quelle (gewogen); die Quelle behält ihren
        _durchschnitts_ek_fortschreiben(nach_mandant, artikel, ziel_bestand, menge, quelle_ek)
    im_lager = Seriennummer.objects.filter(mandant=von_mandant, artikel=artikel, status=Seriennummer.IM_LAGER)
    for nr in nummern:
        sn = im_lager.filter(nummer=nr).first()
        if sn is None:
            raise ValueError(f"Seriennummer {nr} ist bei {von_mandant.kurzname} für {artikel.nummer} nicht im Lager.")
        vorhanden = Seriennummer.objects.filter(mandant=nach_mandant, artikel=artikel, nummer=nr).first()
        if vorhanden is not None and vorhanden.status == Seriennummer.IM_LAGER:
            raise ValueError(f"Seriennummer {nr} ist bei {nach_mandant.kurzname} bereits im Lager.")
        if vorhanden is not None:  # früher von dort verkauft oder umgebucht: die Nummer ist wieder im Lager
            vorhanden.status, vorhanden.position, vorhanden.eingang_bemerkung = Seriennummer.IM_LAGER, None, text
            vorhanden.wareneingang = sn.wareneingang or vorhanden.wareneingang  # Herkunft wandert mit
            vorhanden.save()
            sn.delete()
        else:
            sn.mandant, sn.eingang_bemerkung = nach_mandant, text
            sn.save()
    ueberzaehlig = im_lager.count() - max(bestand(von_mandant, artikel), Decimal("0"))
    if ueberzaehlig > 0:  # sonst blieben Nummern ohne Bestand im Quellmandanten zurück
        raise ValueError(f"Bei {von_mandant.kurzname} liegen {ueberzaehlig:.0f} Seriennummer(n) mehr im Lager als Bestand bleibt: "
                         "Bitte die Nummern angeben, die mit umgebucht werden.")


def rueckgabe_mengen(positionen) -> dict:
    """Menge je Lagerartikel, die mit R oder B zurückgebucht wird."""
    mengen = defaultdict(Decimal)
    for p in positionen.filter(ruecknahme__in=(Ruecknahme.REPARATUR, Ruecknahme.LAGER), artikel__typ=LAGERFUEHREND):
        mengen[p.artikel_id] += p.menge
    return mengen


def gutschrift_menge_pruefen(gutschrift):
    """Über alle Gutschriften einer Rechnung kommt je Artikel höchstens zurück, was auf der Rechnung steht."""
    rechnung = gutschrift.vorgaenger
    if rechnung is None or rechnung.art != Belegart.RECHNUNG:
        return
    verkauft = defaultdict(Decimal)
    for p in rechnung.positionen.filter(artikel__typ=LAGERFUEHREND):
        verkauft[p.artikel_id] += p.menge
    frueher = defaultdict(Decimal)
    for g in rechnung.folgebelege.filter(art=Belegart.GUTSCHRIFT, abgeschlossen=True).exclude(pk=gutschrift.pk):
        for artikel_id, menge in rueckgabe_mengen(g.positionen).items():
            frueher[artikel_id] += menge
    for artikel_id, menge in rueckgabe_mengen(gutschrift.positionen).items():
        if frueher[artikel_id] + menge > verkauft[artikel_id]:
            nummer = gutschrift.positionen.filter(artikel_id=artikel_id).first().artikel.nummer
            raise ValueError(f"Artikel {nummer}: Zurückgebucht würden {(frueher[artikel_id] + menge).normalize():f}, auf der Rechnung "
                             f"{rechnung.nummer} stehen {verkauft[artikel_id].normalize():f}. Menge korrigieren oder in Spalte B „–“ wählen.")


@transaction.atomic
def gutschrift_buchen(gutschrift, benutzer=None) -> int:
    """Bucht die Ware beim Abschließen der Gutschrift nach Spalte B: R in den Reparaturbestand, B in den Lagerbestand."""
    mengen, artikel = defaultdict(Decimal), {}
    for p in gutschrift.positionen.filter(ruecknahme__in=(Ruecknahme.REPARATUR, Ruecknahme.LAGER),
                                          artikel__typ=LAGERFUEHREND).select_related("artikel"):
        mengen[(p.artikel_id, p.ruecknahme)] += p.menge
        artikel[p.artikel_id] = p.artikel
    for (artikel_id, ziel), menge in mengen.items():
        if ziel == Ruecknahme.LAGER:
            Lagerbewegung.objects.create(mandant=gutschrift.mandant, artikel=artikel[artikel_id], art=Bewegungsart.RETOURE,
                                         menge=menge, beleg=gutschrift, bemerkung=str(gutschrift), benutzer=benutzer)
        else:
            Reparaturbewegung.objects.create(mandant=gutschrift.mandant, artikel=artikel[artikel_id], art=Reparaturart.ZUGANG,
                                             menge=menge, beleg=gutschrift, bemerkung=str(gutschrift), benutzer=benutzer)
    return len(mengen)


@transaction.atomic
def reparatur_umbuchen(mandant, artikel, menge, bemerkung="", benutzer=None):
    """Repariert: vom Reparaturbestand in den Lagerbestand."""
    if menge <= 0:
        raise ValueError("Menge muss positiv sein.")
    Reparaturbewegung.objects.create(mandant=mandant, artikel=artikel, art=Reparaturart.LAGER, menge=-menge,
                                     bemerkung=bemerkung, benutzer=benutzer)
    return Lagerbewegung.objects.create(mandant=mandant, artikel=artikel, art=Bewegungsart.REPARATUR, menge=menge,
                                        bemerkung=bemerkung, benutzer=benutzer)


def reparatur_ausbuchen(mandant, artikel, menge, bemerkung, benutzer=None):
    """Schrott oder Rückgabe an den Lieferanten: Die Ware verlässt den Reparaturbestand, der Lagerbestand bleibt unberührt."""
    if menge <= 0:
        raise ValueError("Menge muss positiv sein.")
    if not bemerkung:
        raise ValueError("Bitte beim Ausbuchen eine Bemerkung angeben, zum Beispiel Schrott oder an welchen Lieferanten.")
    return Reparaturbewegung.objects.create(mandant=mandant, artikel=artikel, art=Reparaturart.AUSBUCHUNG, menge=-menge,
                                            bemerkung=bemerkung, benutzer=benutzer)


def retoure_offen(gutschrift) -> bool:
    """Ältere Gutschriften ohne Spalte B werden weiter von Hand zurückgebucht (einmal je Gutschrift)."""
    return (gutschrift.art == Belegart.GUTSCHRIFT and gutschrift.abgeschlossen
            and gutschrift.positionen.filter(ruecknahme="", artikel__typ=LAGERFUEHREND).exists()
            and not Lagerbewegung.objects.filter(beleg=gutschrift, art=Bewegungsart.RETOURE).exists())


@transaction.atomic
def retoure_aus_gutschrift(gutschrift, benutzer=None) -> int:
    """Bucht die Lagerartikel einer abgeschlossenen älteren Gutschrift wieder ins Lager (einmal je Gutschrift)."""
    if gutschrift.art != Belegart.GUTSCHRIFT or not gutschrift.abgeschlossen:
        raise ValueError("Nur zu einer abgeschlossenen Gutschrift möglich.")
    type(gutschrift).objects.select_for_update().filter(pk=gutschrift.pk).first()  # Doppelklick darf nicht doppelt buchen
    if Lagerbewegung.objects.filter(beleg=gutschrift, art=Bewegungsart.RETOURE).exists():
        raise ValueError("Diese Gutschrift wurde bereits ins Lager zurückgebucht.")
    mengen, artikel = defaultdict(Decimal), {}
    for p in gutschrift.positionen.filter(ruecknahme="").select_related("artikel"):
        if p.artikel_id and p.artikel.typ == LAGERFUEHREND:
            mengen[p.artikel_id] += p.menge
            artikel[p.artikel_id] = p.artikel
    for artikel_id, menge in mengen.items():
        Lagerbewegung.objects.create(mandant=gutschrift.mandant, artikel=artikel[artikel_id], art=Bewegungsart.RETOURE,
                                     menge=menge, beleg=gutschrift, bemerkung=str(gutschrift), benutzer=benutzer)
    return len(mengen)
