"""
Lager: Bestandsbewegungen pro Mandant (Pflichtenheft Abschnitt 8, Lager).

Der Bestand ist die Summe aller Bewegungen; Bewegungen sind unveränderlich (Historie mit Datum,
Menge, Grund und Beleg). Reserviert sind Mengen aus Aufträgen, die noch keinen Folgebeleg haben.
"""
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.kern.models import Mandant
from apps.stammdaten.models import Artikel


class Bewegungsart(models.TextChoices):
    ANFANGSBESTAND = "AB", "Anfangsbestand"
    WARENEINGANG = "WE", "Wareneingang"
    VERKAUF = "VK", "Verkauf (Lieferschein/Rechnung)"
    RETOURE = "RT", "Kundenretoure"
    EIGENVERBRAUCH = "EV", "Eigenverbrauch"
    INVENTUR = "IN", "Inventur-Differenz"
    UMBUCHUNG = "UM", "Umbuchung zwischen Mandanten"
    KORREKTUR = "KO", "Korrektur"


class Lagerbewegung(models.Model):
    mandant = models.ForeignKey(Mandant, on_delete=models.PROTECT, related_name="lagerbewegungen")
    artikel = models.ForeignKey(Artikel, on_delete=models.PROTECT, related_name="lagerbewegungen")
    zeitpunkt = models.DateTimeField(default=timezone.now, db_index=True)
    art = models.CharField("Grund", max_length=2, choices=Bewegungsart.choices)
    menge = models.DecimalField("Menge (+ Zugang, − Abgang)", max_digits=10, decimal_places=3)
    beleg = models.ForeignKey("verkauf.Beleg", on_delete=models.PROTECT, null=True, blank=True,
                              related_name="lagerbewegungen")
    bemerkung = models.CharField("Bemerkung", max_length=200, blank=True)
    benutzer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)

    class Meta:
        verbose_name = "Lagerbewegung"
        verbose_name_plural = "Lagerbewegungen"
        ordering = ["-zeitpunkt", "-id"]
        indexes = [models.Index(fields=["mandant", "artikel"])]

    def save(self, *args, **kwargs):
        if self.pk:
            raise PermissionError("Lagerbewegungen sind unveränderlich; bitte eine Gegenbuchung erfassen.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise PermissionError("Lagerbewegungen sind unveränderlich; bitte eine Gegenbuchung erfassen.")

    def __str__(self):
        return f"{self.artikel.nummer} {self.menge:+} ({self.get_art_display()})"


def bestand(mandant, artikel) -> Decimal:
    return Lagerbewegung.objects.filter(mandant=mandant, artikel=artikel).aggregate(
        s=models.Sum("menge"))["s"] or Decimal("0")


def reserviert(mandant, artikel) -> Decimal:
    """Menge in Aufträgen ohne Folgebeleg (Lieferschein/Rechnung noch nicht erstellt)."""
    from apps.verkauf.models import Belegart, Position
    return Position.objects.filter(
        artikel=artikel, beleg__mandant=mandant, beleg__art=Belegart.AUFTRAG, beleg__folgebelege__isnull=True
    ).aggregate(s=models.Sum("menge"))["s"] or Decimal("0")


class Seriennummer(models.Model):
    """Seriennummer eines Artikels vom Wareneingang bis zum Verkauf (und zurück bei Retoure)."""

    IM_LAGER, VERKAUFT = "lager", "verkauft"

    mandant = models.ForeignKey(Mandant, on_delete=models.PROTECT, related_name="seriennummern")
    artikel = models.ForeignKey(Artikel, on_delete=models.PROTECT, related_name="seriennummern")
    nummer = models.CharField("Seriennummer", max_length=100, db_index=True)
    status = models.CharField(max_length=8, default=IM_LAGER, choices=[(IM_LAGER, "im Lager"), (VERKAUFT, "verkauft")])
    eingang = models.DateTimeField("Wareneingang", default=timezone.now)
    eingang_bemerkung = models.CharField(max_length=200, blank=True)
    position = models.ForeignKey("verkauf.Position", on_delete=models.SET_NULL, null=True, blank=True,
                                 related_name="seriennummern")

    class Meta:
        verbose_name = "Seriennummer"
        verbose_name_plural = "Seriennummern"
        ordering = ["artikel", "nummer"]
        constraints = [models.UniqueConstraint(fields=["mandant", "artikel", "nummer"], name="seriennummer_eindeutig")]

    def __str__(self):
        return f"{self.artikel.nummer} SN {self.nummer}"
