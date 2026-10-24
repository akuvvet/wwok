"""
Verkauf: Belege (Angebot -> Auftrag -> Lieferschein -> Rechnung, Gutschrift) - Pflichtenheft Abschnitt 5.

Belege und Nummernkreise sind pro Mandant getrennt. Abgeschlossene Rechnungen und
Gutschriften sind gesperrt (GoBD); Korrektur nur per Storno/Gutschrift.
"""
from decimal import ROUND_HALF_UP, Decimal

from django.db import models, transaction
from django.utils import timezone

from apps.kern.models import Mandant, Zeitstempel
from apps.stammdaten.models import Ansprechpartner, Artikel, Artikeltyp, Kunde, Zahlungsbedingung

CENT = Decimal("0.01")


def runden(wert: Decimal) -> Decimal:
    return wert.quantize(CENT, rounding=ROUND_HALF_UP)


class Belegart(models.TextChoices):
    ANGEBOT = "AN", "Angebot"
    AUFTRAG = "AB", "Auftrag"
    LIEFERSCHEIN = "LS", "Lieferschein"
    RECHNUNG = "RE", "Rechnung"
    GUTSCHRIFT = "GS", "Gutschrift"


# Nächste Stufe im Belegfluss ("per Klick")
FOLGEBELEG = {
    Belegart.ANGEBOT: Belegart.AUFTRAG,
    Belegart.AUFTRAG: Belegart.LIEFERSCHEIN,
    Belegart.LIEFERSCHEIN: Belegart.RECHNUNG,
}
# Diese Belegarten werden erst beim Abschluss nummeriert und danach gesperrt.
GESPERRT_NACH_ABSCHLUSS = {Belegart.RECHNUNG, Belegart.GUTSCHRIFT}


class BelegGesperrt(Exception):
    pass


class Nummernkreis(models.Model):
    mandant = models.ForeignKey(Mandant, on_delete=models.PROTECT)
    art = models.CharField("Belegart", max_length=2, choices=Belegart.choices)
    jahr = models.PositiveSmallIntegerField("Jahr")
    letzte_nummer = models.PositiveIntegerField("zuletzt vergeben", default=0)

    class Meta:
        verbose_name = "Nummernkreis"
        verbose_name_plural = "Nummernkreise"
        constraints = [models.UniqueConstraint(fields=["mandant", "art", "jahr"], name="nummernkreis_eindeutig")]

    @classmethod
    def naechste(cls, mandant: Mandant, art: str, jahr: int) -> str:
        with transaction.atomic():
            kreis, _ = cls.objects.select_for_update().get_or_create(mandant=mandant, art=art, jahr=jahr)
            kreis.letzte_nummer += 1
            kreis.save(update_fields=["letzte_nummer"])
        return f"{art}{jahr}-{kreis.letzte_nummer:05d}"

    def __str__(self):
        return f"{self.mandant} {self.art} {self.jahr}: {self.letzte_nummer}"


class Beleg(Zeitstempel):
    mandant = models.ForeignKey(Mandant, on_delete=models.PROTECT, related_name="belege")
    art = models.CharField("Belegart", max_length=2, choices=Belegart.choices)
    nummer = models.CharField("Belegnummer", max_length=20, null=True, blank=True, default=None)
    kunde = models.ForeignKey(Kunde, on_delete=models.PROTECT, related_name="belege")
    ansprechpartner = models.ForeignKey(Ansprechpartner, on_delete=models.SET_NULL, null=True, blank=True)
    belegdatum = models.DateField("Belegdatum", default=timezone.localdate)
    zahlungsbedingung = models.ForeignKey(Zahlungsbedingung, on_delete=models.PROTECT, null=True, blank=True)
    rechnungsanschrift = models.TextField("Rechnungsanschrift", blank=True)
    versandanschrift = models.TextField("Versandanschrift", blank=True)
    versandart = models.CharField("Versandart", max_length=100, blank=True)
    versandkosten = models.DecimalField("Versandkosten netto", max_digits=12, decimal_places=2, default=Decimal("0"))
    bemerkung = models.TextField("Bemerkung", blank=True)
    vorgaenger = models.ForeignKey("self", on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="folgebelege")
    abgeschlossen = models.BooleanField("abgeschlossen", default=False, editable=False)
    summe_netto = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"), editable=False)
    summe_steuer = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"), editable=False)
    summe_brutto = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"), editable=False)

    class Meta:
        verbose_name = "Beleg"
        verbose_name_plural = "Belege"
        ordering = ["-belegdatum", "-id"]
        # Entwürfe haben keine Nummer (NULL) und zählen nicht als Duplikat
        constraints = [models.UniqueConstraint(fields=["mandant", "art", "nummer"], name="belegnummer_eindeutig")]

    def __str__(self):
        return f"{self.get_art_display()} {self.nummer or '(Entwurf)'}"

    @property
    def gesperrt(self) -> bool:
        return self.abgeschlossen and self.art in GESPERRT_NACH_ABSCHLUSS

    @property
    def folgeart(self):
        return FOLGEBELEG.get(self.art)

    @property
    def folgeart_name(self):
        return Belegart(self.folgeart).label if self.folgeart else ""

    def save(self, *args, **kwargs):
        if self.pk and self.gesperrt:
            vorher = Beleg.objects.get(pk=self.pk)
            # nur Versandart und Zahlungsbedingung bleiben änderbar
            for feld in ("kunde_id", "belegdatum", "versandkosten", "summe_netto", "summe_brutto", "art", "nummer"):
                if getattr(vorher, feld) != getattr(self, feld):
                    raise BelegGesperrt(f"{self}: Feld '{feld}' ist nach Abschluss gesperrt.")
        self.nummer = self.nummer or None
        if not self.pk and not self.nummer and self.art not in GESPERRT_NACH_ABSCHLUSS:
            self.nummer = Nummernkreis.naechste(self.mandant, self.art, self.belegdatum.year)
        super().save(*args, **kwargs)

    def berechne(self):
        """Summen aus Positionen; Versandkosten laufen mit dem höchsten Steuersatz der Positionen."""
        if self.gesperrt:
            raise BelegGesperrt(f"{self} ist gesperrt.")
        netto = Decimal("0")
        saetze = {}
        for p in self.positionen.all():
            n = p.netto_gesamt
            netto += n
            saetze[p.steuer_prozent] = saetze.get(p.steuer_prozent, Decimal("0")) + n
        if self.versandkosten:
            satz = max(saetze, default=Decimal("19"))
            saetze[satz] = saetze.get(satz, Decimal("0")) + self.versandkosten
            netto += self.versandkosten
        steuer = sum((runden(basis * satz / 100) for satz, basis in saetze.items()), Decimal("0"))
        self.summe_netto, self.summe_steuer = runden(netto), steuer
        self.summe_brutto = self.summe_netto + steuer
        Beleg.objects.filter(pk=self.pk).update(
            summe_netto=self.summe_netto, summe_steuer=self.summe_steuer, summe_brutto=self.summe_brutto)

    @transaction.atomic
    def abschliessen(self):
        if self.abgeschlossen:
            return
        if not self.positionen.exists():
            raise ValueError("Beleg ohne Positionen kann nicht abgeschlossen werden.")
        self.berechne()
        if not self.nummer:
            self.nummer = Nummernkreis.naechste(self.mandant, self.art, self.belegdatum.year)
        self.abgeschlossen = True
        Beleg.objects.filter(pk=self.pk).update(nummer=self.nummer, abgeschlossen=True)

    @transaction.atomic
    def folgebeleg_erzeugen(self) -> "Beleg":
        art = self.folgeart
        if not art:
            raise ValueError("Für diese Belegart gibt es keinen Folgebeleg.")
        neu = Beleg.objects.create(
            mandant=self.mandant, art=art, kunde=self.kunde, ansprechpartner=self.ansprechpartner,
            zahlungsbedingung=self.zahlungsbedingung, rechnungsanschrift=self.rechnungsanschrift,
            versandanschrift=self.versandanschrift, versandart=self.versandart,
            versandkosten=self.versandkosten, bemerkung=self.bemerkung, vorgaenger=self)
        for p in self.positionen.all():
            Position.objects.create(
                beleg=neu, typ=p.typ, artikel=p.artikel, bezeichnung=p.bezeichnung, menge=p.menge,
                einzelpreis=p.einzelpreis, rabatt_prozent=p.rabatt_prozent, steuer_prozent=p.steuer_prozent,
                zusatztext=p.zusatztext)
        neu.berechne()
        return neu


class Position(Zeitstempel):
    beleg = models.ForeignKey(Beleg, on_delete=models.CASCADE, related_name="positionen")
    pos = models.PositiveSmallIntegerField("Pos.", default=0)
    typ = models.CharField("Typ", max_length=1, choices=Artikeltyp.choices, default=Artikeltyp.A)
    artikel = models.ForeignKey(Artikel, on_delete=models.PROTECT, null=True, blank=True)
    bezeichnung = models.CharField("Bezeichnung", max_length=255)
    zusatztext = models.TextField("Zusatztext", blank=True)
    menge = models.DecimalField("Menge", max_digits=10, decimal_places=3, default=Decimal("1"))
    einzelpreis = models.DecimalField("Einzelpreis netto", max_digits=12, decimal_places=2)
    rabatt_prozent = models.DecimalField("Rabatt %", max_digits=5, decimal_places=2, default=Decimal("0"))
    steuer_prozent = models.DecimalField("Steuer %", max_digits=5, decimal_places=2, default=Decimal("19"))

    class Meta:
        verbose_name = "Position"
        verbose_name_plural = "Positionen"
        ordering = ["pos", "id"]

    @property
    def netto_gesamt(self) -> Decimal:
        return runden(self.menge * self.einzelpreis * (Decimal("100") - self.rabatt_prozent) / 100)

    def save(self, *args, **kwargs):
        if self.beleg.gesperrt:
            raise BelegGesperrt(f"{self.beleg} ist gesperrt.")
        if not self.pk and not self.pos:
            letzte = Position.objects.filter(beleg=self.beleg).aggregate(m=models.Max("pos"))["m"] or 0
            self.pos = letzte + 1
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.beleg.gesperrt:
            raise BelegGesperrt(f"{self.beleg} ist gesperrt.")
        super().delete(*args, **kwargs)
