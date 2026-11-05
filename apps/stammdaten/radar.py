"""
Preis-Radar (Issue 12, Schritt 3). Sammelt Preisgefahren je Mandant und sortiert sie nach Euro-Wert.

Vergleichsbasis: Bestand > 0 und Ø-EK vorhanden -> Ø-EK, sonst ek_kalk (Import-EK, letzter Wareneingang oder EK2).
Euro-Wert = (Bestand + offene Bestellungen + offene Angebote) × Euro-Differenz je Stück.

Ruhe-Regeln:
- Dashboard und Radar-Liste nur mit Menge > 0 (Bestand oder offener Beleg); „EK2 prüfen“ und „Kein EK“ nur als Ampel in der Artikelliste.
- Typ P ohne Bestand und ohne offene Belege nie; Typ L nur mit hinterlegten Kosten; Typ G nie.
- Differenzen unter 3 % der Basis oder unter 0,50 € je Stück werden nicht gemeldet (einstellbar).
- „Gesehen“ blendet aus, bis sich ek_kalk, ek_avg oder VK spürbar ändern (gleiche Grenzen). warnungen_aus: nie im Radar.
"""
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

from django.db.models import Q, Sum
from django.utils import timezone

from .ekkalk import QUELLEN, abweichung_prozent, ek_import, kalkulations_ek, marge_stufe, vk_uebernahme, vk_vorschlag_aus
from .models import Artikel, ArtikelLieferant, ArtikelMandant, Artikeltyp, EkHerkunft, PreisVorgabe, RadarGesehen
from .preise import LADEN

NULL = Decimal("0")
QUELLEN_RADAR = {**QUELLEN, "kosten": "Kosten"}

# Reihenfolge = Schwere (für die Ampel); (Schlüssel, Name, Ampelfarbe)
KATEGORIEN = (
    ("verlust", "Verlust", "rot"),
    ("marge", "Marge unter Ziel", "gelb"),
    ("verfall", "Preisverfall", "blau"),
    ("erhoehung", "Preiserhöhung möglich", "gruen"),
    ("ek2_pruefen", "EK2 prüfen", "grau"),
    ("ek_abweichung", "EK-Abweichung", "grau"),
    ("kein_ek", "Kein EK", "grau"),
)
NAMEN = {k: n for k, n, _ in KATEGORIEN}
FARBEN = {k: f for k, _, f in KATEGORIEN}
NUR_LISTE = ("ek2_pruefen", "kein_ek")  # nie im Dashboard, nur Ampel in der Artikelliste
AMPEL_REIHENFOLGE = ("rot", "gelb", "blau", "gruen", "grau")


def einstellungen() -> dict:
    v = PreisVorgabe.objects.first()
    if v is None:
        return {"faktor": Decimal("0.85"), "ek2_abweichung": Decimal("20"), "ek2_tage": 180,
                "prozent": Decimal("3"), "euro": Decimal("0.50")}
    return {"faktor": v.radar_preisverfall_faktor, "ek2_abweichung": v.radar_ek2_abweichung, "ek2_tage": v.radar_ek2_alter_tage,
            "prozent": v.radar_ignorieren_prozent, "euro": v.radar_ignorieren_euro}


@dataclass(frozen=True)
class Befund:
    kategorie: str
    diff: Decimal | None = None  # Euro-Differenz je Stück; None = ohne Euro-Wert

    @property
    def name(self):
        return NAMEN[self.kategorie]

    @property
    def farbe(self):
        return FARBEN[self.kategorie]


def _zu_klein(diff, basis, einst) -> bool:
    """Ruhe-Regel: Differenz unter 0,50 € je Stück oder unter 3 % der Basis wird ignoriert."""
    if diff < einst["euro"]:
        return True
    return bool(basis) and diff / basis * 100 < einst["prozent"]


def bewerte(*, bestand, ek_avg, ek_import_wert, ek_kalk, quelle, vk, vorschlag, vk_fix, ziel_marge, einst,
            ek2=None, ek2_datum=None, ek_we=None, jetzt=None):
    """Reine Regel ohne Datenbank. Gibt (basis, basis_quelle, befunde) zurück; befunde nach Schwere sortiert."""
    jetzt = jetzt or timezone.now()
    if bestand > 0 and ek_avg is not None:
        basis, basis_quelle = ek_avg, "Ø-EK"
    elif ek_kalk is not None:
        basis, basis_quelle = ek_kalk, QUELLEN_RADAR[quelle]
    else:
        return None, "", [Befund("kein_ek")] if quelle == "keine" else []
    befunde = []
    preis = vk if vk is not None else vorschlag
    unter_ziel = False
    if preis is not None:
        if preis < basis:
            unter_ziel = True
            diff = basis - preis
            if not _zu_klein(diff, basis, einst):
                befunde.append(Befund("verlust", diff))
        elif ziel_marge is not None and ziel_marge < 1 and preis > 0 and (preis - basis) / preis < ziel_marge:
            unter_ziel = True
            diff = basis / (1 - ziel_marge) - preis
            if not _zu_klein(diff, basis, einst):
                befunde.append(Befund("marge", diff))
    if bestand > 0 and ek_avg is not None and ek_import_wert is not None and ek_import_wert < ek_avg * einst["faktor"]:
        diff = ek_avg - ek_import_wert
        if not _zu_klein(diff, ek_avg, einst):
            befunde.append(Befund("verfall", diff))
    if vk_fix and vk is not None and vorschlag is not None and vorschlag > vk and not unter_ziel:
        diff = vorschlag - vk
        if not _zu_klein(diff, basis, einst):
            befunde.append(Befund("erhoehung", diff))
    if quelle == "ek2" and ek2_datum is not None and jetzt - ek2_datum > timedelta(days=einst["ek2_tage"]):
        befunde.append(Befund("ek2_pruefen"))
    if ek2 is not None and quelle not in ("ek2", "ek2_fix"):
        vergleich = ek_import_wert if ek_import_wert is not None else ek_we
        abw = abweichung_prozent(ek2, vergleich)
        if abw is not None and abw > einst["ek2_abweichung"]:
            befunde.append(Befund("ek_abweichung"))
    reihenfolge = [k for k, _, _ in KATEGORIEN]
    befunde.sort(key=lambda b: reihenfolge.index(b.kategorie))
    return basis, basis_quelle, befunde


@dataclass
class Eintrag:
    artikel: Artikel
    bestand: Decimal
    menge: Decimal  # Bestand (nur positiv) + offene Bestellungen und Angebote
    ek_kalk: Decimal | None
    ek_avg: Decimal | None
    quelle: str
    basis: Decimal | None
    basis_quelle: str
    vk: Decimal | None
    vorschlag: Decimal | None
    befunde: list = field(default_factory=list)
    warnungen_aus: bool = False
    gesehen: bool = False
    senkung_pruefen: bool = False

    @property
    def ampel(self) -> str:
        farben = {b.farbe for b in self.befunde}
        return next((f for f in AMPEL_REIHENFOLGE if f in farben), "")

    @property
    def gemeldet(self) -> bool:
        """Erscheint im Radar (Dashboard und Liste)."""
        return not self.warnungen_aus and not self.gesehen and self.menge > 0

    def euro(self, befund) -> Decimal:
        return NULL if befund.diff is None else (self.menge * befund.diff).quantize(Decimal("0.01"))

    def meldungen(self):
        """Befunde, die im Radar stehen (nicht, wenn ausgeblendet oder ohne Menge)."""
        return [b for b in self.befunde if b.kategorie not in NUR_LISTE] if self.gemeldet else []


def _geaendert(alt, neu, einst) -> bool:
    if alt is None and neu is None:
        return False
    if alt is None or neu is None:
        return True
    d = abs(neu - alt)
    return d > 0 and d >= einst["euro"] and (alt == 0 or d / abs(alt) * 100 >= einst["prozent"])


def eintrag(artikel, mandant, bestand=None, offen=None, einst=None, jetzt=None, *, am=LADEN, gesehen_zeile=LADEN, imp=LADEN,
            vorgabe=LADEN) -> Eintrag | None:
    """Radar-Auswertung eines Artikels im Mandanten; None, wenn der Artikel nie gemeldet wird (Typ G, L ohne Kosten, P ohne Menge).
    am, gesehen_zeile, imp, vorgabe: bereits geladene Werte (ArtikelMandant, RadarGesehen, Import-EK, PreisVorgabe) für Listen;
    ohne Angabe werden sie hier geladen."""
    if artikel.typ == Artikeltyp.G:
        return None
    einst = einst or einstellungen()
    if bestand is None:
        from apps.lager.models import bestand as lagerbestand
        bestand = lagerbestand(mandant, artikel)
    if offen is None:
        offen = offene_mengen(mandant, [artikel.pk]).get(artikel.pk, NULL)
    menge = max(bestand, NULL) + offen
    if artikel.typ == Artikeltyp.P and menge <= 0:
        return None
    if am is LADEN:
        am = ArtikelMandant.objects.filter(artikel=artikel, mandant=mandant).first()
    if imp is LADEN:
        imp = ek_import(artikel)
    if vorgabe is LADEN:
        vorgabe = PreisVorgabe.objects.first()
    auf_99 = bool(vorgabe and vorgabe.rundung_99)
    marge = marge_stufe(artikel, 1, vorgabe)
    if artikel.typ == Artikeltyp.L:
        if artikel.kosten is None:
            return None
        ek_kalk, quelle, vorschlag, vk_fix = artikel.kosten, "kosten", None, False
    else:
        k = kalkulations_ek(bestand, am.ek_avg if am else None, imp, am.ek_we if am else None,
                            am.ek2 if am else None, am.ek2_fix if am else False)
        ek_kalk, quelle = k.ek_kalk, k.quelle
        vorschlag = vk_vorschlag_aus(ek_kalk, marge, auf_99)
        vk_fix = am.vk_fix1 if am else False
    ek_avg = am.ek_avg if am else None
    basis, basis_quelle, befunde = bewerte(
        bestand=bestand, ek_avg=ek_avg, ek_import_wert=imp, ek_kalk=ek_kalk, quelle=quelle, vk=artikel.vk1, vorschlag=vorschlag,
        vk_fix=vk_fix, ziel_marge=marge, einst=einst, ek2=am.ek2 if am else None,
        ek2_datum=am.ek2_datum if am else None, ek_we=am.ek_we if am else None, jetzt=jetzt)
    g = RadarGesehen.objects.filter(artikel=artikel, mandant=mandant).first() if gesehen_zeile is LADEN else gesehen_zeile
    gesehen = g is not None and not any(_geaendert(a, n, einst) for a, n in ((g.ek_kalk, ek_kalk), (g.ek_avg, ek_avg), (g.vk, artikel.vk1)))
    senkung = (artikel.typ != Artikeltyp.L and not vk_fix and artikel.vk1 is not None
               and vk_uebernahme(artikel.vk1, vorschlag, False) == "senkung_pruefen")
    return Eintrag(artikel=artikel, bestand=bestand, menge=menge, ek_kalk=ek_kalk, ek_avg=ek_avg, quelle=quelle, basis=basis,
                   basis_quelle=basis_quelle, vk=artikel.vk1, vorschlag=vorschlag, befunde=befunde,
                   warnungen_aus=bool(am and am.warnungen_aus), gesehen=gesehen, senkung_pruefen=senkung)


def ampel(artikel, mandant) -> str:
    """Ampelfarbe für die Artikelliste: rot, gelb, blau, gruen, grau oder leer. Ausgeblendete Artikel zeigen ihre Ampel weiter."""
    e = eintrag(artikel, mandant)
    return e.ampel if e else ""


def ampeln(artikel_liste, mandant) -> dict:
    """{artikel_pk: Ampelfarbe} für eine ganze Liste (Seite der Artikelverwaltung) mit gesammelten Abfragen statt je Zeile.
    Gleiche Farben wie ampel(); Artikel ohne Eintrag (Typ G, L ohne Kosten, P ohne Menge) fehlen im Ergebnis.
    Voraussetzung für wenige Abfragen: kategorie__eltern__eltern per select_related geladen."""
    from apps.lager.models import Lagerbewegung
    artikel_liste = [a for a in artikel_liste if a.typ != Artikeltyp.G]
    ids = [a.pk for a in artikel_liste]
    if not ids:
        return {}
    bestaende = {z["artikel_id"]: z["s"] for z in Lagerbewegung.objects.filter(mandant=mandant, artikel_id__in=ids)
                 .order_by().values("artikel_id").annotate(s=Sum("menge"))}
    offen = offene_mengen(mandant, ids)
    mandantendaten = {am.artikel_id: am for am in ArtikelMandant.objects.filter(mandant=mandant, artikel_id__in=ids)}
    gesehen = {g.artikel_id: g for g in RadarGesehen.objects.filter(mandant=mandant, artikel_id__in=ids)}
    guenstigst = {}
    for al in ArtikelLieferant.objects.filter(artikel_id__in=ids, ek__gt=0).order_by("artikel_id", "ek"):
        guenstigst.setdefault(al.artikel_id, al.ek)
    einst, vorgabe = einstellungen(), PreisVorgabe.objects.first()
    ergebnis = {}
    for a in artikel_liste:
        imp = a.durchschnitts_ek if a.durchschnitts_ek and a.ek_herkunft != EkHerkunft.WARENEINGANG else guenstigst.get(a.pk)
        e = eintrag(a, mandant, bestaende.get(a.pk, NULL) or NULL, offen.get(a.pk, NULL), einst, am=mandantendaten.get(a.pk),
                    gesehen_zeile=gesehen.get(a.pk), imp=imp, vorgabe=vorgabe)
        if e:
            ergebnis[a.pk] = e.ampel
    return ergebnis


def offene_mengen(mandant, artikel_ids=None, heute=None) -> dict:
    """{artikel_id: Menge} aus offenen Bestellungen (ohne Folgebeleg) und gültigen offenen Angeboten des Mandanten."""
    from apps.verkauf.models import Belegart, Position
    heute = heute or timezone.localdate()
    from apps.verkauf.models import mit_offener_menge
    basis = Position.objects.filter(beleg__mandant=mandant, artikel__isnull=False, gruppe__isnull=True)
    if artikel_ids is not None:
        basis = basis.filter(artikel_id__in=artikel_ids)
    angebote = basis.filter(beleg__folgebelege__isnull=True, beleg__art=Belegart.ANGEBOT).filter(
        Q(beleg__gueltig_bis__isnull=True) | Q(beleg__gueltig_bis__gte=heute))
    ergebnis = {z["artikel_id"]: z["s"] for z in angebote.order_by().values("artikel_id").annotate(s=Sum("menge"))}
    # Bestellungen zählen mit ihrem Rückstand (offene Menge)
    auftraege = mit_offener_menge(basis.filter(beleg__art=Belegart.AUFTRAG)).filter(offen__gt=0)
    for z in auftraege.order_by().values("artikel_id").annotate(s=Sum("offen")):
        ergebnis[z["artikel_id"]] = ergebnis.get(z["artikel_id"], 0) + z["s"]
    return ergebnis


def eintraege(mandant, jetzt=None) -> list:
    """Alle Artikel mit Menge > 0 (Bestand oder offene Belege) bewertet, nur solche mit mindestens einem Befund oder VK-Senkung."""
    from apps.lager.models import Lagerbewegung
    bestaende = {z["artikel_id"]: z["s"] for z in Lagerbewegung.objects.filter(mandant=mandant).order_by().values("artikel_id").annotate(s=Sum("menge"))}
    offen = offene_mengen(mandant)
    ids = {a for a, s in bestaende.items() if s and s > 0} | set(offen)
    einst = einstellungen()
    liste = []
    for artikel in Artikel.objects.filter(pk__in=ids).select_related("kategorie"):
        e = eintrag(artikel, mandant, bestaende.get(artikel.pk, NULL) or NULL, offen.get(artikel.pk, NULL), einst, jetzt)
        if e and (e.befunde or e.senkung_pruefen):
            liste.append(e)
    return liste


def auswertung(mandant, jetzt=None) -> dict:
    """Dashboard-Kachel: je Kategorie Anzahl und Euro-Summe, dazu „VK-Senkung prüfen“ und „Import zur Prüfung“."""
    from apps.einkauf.models import Preislistenimport, Preispruefung
    liste = eintraege(mandant, jetzt)
    kategorien = []
    for schluessel, name, farbe in KATEGORIEN:
        if schluessel in NUR_LISTE:
            continue
        zeilen = [(e, b) for e in liste for b in e.meldungen() if b.kategorie == schluessel]
        kategorien.append({"schluessel": schluessel, "name": name, "farbe": farbe, "anzahl": len(zeilen),
                           "euro": sum((e.euro(b) for e, b in zeilen), NULL)})
    senkung = [e for e in liste if e.senkung_pruefen and not e.warnungen_aus and e.menge > 0]
    importe = Preislistenimport.objects.filter(status="zur_pruefung").count() + Preispruefung.objects.filter(status="offen").count()
    return {"kategorien": kategorien, "senkung": len(senkung), "importe": importe,
            "rot": next((k["anzahl"] for k in kategorien if k["schluessel"] == "verlust"), 0)}


def zeilen(mandant, kategorie, jetzt=None) -> list:
    """Liste einer Kategorie, nach Euro-Wert absteigend: [(Eintrag, Befund, Euro)]. 'senkung' liefert die Liste „VK-Senkung prüfen“."""
    liste = eintraege(mandant, jetzt)
    if kategorie == "senkung":
        return sorted(((e, None, (e.menge * (e.vk - e.vorschlag)).quantize(Decimal("0.01"))) for e in liste
                       if e.senkung_pruefen and not e.warnungen_aus and e.menge > 0), key=lambda z: -z[2])
    ergebnis = [(e, b, e.euro(b)) for e in liste for b in e.meldungen() if b.kategorie == kategorie]
    return sorted(ergebnis, key=lambda z: (-z[2], z[0].artikel.nummer))


def gesehen_setzen(artikel, mandant) -> None:
    """Button „Gesehen“: merkt den aktuellen Stand; erst eine spürbare Änderung bringt den Artikel zurück."""
    e = eintrag(artikel, mandant)
    if e is None:
        return
    RadarGesehen.objects.update_or_create(artikel=artikel, mandant=mandant,
                                          defaults={"ek_kalk": e.ek_kalk, "ek_avg": e.ek_avg, "vk": artikel.vk1})


def vk_senkung_uebernehmen(artikel, mandant) -> Decimal:
    """Übernimmt den VK-Vorschlag als VK1 (Klick in „VK-Senkung prüfen“). ValueError, wenn nichts zu übernehmen ist."""
    e = eintrag(artikel, mandant)
    if e is None or not e.senkung_pruefen or e.vorschlag is None:
        raise ValueError("Für diesen Artikel liegt keine VK-Senkung zur Prüfung vor.")
    artikel.vk1 = e.vorschlag
    artikel.save(update_fields=["vk1", "geaendert_am"])
    return e.vorschlag
