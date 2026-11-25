"""Artikel (alle Typen), Kunden und Lieferanten dürfen nur gelöscht werden, wenn sie nirgends verwendet sind (Wunsch des Nutzers
25.11.2026). Geprüft werden alle Verweise aus anderen Tabellen – auch künftige, weil die Prüfung über die Modellbeziehungen läuft.
Nicht als Verwendung zählen nur die eigenen Bestandteile des Datensatzes (z. B. Bilder des Artikels, Anschriften des Kunden)."""
from django.db.models import ProtectedError
from django.db.models.signals import pre_delete
from django.dispatch import receiver

# (Tabelle, Feld) der eigenen Bestandteile: werden mit dem Datensatz gelöscht und zählen nicht als Verwendung
EIGENE_TEILE = {
    "stammdaten.Artikel": {("stammdaten.ArtikelMandant", "artikel"), ("stammdaten.ArtikelBild", "artikel"),
                           ("stammdaten.ArtikelLieferant", "artikel"), ("stammdaten.RadarGesehen", "artikel"),
                           ("stammdaten.GruppenKomponente", "gruppe")},
    "stammdaten.Kunde": {("stammdaten.KundeMandant", "kunde"), ("stammdaten.Kundenanschrift", "kunde"),
                         ("stammdaten.Ansprechpartner", "kunde"), ("stammdaten.Kundenpreis", "kunde"),
                         ("stammdaten.UstIdPruefung", "kunde")},
    "stammdaten.Lieferant": {("stammdaten.LieferantMandant", "lieferant"), ("stammdaten.Ansprechpartner", "lieferant"),
                             ("stammdaten.UstIdPruefung", "lieferant")},
}


def _basis(obj) -> str:
    return obj._meta.concrete_model._meta.label


def geprueft(obj) -> bool:
    return _basis(obj) in EIGENE_TEILE


def verwendungen(obj) -> list:
    """[(Bezeichnung der Tabelle, Anzahl)] aller Verweise auf den Datensatz außer seinen eigenen Bestandteilen."""
    basis = _basis(obj)
    eigene = EIGENE_TEILE.get(basis, set())
    treffer = []
    for rel in obj._meta.concrete_model._meta.related_objects:
        modell = rel.related_model
        if (modell._meta.label, rel.field.name) in eigene:
            continue
        anzahl = modell._base_manager.filter(**{rel.field.name: obj.pk}).count()
        if anzahl:
            treffer.append((str(modell._meta.verbose_name_plural), anzahl))
    return treffer


def text(obj, treffer) -> str:
    teile = ", ".join(f"{name} ({anzahl})" for name, anzahl in treffer)
    return f"„{obj}“ wird noch verwendet und kann nicht gelöscht werden: {teile}. Stattdessen auf „nicht aktiv“ bzw. gesperrt setzen."


@receiver(pre_delete)
def _nicht_loeschen_wenn_verwendet(sender, instance, **kwargs):
    """Letzte Sicherung bei jedem Löschen (auch Sammellöschung in der Verwaltung)."""
    if geprueft(instance):
        treffer = verwendungen(instance)
        if treffer:
            raise ProtectedError(text(instance, treffer), set())
