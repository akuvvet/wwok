"""
Einkauf (Pflichtenheft Abschnitt 6): Lieferantenbestellung, Wareneingang, Lieferantenrechnung,
Rücksendung/RMA. Alles pro Mandant getrennt.
"""
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.kern.models import Mandant, Zeitstempel
from apps.stammdaten.models import Artikel, Lieferant

EK = {"max_digits": 12, "decimal_places": 4}
GELD = {"max_digits": 12, "decimal_places": 2}


class BestellStatus(models.TextChoices):
    ENTWURF = "entwurf", "Entwurf"
    BESTELLT = "bestellt", "bestellt"
    TEIL = "teil", "teilweise geliefert"
    GELIEFERT = "geliefert", "vollständig geliefert"
    STORNIERT = "storniert", "storniert"


class Bestellung(Zeitstempel):
    mandant = models.ForeignKey(Mandant, on_delete=models.PROTECT, related_name="bestellungen")
    lieferant = models.ForeignKey(Lieferant, on_delete=models.PROTECT, related_name="bestellungen")
    nummer = models.CharField("Bestellnummer", max_length=20, db_index=True)
    datum = models.DateField("Datum", default=timezone.localdate)
    status = models.CharField(max_length=10, choices=BestellStatus.choices, default=BestellStatus.ENTWURF, db_index=True)
    bemerkung = models.TextField("Bemerkung", blank=True)
    aus_beleg = models.ForeignKey("verkauf.Beleg", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
                                  help_text="Angebot/Auftrag, aus dem die Bestellung kopiert wurde")

    class Meta:
        verbose_name = "Lieferantenbestellung"
        verbose_name_plural = "Lieferantenbestellungen"
        ordering = ["-datum", "-id"]
        constraints = [models.UniqueConstraint(fields=["mandant", "nummer"], name="bestellnummer_eindeutig")]

    def __str__(self):
        return f"Bestellung {self.nummer}"

    @property
    def summe_netto(self) -> Decimal:
        return sum((p.netto for p in self.positionen.all()), Decimal("0"))

    def status_aktualisieren(self):
        """Nach Wareneingängen: teilweise oder vollständig geliefert."""
        if self.status in (BestellStatus.ENTWURF, BestellStatus.STORNIERT):
            return
        positionen = list(self.positionen.all())
        if positionen and all(p.offen <= 0 for p in positionen):
            self.status = BestellStatus.GELIEFERT
        elif any(p.geliefert > 0 for p in positionen):
            self.status = BestellStatus.TEIL
        else:
            self.status = BestellStatus.BESTELLT
        self.save(update_fields=["status"])


class BestellPosition(models.Model):
    bestellung = models.ForeignKey(Bestellung, on_delete=models.CASCADE, related_name="positionen")
    artikel = models.ForeignKey(Artikel, on_delete=models.PROTECT)
    bestellnummer = models.CharField("Bestellnr. des Lieferanten", max_length=100, blank=True)
    bezeichnung = models.CharField(max_length=255)
    menge = models.DecimalField(max_digits=10, decimal_places=3)
    ek = models.DecimalField("EK netto", **EK)
    geliefert = models.DecimalField(max_digits=10, decimal_places=3, default=Decimal("0"))

    class Meta:
        verbose_name = "Bestellposition"
        verbose_name_plural = "Bestellpositionen"
        ordering = ["id"]

    @property
    def offen(self) -> Decimal:
        return self.menge - self.geliefert

    @property
    def netto(self) -> Decimal:
        return (self.menge * self.ek).quantize(Decimal("0.01"))

    def __str__(self):
        return f"{self.menge:f} × {self.artikel.nummer}"


class Wareneingang(Zeitstempel):
    mandant = models.ForeignKey(Mandant, on_delete=models.PROTECT, related_name="wareneingaenge")
    lieferant = models.ForeignKey(Lieferant, on_delete=models.PROTECT, related_name="wareneingaenge")
    bestellung = models.ForeignKey(Bestellung, on_delete=models.SET_NULL, null=True, blank=True, related_name="wareneingaenge")
    nummer = models.CharField("Wareneingang-Nr.", max_length=20, db_index=True)
    datum = models.DateField(default=timezone.localdate)
    lieferschein = models.CharField("Lieferschein des Lieferanten", max_length=60, blank=True)
    gebucht = models.BooleanField(default=False, editable=False)
    benutzer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")

    class Meta:
        verbose_name = "Wareneingang"
        verbose_name_plural = "Wareneingänge"
        ordering = ["-datum", "-id"]
        constraints = [models.UniqueConstraint(fields=["mandant", "nummer"], name="wareneingang_nummer_eindeutig")]

    def __str__(self):
        return f"Wareneingang {self.nummer}"

    @property
    def summe_netto(self) -> Decimal:
        return sum((p.netto for p in self.positionen.all()), Decimal("0"))


class WareneingangPosition(models.Model):
    wareneingang = models.ForeignKey(Wareneingang, on_delete=models.CASCADE, related_name="positionen")
    bestellposition = models.ForeignKey(BestellPosition, on_delete=models.SET_NULL, null=True, blank=True, related_name="eingaenge")
    artikel = models.ForeignKey(Artikel, on_delete=models.PROTECT)
    menge = models.DecimalField(max_digits=10, decimal_places=3)
    ek = models.DecimalField("EK netto", **EK)
    seriennummern = models.TextField("Seriennummern (eine pro Zeile)", blank=True)

    class Meta:
        verbose_name = "Wareneingangsposition"
        verbose_name_plural = "Wareneingangspositionen"
        ordering = ["id"]

    @property
    def netto(self) -> Decimal:
        return (self.menge * self.ek).quantize(Decimal("0.01"))


class RechnungStatus(models.TextChoices):
    OFFEN = "offen", "offen"
    EXPORTIERT = "exportiert", "zur Zahlung exportiert"
    BEZAHLT = "bezahlt", "bezahlt"


class Lieferantenrechnung(Zeitstempel):
    mandant = models.ForeignKey(Mandant, on_delete=models.PROTECT, related_name="lieferantenrechnungen")
    lieferant = models.ForeignKey(Lieferant, on_delete=models.PROTECT, related_name="rechnungen")
    rechnungsnummer = models.CharField("Rechnungsnummer des Lieferanten", max_length=60)
    datum = models.DateField("Rechnungsdatum")
    faellig = models.DateField("fällig am")
    netto = models.DecimalField(**GELD)
    steuer = models.DecimalField(**GELD, default=Decimal("0"))
    brutto = models.DecimalField(**GELD)
    skonto_prozent = models.DecimalField("Skonto %", max_digits=5, decimal_places=2, default=Decimal("0"))
    skonto_bis = models.DateField("Skonto bis", null=True, blank=True)
    wareneingaenge = models.ManyToManyField(Wareneingang, blank=True, related_name="rechnungen")
    status = models.CharField(max_length=10, choices=RechnungStatus.choices, default=RechnungStatus.OFFEN, db_index=True)
    bezahlt_am = models.DateField(null=True, blank=True)
    bezahlter_betrag = models.DecimalField(**GELD, null=True, blank=True)
    historie = models.TextField("Änderungshistorie", blank=True, help_text="Stornos der Zahlung mit Grund, Benutzer und Zeitpunkt")
    quelle = models.CharField(max_length=20, default="manuell")
    bemerkung = models.CharField(max_length=200, blank=True)

    class Meta:
        verbose_name = "Lieferantenrechnung"
        verbose_name_plural = "Lieferantenrechnungen"
        ordering = ["faellig", "id"]
        constraints = [models.UniqueConstraint(fields=["mandant", "lieferant", "rechnungsnummer"],
                                               name="lieferantenrechnung_eindeutig")]

    def __str__(self):
        return f"{self.lieferant.name1} {self.rechnungsnummer}"

    def zahlung_stornieren(self, grund, benutzer=None):
        """Zahlungsausgang rückgängig machen: Rechnung ist wieder offen; Grund und Benutzer bleiben in der Historie."""
        grund = (grund or "").strip()
        if not grund:
            raise ValueError("Bitte einen Stornogrund angeben.")
        if self.status != RechnungStatus.BEZAHLT:
            raise ValueError("Die Rechnung ist nicht als bezahlt gebucht.")
        zeile = (f"{timezone.localtime():%d.%m.%Y %H:%M} {benutzer or '-'}: Zahlung vom "
                 f"{self.bezahlt_am:%d.%m.%Y} über {self.bezahlter_betrag} € storniert – {grund}")
        self.historie = (self.historie + chr(10) + zeile).strip()
        self.status, self.bezahlt_am, self.bezahlter_betrag = RechnungStatus.OFFEN, None, None
        self.save()

    def zahlbetrag(self, am=None) -> Decimal:
        """Betrag für die Überweisung; Skonto nur, wenn die Skontofrist am Zahlungstag noch läuft."""
        am = am or timezone.localdate()
        if self.skonto_prozent and self.skonto_bis and am <= self.skonto_bis:
            return (self.brutto * (Decimal("100") - self.skonto_prozent) / 100).quantize(Decimal("0.01"))
        return self.brutto

    def abgleich(self) -> dict:
        """Netto der verknüpften Wareneingänge gegen Rechnung: Differenz > 1 Cent ist eine Abweichung."""
        wareneingaenge = list(self.wareneingaenge.all())
        if not wareneingaenge:
            return {"verknuepft": False, "differenz": None, "ok": None}
        soll = sum((w.summe_netto for w in wareneingaenge), Decimal("0"))
        differenz = self.netto - soll
        return {"verknuepft": True, "wareneingang_netto": soll, "differenz": differenz, "ok": abs(differenz) <= Decimal("0.01")}


class RmaStatus(models.TextChoices):
    ANGELEGT = "angelegt", "angelegt"
    EINGESENDET = "eingesendet", "an Lieferanten eingesendet"
    ERSATZ = "ersatz", "Ersatz erhalten"
    GUTSCHRIFT = "gutschrift", "Gutschrift erhalten"
    ABGELEHNT = "abgelehnt", "abgelehnt/zurück"
    ABGESCHLOSSEN = "abgeschlossen", "abgeschlossen"


class Rma(Zeitstempel):
    """Rücksendung/Garantiefall beim Lieferanten; optional aus einem Werkstattauftrag."""

    mandant = models.ForeignKey(Mandant, on_delete=models.PROTECT, related_name="rmas")
    lieferant = models.ForeignKey(Lieferant, on_delete=models.PROTECT, related_name="rmas")
    nummer = models.CharField("RMA-Nummer (intern)", max_length=20, db_index=True)
    lieferanten_rma_nr = models.CharField("RMA-Nr. des Lieferanten", max_length=60, blank=True)
    artikel = models.ForeignKey(Artikel, on_delete=models.PROTECT, null=True, blank=True)
    bezeichnung = models.CharField(max_length=255)
    seriennummer = models.CharField(max_length=100, blank=True)
    fehler = models.TextField("Fehlerbeschreibung")
    status = models.CharField(max_length=14, choices=RmaStatus.choices, default=RmaStatus.ANGELEGT, db_index=True)
    eingesendet_am = models.DateField(null=True, blank=True)
    erledigt_am = models.DateField(null=True, blank=True)
    gutschrift_betrag = models.DecimalField(**GELD, null=True, blank=True)
    reparatur = models.ForeignKey("werkstatt.Reparatur", on_delete=models.SET_NULL, null=True, blank=True, related_name="rmas")
    notiz = models.TextField(blank=True)

    class Meta:
        verbose_name = "RMA"
        verbose_name_plural = "RMA-Fälle"
        ordering = ["-id"]
        constraints = [models.UniqueConstraint(fields=["mandant", "nummer"], name="rma_nummer_eindeutig")]

    def __str__(self):
        return f"{self.nummer} {self.bezeichnung}"
