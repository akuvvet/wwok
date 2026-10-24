"""
Werkstatt (Pflichtenheft Abschnitt 11): Geräteannahme, Status mit Statusmail, Teile und Arbeitszeit,
Rechnung oder Garantie, Übergang in die Lieferanten-RMA.
"""
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.kern.models import Mandant, Zeitstempel
from apps.stammdaten.models import Artikel, Kunde


class Status(models.TextChoices):
    ANGENOMMEN = "angenommen", "angenommen"
    IN_REPARATUR = "in_reparatur", "in Reparatur"
    WARTET = "wartet", "wartet auf Teile/Rückmeldung"
    FERTIG = "fertig", "fertig – abholbereit"
    ABGEHOLT = "abgeholt", "abgeholt/ausgeliefert"
    RMA = "rma", "in Lieferanten-RMA"
    STORNIERT = "storniert", "storniert"


STATUS_MIT_MAIL = {Status.ANGENOMMEN, Status.IN_REPARATUR, Status.FERTIG}


class Reparatur(Zeitstempel):
    mandant = models.ForeignKey(Mandant, on_delete=models.PROTECT, related_name="reparaturen")
    nummer = models.CharField("Auftragsnummer", max_length=20, db_index=True)
    kunde = models.ForeignKey(Kunde, on_delete=models.PROTECT, related_name="reparaturen")
    geraet = models.CharField("Gerät", max_length=200)
    artikel = models.ForeignKey(Artikel, on_delete=models.SET_NULL, null=True, blank=True)
    seriennummer = models.CharField("Seriennummer", max_length=100, blank=True, db_index=True)
    fehler = models.TextField("Fehlerbeschreibung")
    zubehoer = models.CharField("mitgegebenes Zubehör", max_length=200, blank=True)
    status = models.CharField("Status", max_length=12, choices=Status.choices, default=Status.ANGENOMMEN, db_index=True)
    garantie = models.BooleanField("Garantiefall (keine Rechnung)", default=False)
    angenommen_am = models.DateField("angenommen am", default=timezone.localdate)
    fertig_am = models.DateField("fertig am", null=True, blank=True)
    abgeholt_am = models.DateField("abgeholt am", null=True, blank=True)
    interne_notiz = models.TextField("interne Notiz", blank=True)
    rechnung = models.ForeignKey("verkauf.Beleg", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    teile_gebucht = models.BooleanField("Teile aus Lager gebucht (Garantie)", default=False, editable=False)
    angenommen_von = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                       related_name="+")

    class Meta:
        verbose_name = "Reparatur"
        verbose_name_plural = "Reparaturen"
        ordering = ["-angenommen_am", "-id"]
        constraints = [models.UniqueConstraint(fields=["mandant", "nummer"], name="reparatur_nummer_eindeutig")]

    def __str__(self):
        return f"{self.nummer} {self.geraet}"

    @property
    def summe_netto(self) -> Decimal:
        return sum((p.netto for p in self.positionen.all()), Decimal("0"))


class ReparaturPosition(Zeitstempel):
    TEIL, ARBEIT = "T", "A"

    reparatur = models.ForeignKey(Reparatur, on_delete=models.CASCADE, related_name="positionen")
    art = models.CharField("Art", max_length=1, choices=[(TEIL, "Teil"), (ARBEIT, "Arbeitszeit")])
    artikel = models.ForeignKey(Artikel, on_delete=models.PROTECT, null=True, blank=True)
    bezeichnung = models.CharField("Bezeichnung", max_length=255)
    menge = models.DecimalField("Menge / Stunden", max_digits=10, decimal_places=3, default=Decimal("1"))
    einzelpreis = models.DecimalField("Preis netto", max_digits=12, decimal_places=2)
    steuer_prozent = models.DecimalField("Steuer %", max_digits=5, decimal_places=2, default=Decimal("19"))

    class Meta:
        verbose_name = "Reparaturposition"
        verbose_name_plural = "Reparaturpositionen"
        ordering = ["id"]

    @property
    def netto(self) -> Decimal:
        return (self.menge * self.einzelpreis).quantize(Decimal("0.01"))

    def __str__(self):
        return self.bezeichnung
