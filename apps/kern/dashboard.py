"""Kennzahlen für das Dashboard. Alles strikt je Mandant, Aggregation in der Datenbank.

Fachliche Festlegungen:
- Umsatz (netto) = abgeschlossene Rechnungen minus abgeschlossene Gutschriften nach Belegdatum.
  Proforma, Angebot, Bestellung und Lieferschein zählen nie; Entwürfe auch nicht.
- Offene Kundenbestellung = Beleg der Art AB mit mindestens einer Position, zu dem es noch keinen Lieferschein
  und keine Rechnung gibt (folge_pruefen erlaubt aus einer Bestellung genau eine Lieferung bzw. Abrechnung).
  Ob die Bestellung schon abgeschlossen (nummeriert) ist, spielt keine Rolle: Offen heißt „noch nicht ausgeliefert“.
- Offene Angebote = Beleg der Art AN mit Positionen, ohne Folgebeleg und (falls befristet) noch gültig.
- Offene Posten Kunden = abgeschlossene Rechnungen mit Restbetrag (Brutto − Zahlungen − verrechnete Gutschriften),
  abzüglich der noch unverrechneten Gutschriften, wie auf der Seite „Offene Posten“.
- Offene Lieferantenbestellung = Status „bestellt“ oder „teilweise geliefert“; Summe = Wert der noch nicht gelieferten Menge.
- Alle zeitabhängigen Funktionen nehmen „heute“ als Parameter (Tests).
"""
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from math import ceil

from django.db.models import Case, Count, DecimalField, Exists, ExpressionWrapper, F, OuterRef, Q, Subquery, Sum, Value, When
from django.db.models.functions import Coalesce, Greatest
from django.utils import timezone

from apps.einkauf.models import BestellPosition, BestellStatus, Bestellung, Lieferantenrechnung, RechnungStatus
from apps.finanzen.models import Gutschriftverrechnung, Zahlung
from apps.shop.models import ShopBestellung
from apps.verkauf.models import Beleg, Belegart, Position, Wiedervorlage
from apps.werkstatt.models import Reparatur, Status

ZEITRAEUME = (7, 30, 90, 365)
STANDARD_TAGE = 30
CENT = Decimal("0.01")
GELD = DecimalField(max_digits=16, decimal_places=2)
NULL = Value(Decimal("0"), output_field=GELD)
UMSATZ_ARTEN = (Belegart.RECHNUNG, Belegart.GUTSCHRIFT)

# Netto mit Vorzeichen: Rechnung +, Gutschrift −
NETTO_MIT_VORZEICHEN = Case(
    When(art=Belegart.RECHNUNG, then=F("summe_netto")),
    When(art=Belegart.GUTSCHRIFT, then=F("summe_netto") * Value(Decimal("-1"))),
    default=NULL, output_field=GELD)


def zeitraum_pruefen(wert) -> int:
    """?tage=…: nur die angebotenen Zeiträume, sonst 30."""
    try:
        tage = int(wert)
    except (TypeError, ValueError):
        return STANDARD_TAGE
    return tage if tage in ZEITRAEUME else STANDARD_TAGE


def de_betrag(wert) -> str:
    """1234.5 -> '1.234,50' (für Tooltips im Diagramm)."""
    wert = Decimal(wert).quantize(CENT, rounding=ROUND_HALF_UP)
    return f"{wert:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def umsatzbelege(mandant, von, bis):
    """Abgeschlossene Rechnungen und Gutschriften im Zeitraum (beide Tage eingeschlossen)."""
    return Beleg.objects.filter(mandant=mandant, abgeschlossen=True, art__in=UMSATZ_ARTEN,
                                belegdatum__gte=von, belegdatum__lte=bis).order_by()


def umsatz(mandant, von, bis) -> dict:
    """Netto-Umsatz (Rechnungen − Gutschriften) und Anzahl der Rechnungen im Zeitraum."""
    erg = umsatzbelege(mandant, von, bis).aggregate(
        netto=Coalesce(Sum(NETTO_MIT_VORZEICHEN), NULL), anzahl=Count("pk", filter=Q(art=Belegart.RECHNUNG)))
    return {"netto": erg["netto"], "anzahl": erg["anzahl"]}


def veraenderung(aktuell, vorher) -> dict:
    """Veränderung in % gegenüber dem Vergleichswert. Bei Vergleichswert 0 gibt es kein Prozent: „neu“ bzw. „–“.
    prozent ist der Betrag; die Richtung steht getrennt (auf/ab/gleich)."""
    aktuell, vorher = Decimal(aktuell), Decimal(vorher)
    if vorher == 0:
        if aktuell == 0:
            return {"art": "keine", "prozent": None, "richtung": "gleich"}
        return {"art": "neu", "prozent": None, "richtung": "auf" if aktuell > 0 else "ab"}
    prozent = ((aktuell - vorher) / abs(vorher) * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    richtung = "auf" if prozent > 0 else "ab" if prozent < 0 else "gleich"
    return {"art": "prozent", "prozent": abs(prozent), "richtung": richtung}


def zeitraeume(heute, tage):
    """(von, bis, vorher_von, vorher_bis): der Zeitraum endet heute, der Vorzeitraum ist gleich lang und schließt direkt an."""
    von = heute - timedelta(days=tage - 1)
    vorher_bis = von - timedelta(days=1)
    return von, heute, vorher_bis - timedelta(days=tage - 1), vorher_bis


def monatsende(tag: date) -> date:
    naechster = (tag.replace(day=28) + timedelta(days=4)).replace(day=1)
    return naechster - timedelta(days=1)


def vormonat_anfang(tag: date) -> date:
    return (tag.replace(day=1) - timedelta(days=1)).replace(day=1)


def vergleich(titel, a_text, b_text, mandant, a, b) -> dict:
    ua, ub = umsatz(mandant, *a), umsatz(mandant, *b)
    return {"titel": titel, "a_text": a_text, "b_text": b_text, "a": ua, "b": ub,
            "veraenderung": veraenderung(ua["netto"], ub["netto"])}


def zeitraumvergleiche(mandant, heute=None) -> list:
    """Heute/gestern, gestern/vorgestern, Monat bis heute/Vormonat bis zum selben Tag, letzter/vorletzter Monat.
    Hat der Vormonat weniger Tage (31.03. gegen Februar), wird der Tag gekappt."""
    heute = heute or timezone.localdate()
    gestern, vorgestern = heute - timedelta(days=1), heute - timedelta(days=2)
    anfang = heute.replace(day=1)
    vm = vormonat_anfang(heute)
    vm_bis = vm.replace(day=min(heute.day, monatsende(vm).day))
    vvm = vormonat_anfang(vm)
    return [
        vergleich("Heute", "heute", "gestern", mandant, (heute, heute), (gestern, gestern)),
        vergleich("Gestern", "gestern", "vorgestern", mandant, (gestern, gestern), (vorgestern, vorgestern)),
        vergleich("Dieser Monat", f"{anfang:%d.%m.}–{heute:%d.%m.}", f"{vm:%d.%m.}–{vm_bis:%d.%m.}", mandant,
                  (anfang, heute), (vm, vm_bis)),
        vergleich("Letzter Monat", f"{vm:%m/%Y}", f"{vvm:%m/%Y}", mandant,
                  (vm, monatsende(vm)), (vvm, monatsende(vvm))),
    ]


def offene_kundenbestellungen(mandant) -> dict:
    hat_position = Position.objects.filter(beleg=OuterRef("pk"))
    geliefert = Beleg.objects.filter(vorgaenger=OuterRef("pk"), art__in=(Belegart.LIEFERSCHEIN, Belegart.RECHNUNG))
    from apps.verkauf.models import mit_offener_menge
    rueckstand = mit_offener_menge(Position.objects.filter(beleg=OuterRef("pk"))).filter(offen__gt=0)
    erg = (Beleg.objects.filter(mandant=mandant, art=Belegart.AUFTRAG).filter(Exists(hat_position))
           .exclude(Exists(geliefert) & ~Exists(rueckstand))  # teilweise gelieferte Bestellungen mit Rückstand bleiben offen
           .order_by().aggregate(anzahl=Count("pk"), netto=Coalesce(Sum("summe_netto"), NULL)))
    return {"anzahl": erg["anzahl"], "netto": erg["netto"]}


def offene_angebote(mandant, heute) -> int:
    hat_position = Position.objects.filter(beleg=OuterRef("pk"))
    folge = Beleg.objects.filter(vorgaenger=OuterRef("pk"))
    return (Beleg.objects.filter(mandant=mandant, art=Belegart.ANGEBOT).filter(Q(gueltig_bis__isnull=True) | Q(gueltig_bis__gte=heute))
            .filter(Exists(hat_position)).exclude(Exists(folge)).order_by().count())


def _summe_je_beleg(modell, feld):
    """Summe der Beträge je Beleg als Unterabfrage (keine Abfrage pro Beleg)."""
    return Coalesce(Subquery(modell.objects.filter(**{feld: OuterRef("pk")}).order_by().values(feld)
                             .annotate(s=Sum("betrag")).values("s"), output_field=GELD), NULL)


def offene_posten(mandant, heute) -> dict:
    """Offene Rechnungen (Anzahl, Betrag, überfällig) abzüglich unverrechneter Gutschriften; eine Abfrage je Art,
    nur die offenen Belege (wenige) werden in Python auf Fälligkeit geprüft."""
    bezahlt = _summe_je_beleg(Zahlung, "beleg")
    verrechnet_re = _summe_je_beleg(Gutschriftverrechnung, "rechnung")
    rechnungen = (Beleg.objects.filter(mandant=mandant, art=Belegart.RECHNUNG, abgeschlossen=True)
                  .annotate(rest=F("summe_brutto") - bezahlt - verrechnet_re)
                  .filter(Q(rest__gte=CENT) | Q(rest__lte=-CENT)).order_by()
                  .values_list("rest", "belegdatum", "zahlungsbedingung__tage_netto"))
    anzahl, betrag, ueberfaellig_anzahl, ueberfaellig_betrag = 0, Decimal("0"), 0, Decimal("0")
    for rest, datum, tage in rechnungen:
        anzahl += 1
        betrag += rest
        if rest > 0 and datum + timedelta(days=tage or 0) < heute:
            ueberfaellig_anzahl += 1
            ueberfaellig_betrag += rest
    verrechnet_gs = _summe_je_beleg(Gutschriftverrechnung, "gutschrift")
    gutschriften = (Beleg.objects.filter(mandant=mandant, art=Belegart.GUTSCHRIFT, abgeschlossen=True)
                    .annotate(rest=F("summe_brutto") - verrechnet_gs).filter(rest__gte=CENT).order_by()
                    .values_list("rest", flat=True))
    betrag -= sum(gutschriften, Decimal("0"))
    return {"anzahl": anzahl, "betrag": betrag, "ueberfaellig_anzahl": ueberfaellig_anzahl, "ueberfaellig_betrag": ueberfaellig_betrag}


def lieferanten(mandant, heute) -> dict:
    """Offene Lieferantenbestellungen (noch nicht gelieferter Wert) und offene Lieferantenrechnungen."""
    status = (BestellStatus.BESTELLT, BestellStatus.TEIL)
    anzahl = Bestellung.objects.filter(mandant=mandant, status__in=status).order_by().count()
    rest = BestellPosition.objects.filter(bestellung__mandant=mandant, bestellung__status__in=status).order_by().aggregate(
        s=Coalesce(Sum(ExpressionWrapper(Greatest(F("menge") - F("geliefert"), Value(Decimal("0")), output_field=GELD) * F("ek"), output_field=GELD)), NULL))["s"]
    rechnungen = Lieferantenrechnung.objects.filter(mandant=mandant).exclude(status=RechnungStatus.BEZAHLT).order_by().aggregate(
        anzahl=Count("pk"), brutto=Coalesce(Sum("brutto"), NULL), ueberfaellig=Count("pk", filter=Q(faellig__lt=heute)))
    return {"bestellungen_anzahl": anzahl, "bestellungen_netto": max(rest, Decimal("0")).quantize(CENT),
            "rechnungen_anzahl": rechnungen["anzahl"], "rechnungen_brutto": rechnungen["brutto"],
            "rechnungen_ueberfaellig": rechnungen["ueberfaellig"]}


def kacheln(mandant, tage, heute=None) -> dict:
    heute = heute or timezone.localdate()
    von, bis, vvon, vbis = zeitraeume(heute, tage)
    jetzt, davor = umsatz(mandant, von, bis), umsatz(mandant, vvon, vbis)
    return {"bestellungen": offene_kundenbestellungen(mandant), "posten": offene_posten(mandant, heute),
            "umsatz": {**jetzt, "vorher": davor["netto"], "veraenderung": veraenderung(jetzt["netto"], davor["netto"])},
            "lieferanten": lieferanten(mandant, heute), "von": von, "bis": bis, "vorher_von": vvon, "vorher_bis": vbis}


def verlauf(mandant, tage, heute=None) -> dict:
    """Umsatzverlauf als SVG-Geometrie: Tageswerte (bis 90 Tage), bei 365 Tagen Wochenwerte; der Vorzeitraum läuft
    als zweite Linie über denselben Index. Eine Abfrage (Summe je Tag), das Zusammenfassen geschieht in Python."""
    heute = heute or timezone.localdate()
    von, bis, vvon, vbis = zeitraeume(heute, tage)
    je_tag = {z["belegdatum"]: z["s"] for z in umsatzbelege(mandant, vvon, bis).values("belegdatum")
              .annotate(s=Sum(NETTO_MIT_VORZEICHEN))}
    schritt = 7 if tage > 90 else 1
    werte = []
    for i in range(ceil(tage / schritt)):
        tage_im_block = min(schritt, tage - i * schritt)
        start, vstart = von + timedelta(days=i * schritt), vvon + timedelta(days=i * schritt)
        jetzt = sum((je_tag.get(start + timedelta(days=d), Decimal("0")) for d in range(tage_im_block)), Decimal("0"))
        davor = sum((je_tag.get(vstart + timedelta(days=d), Decimal("0")) for d in range(tage_im_block)), Decimal("0"))
        ende = start + timedelta(days=tage_im_block - 1)
        werte.append({"start": start, "ende": ende, "vstart": vstart, "jetzt": jetzt, "davor": davor})
    breite, hoehe, links, rechts, oben, unten = 720, 240, 64, 14, 14, 30
    nutz_b, nutz_h = breite - links - rechts, hoehe - oben - unten
    alle = [w[k] for w in werte for k in ("jetzt", "davor")]
    ymax, ymin = max(alle + [Decimal("0")]), min(alle + [Decimal("0")])
    spanne = (ymax - ymin) or Decimal("1")

    def x(i):
        return links + (nutz_b * i / (len(werte) - 1) if len(werte) > 1 else nutz_b / 2)

    def y(wert):
        return oben + float((ymax - wert) / spanne) * nutz_h

    def pkt(reihe):
        return " ".join(f"{x(i):.1f},{y(w[reihe]):.1f}" for i, w in enumerate(werte))

    nulllinie = y(Decimal("0"))
    flaeche = f"M{x(0):.1f},{nulllinie:.1f} " + " ".join(f"L{x(i):.1f},{y(w['jetzt']):.1f}" for i, w in enumerate(werte)) \
        + f" L{x(len(werte) - 1):.1f},{nulllinie:.1f} Z"
    punkte = []
    for i, w in enumerate(werte):
        zeitraum = f"{w['start']:%d.%m.%Y}" if w["start"] == w["ende"] else f"{w['start']:%d.%m.}–{w['ende']:%d.%m.%Y}"
        punkte.append({"x": f"{x(i):.1f}", "y": f"{y(w['jetzt']):.1f}", "vy": f"{y(w['davor']):.1f}",
                       "text": f"{zeitraum}: {de_betrag(w['jetzt'])} € (Vorzeitraum {de_betrag(w['davor'])} €)"})
    gitter = [{"y": f"{y(wert):.1f}", "text": de_betrag(wert)} for wert in (ymax, ymin + spanne / 2, ymin)]
    beschriftung = [{"x": f"{x(i):.1f}", "text": f"{werte[i]['start']:%d.%m.}"} for i in sorted({0, len(werte) // 2, len(werte) - 1})]
    return {"breite": breite, "hoehe": hoehe, "links": links, "rechts": breite - rechts, "unten": hoehe - unten + 14,
            "linie": pkt("jetzt"), "vorlinie": pkt("davor"), "flaeche": flaeche, "punkte": punkte, "gitter": gitter,
            "beschriftung": beschriftung, "nulllinie": f"{nulllinie:.1f}", "wochen": schritt == 7,
            "summe": sum((w["jetzt"] for w in werte), Decimal("0"))}


def top_kunden(mandant, von, bis, anzahl=10) -> list:
    return list(umsatzbelege(mandant, von, bis).values("kunde_id", "kunde__name1")
                .annotate(umsatz=Sum(NETTO_MIT_VORZEICHEN)).filter(umsatz__gt=0).order_by("-umsatz")[:anzahl])


def top_artikel(mandant, von, bis, anzahl=10) -> list:
    """Netto je Rechnungsposition (Menge × Preis, nach Positions- und Belegrabatt), Gutschriften werden abgezogen.
    Nur Positionen mit Artikel; Unterpositionen einer Gruppe tragen keinen Preis (er steht an der Gruppenposition).
    Die Summe weicht von den Belegsummen um Rundung und Nebenkosten ab."""
    hundert = Value(Decimal("100"))
    vorzeichen = Case(When(beleg__art=Belegart.GUTSCHRIFT, then=Value(Decimal("-1"))), default=Value(Decimal("1")), output_field=GELD)
    netto = ExpressionWrapper(F("menge") * F("einzelpreis") * (hundert - F("rabatt_prozent")) * (hundert - F("beleg__rabatt_prozent"))
                              * Value(Decimal("0.0001")) * vorzeichen, output_field=GELD)
    return list(Position.objects.filter(beleg__mandant=mandant, beleg__abgeschlossen=True, beleg__art__in=UMSATZ_ARTEN,
                                        beleg__belegdatum__gte=von, beleg__belegdatum__lte=bis,
                                        gruppe__isnull=True, artikel__isnull=False).order_by()
                .values("artikel_id", "artikel__nummer", "artikel__bezeichnung")
                .annotate(umsatz=Sum(netto), stueck=Sum(F("menge") * vorzeichen)).filter(umsatz__gt=0).order_by("-umsatz")[:anzahl])


def top_lieferanten(mandant, von, bis, anzahl=10) -> list:
    """Einkaufsvolumen = netto der Lieferantenrechnungen nach Rechnungsdatum (verlässlicher als Bestellungen,
    die auch nur teilweise geliefert sein können)."""
    return list(Lieferantenrechnung.objects.filter(mandant=mandant, datum__gte=von, datum__lte=bis).order_by()
                .values("lieferant_id", "lieferant__name1").annotate(volumen=Sum("netto")).filter(volumen__gt=0)
                .order_by("-volumen")[:anzahl])


def kanaele(mandant, von, bis) -> list:
    """Umsatz nach Bezugnahme (Shop, eBay, telefonisch …); nur Rechnungen – Gutschriften tragen meist keine Bezugnahme.
    Groß-/Kleinschreibung und Leerzeichen werden zusammengefasst, leer = „ohne Angabe“."""
    summen = {}
    for z in (umsatzbelege(mandant, von, bis).filter(art=Belegart.RECHNUNG).values("bezugnahme").annotate(umsatz=Sum("summe_netto"))):
        name = (z["bezugnahme"] or "").strip()
        schluessel = name.lower()
        alt = summen.get(schluessel, (name or "ohne Angabe", Decimal("0")))
        summen[schluessel] = (alt[0], alt[1] + z["umsatz"])
    positiv = sorted(((n, u) for n, u in summen.values() if u > 0), key=lambda e: -e[1])
    gesamt = sum((u for _, u in positiv), Decimal("0"))
    return [{"name": n, "umsatz": u, "prozent": (u / gesamt * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)} for n, u in positiv]


def arbeitsliste(mandant, von, heute) -> dict:
    from apps.finanzen.services import mahnvorschlag
    reparaturen = Reparatur.objects.filter(mandant=mandant).exclude(status__in=(Status.ABGEHOLT, Status.STORNIERT)).order_by().count()
    hat_offene = Beleg.objects.filter(mandant=mandant, art=Belegart.RECHNUNG, abgeschlossen=True).exists()
    return {"reparaturen": reparaturen,
            "mahnungen": len(mahnvorschlag(mandant, heute)) if hat_offene else 0,  # legt beim ersten Aufruf die Standard-Mahnstufen an
            "shop_bestellungen": ShopBestellung.objects.filter(shop__mandant=mandant, importiert_am__gte=timezone.make_aware(datetime.combine(von, time.min))).count(),  # ohne __date: kein CONVERT_TZ auf MariaDB
            "angebote": offene_angebote(mandant, heute),
            "wiedervorlagen": Wiedervorlage.objects.filter(mandant=mandant, erledigt=False, faellig__lte=heute).count()}
