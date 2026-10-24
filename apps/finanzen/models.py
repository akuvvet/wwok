"""
Finanzen: Zahlungen auf Rechnungen (offene Posten), Mahnstufen und Mahnungen - Pflichtenheft Abschnitt 8.

Offen = Rechnungsbetrag - Zahlungen - abgeschlossene Gutschriften zur Rechnung.
Zahlungen sind unveränderlich; ein Fehler wird mit einer Gegenbuchung (negativer Betrag) korrigiert.
"""
from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.kern.models import Mandant


class Zahlart(models.TextChoices):
    UEBERWEISUNG = "ue", "Überweisung"
    LASTSCHRIFT = "ls", "Lastschrift"
    BAR = "ba", "Bar"
    PAYPAL = "pp", "PayPal"
    KARTE = "ka", "Karte"
    SONSTIGE = "so", "Sonstige"


class Zahlung(models.Model):
    mandant = models.ForeignKey(Mandant, on_delete=models.PROTECT, related_name="zahlungen")
    beleg = models.ForeignKey("verkauf.Beleg", on_delete=models.PROTECT, related_name="zahlungen",
                              help_text="Rechnung (oder Auftrag bei Anzahlung)")
    datum = models.DateField("Zahlungsdatum", default=timezone.localdate)
    betrag = models.DecimalField("Betrag brutto", max_digits=12, decimal_places=2,
                                 help_text="Negativ = Gegenbuchung/Rückzahlung")
    zahlart = models.CharField("Zahlart", max_length=2, choices=Zahlart.choices, default=Zahlart.UEBERWEISUNG)
    bemerkung = models.CharField("Bemerkung", max_length=200, blank=True)
    erfasst_am = models.DateTimeField(default=timezone.now)
    benutzer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)

    class Meta:
        verbose_name = "Zahlung"
        verbose_name_plural = "Zahlungen"
        ordering = ["-datum", "-id"]

    def save(self, *args, **kwargs):
        if self.pk:
            raise PermissionError("Zahlungen sind unveränderlich; bitte eine Gegenbuchung erfassen.")
        if self.beleg.mandant_id != self.mandant_id:
            raise ValueError("Beleg gehört zu einem anderen Mandanten.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise PermissionError("Zahlungen sind unveränderlich; bitte eine Gegenbuchung erfassen.")

    def __str__(self):
        return f"{self.beleg}: {self.betrag} € am {self.datum:%d.%m.%Y}"


class Mahnstufe(models.Model):
    """Einstellbar pro Mandant. Stufe 1: Zahlungserinnerung, 2: 1. Mahnung, 3: 2. Mahnung."""

    mandant = models.ForeignKey(Mandant, on_delete=models.CASCADE, related_name="mahnstufen")
    stufe = models.PositiveSmallIntegerField("Stufe")
    bezeichnung = models.CharField("Bezeichnung", max_length=60)
    tage_seit_vorheriger = models.PositiveSmallIntegerField(
        "Tage seit vorheriger Stufe", default=7,
        help_text="Stufe 1: ignoriert, sie gilt nach Ablauf der Mahnfrist der Zahlungsbedingung.")
    zahlungsfrist_tage = models.PositiveSmallIntegerField("neue Zahlungsfrist (Tage)", default=7)
    gebuehr = models.DecimalField("Mahngebühr", max_digits=8, decimal_places=2, default=0)
    text = models.TextField(
        "Text", blank=True,
        help_text="Platzhalter: {anrede} {nummer} {datum} {faelligkeit} {offen} {gebuehr} {gesamt} {frist} {firma}")

    class Meta:
        verbose_name = "Mahnstufe"
        verbose_name_plural = "Mahnstufen"
        ordering = ["mandant", "stufe"]
        constraints = [models.UniqueConstraint(fields=["mandant", "stufe"], name="mahnstufe_eindeutig")]

    def __str__(self):
        return f"{self.mandant}: {self.stufe} – {self.bezeichnung}"


class Mahnung(models.Model):
    beleg = models.ForeignKey("verkauf.Beleg", on_delete=models.PROTECT, related_name="mahnungen")
    stufe = models.PositiveSmallIntegerField()
    bezeichnung = models.CharField(max_length=60)
    datum = models.DateField(default=timezone.localdate)
    offener_betrag = models.DecimalField(max_digits=12, decimal_places=2)
    gebuehr = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    frist = models.DateField("zahlbar bis")
    benutzer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)

    class Meta:
        verbose_name = "Mahnung"
        verbose_name_plural = "Mahnungen"
        ordering = ["-datum", "-id"]
        constraints = [models.UniqueConstraint(fields=["beleg", "stufe"], name="mahnung_je_stufe_einmal")]

    def __str__(self):
        return f"{self.bezeichnung} zu {self.beleg}"
