"""Günstigsten Lieferanten-EK je Herstellernummer in die eigenen Artikel (Typ A) übernehmen.

Die Verknüpfung Preislistenartikel ↔ Artikel läuft über die Herstellernummer (beim Preislisten-Import als „Lieferant zum Artikel“).
Hat ein Artikel mehrere Lieferanten, gewinnt der niedrigste EK netto. Der Import-EK ist maßgebend, auch nach einem Wareneingang
(README 18)."""
from apps.stammdaten.models import Artikel, ArtikelLieferant, EkHerkunft


def bestpreise(anwenden=False):
    best = {}
    for al in ArtikelLieferant.objects.filter(ek__gt=0, artikel__typ="A").select_related("lieferant", "artikel").order_by("artikel_id", "ek"):
        best.setdefault(al.artikel_id, al)  # nach EK sortiert: der erste je Artikel ist der günstigste
    aenderungen, unveraendert = [], 0
    for al in best.values():
        a = al.artikel
        if a.durchschnitts_ek == al.ek and a.ek_herkunft == EkHerkunft.IMPORT:
            unveraendert += 1
        else:
            aenderungen.append({"artikel": a, "alt": a.durchschnitts_ek, "neu": al.ek, "lieferant": al.lieferant.kurz})
    if anwenden and aenderungen:
        for z in aenderungen:
            z["artikel"].durchschnitts_ek = z["neu"]
            z["artikel"].ek_herkunft_setzen(EkHerkunft.IMPORT)  # überschreibt einen manuellen EK und einen alten Wareneingangs-EK
        Artikel.objects.bulk_update([z["artikel"] for z in aenderungen], ["durchschnitts_ek", "ek_herkunft", "ek_geaendert_am"], batch_size=500)
    return {"artikel": len(best), "aenderungen": aenderungen, "unveraendert": unveraendert}


def geplanter_lauf(preislisten_importiert=False, jetzt=None):
    """Führt die Übernahme aus, wenn der Zeitplan (Verwaltung → Zeitplan) es verlangt. Gibt die Meldung oder None zurück."""
    from django.utils import timezone
    from .models import Bestpreisplan
    plan = Bestpreisplan.holen()
    if not plan.faellig(jetzt, preislisten_importiert):
        return None
    ergebnis = bestpreise(anwenden=True)
    plan.letzter_lauf = jetzt or timezone.now()
    plan.letzte_meldung = f"{len(ergebnis['aenderungen'])} Artikel aktualisiert, {ergebnis['unveraendert']} unverändert"
    plan.save(update_fields=["letzter_lauf", "letzte_meldung", "geaendert_am"])
    return plan.letzte_meldung
