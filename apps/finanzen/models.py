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
    storno_zu = models.OneToOneField("self", on_delete=models.PROTECT, null=True, blank=True, related_name="storniert_durch",
                                     help_text="Gesetzt bei einer Stornobuchung: die ursprüngliche Zahlung")
    storno_grund = models.CharField("Stornogrund", max_length=300, blank=True)

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


class Bankumsatz(models.Model):
    """Zeile eines importierten Kontoauszugs (CSV oder CAMT.053)."""

    OFFEN, ZUGEORDNET, IGNORIERT = "offen", "zugeordnet", "ignoriert"

    mandant = models.ForeignKey(Mandant, on_delete=models.PROTECT, related_name="bankumsaetze")
    buchungsdatum = models.DateField("Buchungsdatum")
    betrag = models.DecimalField("Betrag", max_digits=12, decimal_places=2)
    name = models.CharField("Auftraggeber/Empfänger", max_length=150, blank=True)
    iban = models.CharField("IBAN", max_length=34, blank=True)
    zweck = models.CharField("Verwendungszweck", max_length=500, blank=True)
    status = models.CharField(max_length=10, default=OFFEN, choices=[
        (OFFEN, "offen"), (ZUGEORDNET, "zugeordnet"), (IGNORIERT, "ignoriert")], db_index=True)
    vorschlag = models.ForeignKey("verkauf.Beleg", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    zahlung = models.OneToOneField(Zahlung, on_delete=models.PROTECT, null=True, blank=True, related_name="bankumsatz")
    pruefsumme = models.CharField(max_length=64, db_index=True)
    importiert_am = models.DateTimeField(default=timezone.now)
    importiert_von = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)

    class Meta:
        verbose_name = "Bankumsatz"
        verbose_name_plural = "Bankumsätze"
        ordering = ["-buchungsdatum", "-id"]
        constraints = [models.UniqueConstraint(fields=["mandant", "pruefsumme"], name="bankumsatz_eindeutig")]

    def __str__(self):
        return f"{self.buchungsdatum:%d.%m.%Y} {self.betrag} € {self.name}"


class Kassenbuch(models.Model):
    """Kassenbuch pro Mandant: fortlaufend nummeriert, unveränderlich (Korrektur per Gegenbuchung).
    TSE-Pflicht beim Barverkauf ist mit dem Steuerberater zu klären (Pflichtenheft Abschnitt 18)."""

    EINNAHME, AUSGABE = "E", "A"

    mandant = models.ForeignKey(Mandant, on_delete=models.PROTECT, related_name="kassenbuch")
    nummer = models.PositiveIntegerField("lfd. Nr.")
    datum = models.DateField("Datum", default=timezone.localdate)
    art = models.CharField("Art", max_length=1, choices=[(EINNAHME, "Einnahme"), (AUSGABE, "Ausgabe")])
    betrag = models.DecimalField("Betrag", max_digits=12, decimal_places=2)
    text = models.CharField("Buchungstext", max_length=200)
    beleg = models.ForeignKey("verkauf.Beleg", on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    erfasst_am = models.DateTimeField(default=timezone.now)
    benutzer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)

    class Meta:
        verbose_name = "Kassenbuch"
        verbose_name_plural = "Kassenbuch"
        ordering = ["-nummer"]
        constraints = [models.UniqueConstraint(fields=["mandant", "nummer"], name="kassenbuch_nummer_eindeutig")]

    @property
    def signierter_betrag(self):
        return self.betrag if self.art == self.EINNAHME else -self.betrag

    def save(self, *args, **kwargs):
        if self.pk:
            raise PermissionError("Kassenbucheinträge sind unveränderlich; bitte eine Gegenbuchung erfassen.")
        if self.betrag <= 0:
            raise ValueError("Betrag muss positiv sein.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise PermissionError("Kassenbucheinträge sind unveränderlich; bitte eine Gegenbuchung erfassen.")

    def __str__(self):
        return f"Kasse {self.nummer}: {self.text}"


class Lastschrift(models.Model):
    """Eine eingezogene Rechnung in einer SEPA-Lastschriftdatei (pain.008). Bei Rücklastschrift wird sie aufgehoben."""

    mandant = models.ForeignKey(Mandant, on_delete=models.PROTECT, related_name="lastschriften")
    beleg = models.ForeignKey("verkauf.Beleg", on_delete=models.PROTECT, related_name="lastschriften")
    betrag = models.DecimalField("Betrag brutto", max_digits=12, decimal_places=2)
    einzugsdatum = models.DateField("Einzugsdatum")
    mandatsreferenz = models.CharField("Mandatsreferenz", max_length=35)
    sequenz = models.CharField("Sequenz", max_length=4, choices=[("FRST", "Erst"), ("RCUR", "Folge")])
    datei = models.CharField("Nachrichten-ID der Datei", max_length=35)
    erstellt_am = models.DateTimeField(default=timezone.now)
    benutzer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    aufgehoben = models.BooleanField("aufgehoben (z. B. Rücklastschrift)", default=False)
    aufgehoben_grund = models.CharField("Grund", max_length=300, blank=True)
    aufgehoben_von = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                       related_name="+", editable=False)
    aufgehoben_am = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta:
        verbose_name = "Lastschrift"
        verbose_name_plural = "Lastschriften"
        ordering = ["-erstellt_am", "-id"]

    def __str__(self):
        return f"{self.beleg.nummer}: {self.betrag} € am {self.einzugsdatum}"
