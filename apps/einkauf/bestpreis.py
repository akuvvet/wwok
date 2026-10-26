"""Günstigsten Lieferanten-EK je Herstellernummer in die eigenen Artikel (Typ A) übernehmen.

Die Verknüpfung Preislistenartikel ↔ Artikel läuft über die Herstellernummer (beim Preislisten-Import als „Lieferant zum Artikel“).
Hat ein Artikel mehrere Lieferanten, gewinnt der niedrigste EK netto. Artikel mit gebuchtem Wareneingang behalten ihren
Durchschnitts-EK (Wareneingang hat Vorrang)."""
from apps.lager.models import Bewegungsart, Lagerbewegung
from apps.stammdaten.models import Artikel, ArtikelLieferant


def bestpreise(anwenden=False):
    mit_wareneingang = set(Lagerbewegung.objects.filter(art=Bewegungsart.WARENEINGANG).values_list("artikel_id", flat=True).distinct())
    best = {}
    for al in ArtikelLieferant.objects.filter(ek__gt=0, artikel__typ="A").select_related("lieferant", "artikel").order_by("artikel_id", "ek"):
        best.setdefault(al.artikel_id, al)  # nach EK sortiert: der erste je Artikel ist der günstigste
    aenderungen, unveraendert, uebersprungen = [], 0, 0
    for artikel_id, al in best.items():
        a = al.artikel
        if artikel_id in mit_wareneingang:
            uebersprungen += 1
        elif a.durchschnitts_ek == al.ek:
            unveraendert += 1
        else:
            aenderungen.append({"artikel": a, "alt": a.durchschnitts_ek, "neu": al.ek, "lieferant": al.lieferant.kurz})
    if anwenden and aenderungen:
        for z in aenderungen:
            z["artikel"].durchschnitts_ek = z["neu"]
        Artikel.objects.bulk_update([z["artikel"] for z in aenderungen], ["durchschnitts_ek"], batch_size=500)
    return {"artikel": len(best), "aenderungen": aenderungen, "unveraendert": unveraendert, "uebersprungen": uebersprungen}
