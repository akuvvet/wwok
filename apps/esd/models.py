"""
ESD-Key-Verwaltung (Pflichtenheft Abschnitt 10): Keys importieren, Bestand je Mandant, automatische Zuordnung
beim Verkauf, Volumenkeys mit maximaler Vergabeanzahl, Schutz vor doppelter Vergabe an denselben Kunden,
Entscheidung bei Retoure (Key freigeben oder sperren).
"""
from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.kern.models import Mandant
from apps.stammdaten.models import Artikel, Kunde


class KeyStatus(models.TextChoices):
    AKTIV = "aktiv", "verfügbar/in Vergabe"
    GESPERRT = "gesperrt", "gesperrt"


class ESDKey(models.Model):
    mandant = models.ForeignKey(Mandant, on_delete=models.PROTECT, related_name="esd_keys")
    artikel = models.ForeignKey(Artikel, on_delete=models.PROTECT, related_name="esd_keys")
    key = models.CharField("Key", max_length=250)
    max_vergaben = models.PositiveIntegerField("max. Vergaben", default=1, help_text="1 = Einzelkey, größer 1 = Volumenkey")
    vergeben = models.PositiveIntegerField("bereits vergeben", default=0, editable=False)
    status = models.CharField(max_length=8, choices=KeyStatus.choices, default=KeyStatus.AKTIV, db_index=True)
    importiert_am = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name = "ESD-Key"
        verbose_name_plural = "ESD-Keys"
        ordering = ["artikel", "id"]
        constraints = [models.UniqueConstraint(fields=["mandant", "artikel", "key"], name="esdkey_eindeutig")]

    @property
    def frei(self) -> int:
        return 0 if self.status == KeyStatus.GESPERRT else max(self.max_vergaben - self.vergeben, 0)

    @property
    def erschoepft(self) -> bool:
        return self.status == KeyStatus.AKTIV and self.vergeben >= self.max_vergaben

    def __str__(self):
        return f"{self.artikel.nummer}: {self.key}"


class VergabeStatus(models.TextChoices):
    AKTIV = "aktiv", "vergeben"
    FREIGEGEBEN = "freigegeben", "zurück – Key freigegeben"
    GESPERRT = "gesperrt", "zurück – Key gesperrt"


class ESDVergabe(models.Model):
    key = models.ForeignKey(ESDKey, on_delete=models.PROTECT, related_name="vergaben")
    kunde = models.ForeignKey(Kunde, on_delete=models.PROTECT, related_name="esd_vergaben")
    beleg = models.ForeignKey("verkauf.Beleg", on_delete=models.PROTECT, related_name="esd_vergaben")
    position = models.ForeignKey("verkauf.Position", on_delete=models.SET_NULL, null=True, blank=True, related_name="esd_vergaben")
    datum = models.DateTimeField(default=timezone.now)
    status = models.CharField(max_length=12, choices=VergabeStatus.choices, default=VergabeStatus.AKTIV, db_index=True)
    bemerkung = models.CharField(max_length=300, blank=True, help_text="z. B. erneute Vergabe an denselben Kunden: Begründung")
    benutzer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)

    class Meta:
        verbose_name = "ESD-Vergabe"
        verbose_name_plural = "ESD-Vergaben"
        ordering = ["-datum"]

    def __str__(self):
        return f"{self.key} → {self.kunde.nummer}"
