"""
Verkauf: Belege (Angebot -> Auftrag -> Lieferschein -> Rechnung, Gutschrift) - Pflichtenheft Abschnitt 5.

Belege und Nummernkreise sind pro Mandant getrennt. Abgeschlossene Rechnungen und
Gutschriften sind gesperrt (GoBD); Korrektur nur per Storno/Gutschrift.
"""
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
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
    verpackung = models.DecimalField("Verpackung netto", max_digits=12, decimal_places=2, default=Decimal("0"))
    versicherung = models.DecimalField("Versicherung netto", max_digits=12, decimal_places=2, default=Decimal("0"))
    referenz = models.CharField("Ihre Referenz / Bestellnummer", max_length=100, blank=True)
    vortext = models.TextField("Text vor den Positionen", blank=True)
    schlusstext = models.TextField("Text nach den Positionen", blank=True)
    bemerkung = models.TextField("Bemerkung (intern, wird nicht gedruckt)", blank=True)
    vorgaenger = models.ForeignKey("self", on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="folgebelege")
    lieferscheine = models.ManyToManyField("self", symmetrical=False, blank=True, related_name="rechnungen",
                                           help_text="Bei Sammelrechnungen: alle Lieferscheine, die abgerechnet werden")
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

    def loeschhindernis(self):
        """Grund, warum der Beleg nicht gelöscht werden darf (None = löschbar).
        Löschbar sind Angebote, Kundenbestellungen (Aufträge) und noch nicht nummerierte Rechnungs-/Gutschriftentwürfe."""
        if self.art not in (Belegart.ANGEBOT, Belegart.AUFTRAG) and not (
                self.art in GESPERRT_NACH_ABSCHLUSS and not self.abgeschlossen and not self.nummer):
            return f"{self.get_art_display()} kann nicht gelöscht werden – Korrektur nur per Storno/Gutschrift."
        if self.folgebelege.exists() or self.rechnungen.exists():
            return "Es gibt Folgebelege (z. B. Lieferschein oder Rechnung) – bitte zuerst diese entfernen."
        if self.mails.exists():
            return "Der Beleg wurde per E-Mail versendet; der Versandnachweis muss erhalten bleiben."
        if self.zahlungen.exists():
            return "Zum Beleg gibt es Zahlungen (Anzahlung) – bitte zuerst stornieren."
        if self.lagerbewegungen.exists():
            return "Zum Beleg gibt es Lagerbuchungen."
        return None

    def positionen_pruefen(self):
        """Ein Beleg ohne Position darf nicht abgeschlossen, versendet oder weitergeführt werden."""
        if not self.positionen.exists():
            raise ValueError(f"{self} hat keine Position – erst Positionen erfassen.")

    @property
    def abrechenbar(self) -> bool:
        """Abgeschlossener Lieferschein, der noch in keine Rechnung (und keinen anderen Folgebeleg) übernommen wurde."""
        return (self.art == Belegart.LIEFERSCHEIN and self.abgeschlossen
                and not self.folgebelege.exists() and not self.rechnungen.exists())

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
            for feld in ("kunde_id", "belegdatum", "versandkosten", "verpackung", "versicherung", "summe_netto", "summe_brutto", "art", "nummer"):
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
        nebenkosten = self.versandkosten + self.verpackung + self.versicherung
        if nebenkosten:
            satz = max(saetze, default=Decimal("19"))
            saetze[satz] = saetze.get(satz, Decimal("0")) + nebenkosten
            netto += nebenkosten
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
        from apps.lager.services import buche_beleg  # Lieferschein/Rechnung buchen den Bestand ab
        from apps.kern.middleware import aktueller_benutzer
        benutzer = aktueller_benutzer()
        buche_beleg(self, benutzer if benutzer is not None and benutzer.is_authenticated else None)

    @transaction.atomic
    def folgebeleg_erzeugen(self) -> "Beleg":
        art = self.folgeart
        if not art:
            raise ValueError("Für diese Belegart gibt es keinen Folgebeleg.")
        self.positionen_pruefen()
        neu = Beleg.objects.create(
            mandant=self.mandant, art=art, kunde=self.kunde, ansprechpartner=self.ansprechpartner,
            zahlungsbedingung=self.zahlungsbedingung, rechnungsanschrift=self.rechnungsanschrift,
            versandanschrift=self.versandanschrift, versandart=self.versandart,
            versandkosten=self.versandkosten, verpackung=self.verpackung, versicherung=self.versicherung,
            referenz=self.referenz, vortext=self.vortext, schlusstext=self.schlusstext,
            bemerkung=self.bemerkung, vorgaenger=self)
        kopiere_positionen(self, neu)
        if self.art == Belegart.LIEFERSCHEIN:
            neu.lieferscheine.add(self)
        neu.berechne()
        return neu


    @transaction.atomic
    def gutschrift_erzeugen(self) -> "Beleg":
        """Storno/Gutschrift zu einer abgeschlossenen Rechnung (Positionen unverändert, Belegart Gutschrift)."""
        if self.art != Belegart.RECHNUNG or not self.abgeschlossen:
            raise ValueError("Eine Gutschrift gibt es nur zu einer abgeschlossenen Rechnung.")
        neu = Beleg.objects.create(
            mandant=self.mandant, art=Belegart.GUTSCHRIFT, kunde=self.kunde, ansprechpartner=self.ansprechpartner,
            zahlungsbedingung=self.zahlungsbedingung, rechnungsanschrift=self.rechnungsanschrift,
            versandanschrift=self.versandanschrift, versandart=self.versandart,
            versandkosten=self.versandkosten, verpackung=self.verpackung, versicherung=self.versicherung,
            referenz=self.referenz, bemerkung=f"Gutschrift zu Rechnung {self.nummer}", vorgaenger=self)
        kopiere_positionen(self, neu)
        neu.berechne()
        return neu

    @property
    def faellig_am(self):
        """Belegdatum + Tage der Zahlungsbedingung (Rechnung fällig)."""
        if not self.zahlungsbedingung_id:
            return None
        return self.belegdatum + timedelta(days=self.zahlungsbedingung.tage_netto)

    @property
    def zahlungstext(self) -> str:
        """Text der Zahlungsbedingung; ff.ff.ffff wird durch das Fälligkeitsdatum ersetzt."""
        zb = self.zahlungsbedingung
        if not zb:
            return ""
        return (zb.druck_text or zb.bezeichnung).replace("ff.ff.ffff", self.faellig_am.strftime("%d.%m.%Y"))


class Mailvorlage(Zeitstempel):
    """Betreff und Text je Mandant und Belegart. Platzhalter: {anrede} {art} {nummer} {firma} {kunde} {datum}
    {faelligkeit} {zahlungstext} {betrag}"""

    mandant = models.ForeignKey(Mandant, on_delete=models.PROTECT)
    art = models.CharField("Belegart", max_length=2, choices=Belegart.choices)
    betreff = models.CharField("Betreff", max_length=200)
    text = models.TextField("Text")

    class Meta:
        verbose_name = "Mailvorlage"
        verbose_name_plural = "Mailvorlagen"
        constraints = [models.UniqueConstraint(fields=["mandant", "art"], name="mailvorlage_eindeutig")]

    def __str__(self):
        return f"{self.mandant} – {self.get_art_display()}"


class MailVersand(models.Model):
    """Unveränderlicher Nachweis, wann welcher Beleg an wen verschickt wurde."""

    beleg = models.ForeignKey(Beleg, on_delete=models.PROTECT, related_name="mails", null=True, blank=True)
    mandant = models.ForeignKey(Mandant, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    bezug = models.CharField("Bezug", max_length=100, blank=True, help_text="z. B. Reparatur oder Bestellung")
    zeitpunkt = models.DateTimeField(default=timezone.now, db_index=True)
    benutzer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    an = models.CharField("An", max_length=500)
    betreff = models.CharField("Betreff", max_length=200)
    text = models.TextField("Text")
    erfolgreich = models.BooleanField(default=False)
    fehler = models.CharField(max_length=500, blank=True)

    class Meta:
        verbose_name = "Mailversand"
        verbose_name_plural = "Mailversand"
        ordering = ["-zeitpunkt"]

    def save(self, *args, **kwargs):
        if self.pk:
            raise PermissionError("Versandprotokoll ist unveränderlich.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise PermissionError("Versandprotokoll ist unveränderlich.")

    def __str__(self):
        return f"{self.beleg or self.bezug} → {self.an} ({'ok' if self.erfolgreich else 'Fehler'})"


def kopiere_positionen(quelle, ziel):
    """Kopiert alle Positionen samt Gruppenzuordnung in einen anderen Beleg. Stammt die Quelle aus einem
    Lieferschein, bleibt der Lieferschein an der Position vermerkt (Nummer und Datum im Ausdruck)."""
    neu = {}
    lieferschein = quelle if quelle.art == Belegart.LIEFERSCHEIN else None
    for p in quelle.positionen.order_by("pos", "id"):
        neu[p.pk] = Position.objects.create(
            beleg=ziel, gruppe=neu.get(p.gruppe_id), typ=p.typ, artikel=p.artikel, bezeichnung=p.bezeichnung,
            menge=p.menge, einzelpreis=p.einzelpreis, rabatt_prozent=p.rabatt_prozent,
            steuer_prozent=p.steuer_prozent, zusatztext=p.zusatztext, lieferschein=lieferschein or p.lieferschein,
            quellposition=p)


@transaction.atomic
def rechnung_aus_lieferscheinen(lieferscheine) -> Beleg:
    """Mehrere Lieferscheine eines Kunden zu einer Rechnung zusammenfassen."""
    lieferscheine = sorted(lieferscheine, key=lambda b: (b.belegdatum, b.pk))
    if not lieferscheine:
        raise ValueError("Keine Lieferscheine ausgewählt.")
    erster = lieferscheine[0]
    for ls in lieferscheine:
        if ls.mandant_id != erster.mandant_id or ls.kunde_id != erster.kunde_id:
            raise ValueError("Alle Lieferscheine müssen zum selben Kunden und Mandanten gehören.")
        if not ls.abrechenbar:
            raise ValueError(f"{ls} ist nicht abrechenbar (nicht abgeschlossen oder schon berechnet).")
    rechnung = Beleg.objects.create(
        mandant=erster.mandant, art=Belegart.RECHNUNG, kunde=erster.kunde, ansprechpartner=erster.ansprechpartner,
        zahlungsbedingung=erster.zahlungsbedingung or erster.kunde.zahlungsbedingung,
        rechnungsanschrift=erster.rechnungsanschrift, versandanschrift=erster.versandanschrift,
        versandart=erster.versandart, vorgaenger=erster,
        bemerkung="Lieferscheine: " + ", ".join(ls.nummer for ls in lieferscheine))
    for ls in lieferscheine:
        kopiere_positionen(ls, rechnung)
    rechnung.lieferscheine.set(lieferscheine)
    rechnung.versandkosten = sum((ls.versandkosten for ls in lieferscheine), Decimal("0"))
    rechnung.verpackung = sum((ls.verpackung for ls in lieferscheine), Decimal("0"))
    rechnung.versicherung = sum((ls.versicherung for ls in lieferscheine), Decimal("0"))
    rechnung.save()
    rechnung.berechne()
    return rechnung


class Position(Zeitstempel):
    beleg = models.ForeignKey(Beleg, on_delete=models.CASCADE, related_name="positionen")
    lieferschein = models.ForeignKey(Beleg, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
                                     help_text="Lieferschein, aus dem die Position übernommen wurde")
    quellposition = models.ForeignKey("self", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    gruppe = models.ForeignKey("self", on_delete=models.CASCADE, null=True, blank=True, related_name="unterpositionen",
                               help_text="Gesetzt bei Unterpositionen einer Gruppe (Typ G); sie haben keinen eigenen Preis.")
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
        if self.gruppe_id:  # Unterartikel einer Gruppe: der Preis steht an der Gruppenposition
            return Decimal("0.00")
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
