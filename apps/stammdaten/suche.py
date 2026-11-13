"""Gemeinsame Suchsyntax der Artikelsuche (Belegmaske, Tabellensuche, Nachfolger):

- Wörter: jedes Wort muss irgendwo vorkommen (Nummer, Bezeichnung, Herstellernummer, Hersteller, EAN …), Reihenfolge egal
- ``*`` allein: alle Artikel
- ``>5``: Lagerbestand des Mandanten größer als 5 (auch ``> 5``, Komma oder Punkt als Dezimaltrenner)
- ``%`` wie SQL-LIKE: ``Netz%`` beginnt mit „Netz“, ``%750W%`` enthält „750W“, ``%Gold`` endet mit „Gold“
  (gilt für Nummer, Bezeichnung und Herstellernummer; ohne ``%`` am Rand ist der Text dort verankert)
"""
import re
from decimal import Decimal, InvalidOperation

from django.db.models import DecimalField, OuterRef, Q, Subquery, Sum, Value
from django.db.models.functions import Coalesce

BESTAND = re.compile(r">\s*(\d+(?:[.,]\d+)?)")


def zerlegen(q: str):
    """(Wörter, LIKE-Muster, Mindestbestand) aus dem Suchtext; Sterne werden ignoriert."""
    q = q or ""
    mindest = None
    for treffer in BESTAND.findall(q):
        try:
            wert = Decimal(treffer.replace(",", "."))
        except InvalidOperation:
            continue
        mindest = wert if mindest is None else max(mindest, wert)
    rest = BESTAND.sub(" ", q).replace("*", " ")
    woerter, muster = [], []
    for wort in rest.split():
        (muster if "%" in wort else woerter).append(wort)
    return woerter, muster, mindest


def like_regex(muster: str) -> str:
    """SQL-LIKE (nur %) als regulärer Ausdruck, am Anfang und Ende verankert."""
    return "^" + ".*".join(re.escape(teil) for teil in muster.split("%")) + "$"


def mit_bestand(qs, mandant):
    """Annotiert ``lagerbestand`` (Summe der Lagerbewegungen des Mandanten, 0 ohne Bewegung)."""
    from apps.lager.models import Lagerbewegung
    summe = (Lagerbewegung.objects.filter(mandant=mandant, artikel=OuterRef("pk")).order_by()
             .values("artikel").annotate(s=Sum("menge")).values("s"))
    return qs.annotate(lagerbestand=Coalesce(Subquery(summe, output_field=DecimalField(max_digits=14, decimal_places=3)),
                                             Value(Decimal("0")), output_field=DecimalField(max_digits=14, decimal_places=3)))


def artikel_filtern(qs, q: str, mandant, wort_felder=("nummer", "bezeichnung", "herstellernummer", "hersteller__name", "ean"),
                    like_felder=("nummer", "bezeichnung", "herstellernummer")):
    """Wendet die Suchsyntax auf einen Artikel-QuerySet an."""
    woerter, muster, mindest = zerlegen(q)
    for wort in woerter:
        bedingung = Q()
        for feld in wort_felder:
            bedingung |= Q(**{f"{feld}__icontains": wort})
        qs = qs.filter(bedingung)
    for m in muster:
        regex, bedingung = like_regex(m), Q()
        for feld in like_felder:
            bedingung |= Q(**{f"{feld}__iregex": regex})
        qs = qs.filter(bedingung)
    if mindest is not None:
        qs = mit_bestand(qs, mandant).filter(lagerbestand__gt=mindest)
    return qs


def preisliste_filtern(qs, q: str):
    """Dieselbe Syntax für Preislistenartikel (Typ P). Ein Bestandsfilter (>) schließt sie aus: P-Artikel liegen nicht im eigenen Lager."""
    woerter, muster, mindest = zerlegen(q)
    if mindest is not None:
        return qs.none()
    for wort in woerter:
        qs = qs.filter(Q(bezeichnung__icontains=wort) | Q(herstellernummer__icontains=wort) | Q(bestellnummer__icontains=wort)
                       | Q(hersteller__icontains=wort) | Q(ean__icontains=wort))
    for m in muster:
        regex = like_regex(m)
        qs = qs.filter(Q(bezeichnung__iregex=regex) | Q(herstellernummer__iregex=regex) | Q(bestellnummer__iregex=regex))
    return qs


def nur_alle(q: str) -> bool:
    """Suchtext ohne Bedingung (nur Sterne): alle Artikel."""
    return not any(zerlegen(q)[:2]) and zerlegen(q)[2] is None
