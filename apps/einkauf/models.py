"""
Einkauf (Pflichtenheft Abschnitt 6): Lieferantenbestellung, Wareneingang, Lieferantenrechnung,
Rücksendung/RMA. Alles pro Mandant getrennt.
"""
import datetime
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.kern.models import Mandant, Zeitstempel
from apps.stammdaten.models import Artikel, Lieferant

EK = {"max_digits": 12, "decimal_places": 4}
GELD = {"max_digits": 12, "decimal_places": 2}


class BestellStatus(models.TextChoices):
    ENTWURF = "entwurf", "nicht abgeschlossen"
    BESTELLT = "bestellt", "bestellt"
    TEIL = "teil", "teilweise geliefert"
    GELIEFERT = "geliefert", "vollständig geliefert"
    STORNIERT = "storniert", "storniert"


class Bestellung(Zeitstempel):
    mandant = models.ForeignKey(Mandant, on_delete=models.PROTECT, related_name="bestellungen")
    lieferant = models.ForeignKey(Lieferant, on_delete=models.PROTECT, related_name="bestellungen")
    nummer = models.CharField("Bestellnummer", max_length=20, db_index=True, null=True, blank=True,
                              help_text="Leer, solange die Bestellung vorläufig (nicht gespeichert) ist; die Nummer vergibt „Speichern: abschließen & Nummer vergeben“")
    vorlaeufig = models.BooleanField(
        "vorläufig (nicht gespeichert)", default=False, db_index=True, editable=False,
        help_text="Mit „+ Bestellung“ oder aus einem Kundenbeleg angelegt und noch nicht mit „Speichern: abschließen & Nummer vergeben“ gespeichert: "
                  "ohne Nummer; wird verworfen, wenn sie nicht abgeschlossen wird.")
    zuletzt_aktiv = models.DateTimeField("zuletzt aktiv", null=True, blank=True, editable=False,
                                         help_text="Vorläufige Bestellung: letzte Aktivität; ohne Lebenszeichen der geöffneten Seite wird verworfen.")
    datum = models.DateField("Datum", default=timezone.localdate)
    status = models.CharField(max_length=20, choices=BestellStatus.choices, default=BestellStatus.ENTWURF, db_index=True)
    bemerkung = models.TextField("Bemerkung", blank=True)
    aus_beleg = models.ForeignKey("verkauf.Beleg", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
                                  help_text="Angebot oder Kundenbestellung, aus dem die Lieferantenbestellung kopiert wurde")
    angelegt_von = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name="+", verbose_name="angelegt von", editable=False)
    # Ansprechpartner beim Lieferanten (Wunsch des Nutzers 20.11.2026): beim Anlegen der im Lieferantenkopf gewählte, sonst
    # Hauptkontakt bzw. der erste; im Tab „Versand & Konditionen“ änderbar, steht auf dem PDF und ist Empfänger der Mail.
    ansprechpartner = models.ForeignKey("stammdaten.Ansprechpartner", on_delete=models.SET_NULL, null=True, blank=True,
                                        related_name="+", verbose_name="Ansprechpartner")
    # Tab „Versand & Konditionen“ (Wunsch des Nutzers 18.11.2026, wie die Kundenbestellung): erwartete Nebenkosten gehen in den
    # ersten Wareneingang zur Bestellung (dort änderbar); Liefertermin, Bezugnahme und Versandart stehen auf dem PDF.
    liefertermin = models.DateField("Liefertermin", null=True, blank=True)
    bezugnahme = models.CharField("Bezugnahme (z. B. Angebot des Lieferanten)", max_length=100, blank=True)
    versandart = models.CharField("Versandart", max_length=60, blank=True)
    versandkosten = models.DecimalField("Versandkosten netto", **GELD, default=Decimal("0"))
    verpackung = models.DecimalField("Verpackung netto", **GELD, default=Decimal("0"))
    versicherung = models.DecimalField("Versicherung netto", **GELD, default=Decimal("0"))
    notiz = models.TextField("Interne Notiz", blank=True, help_text="Nur intern, nicht auf PDF und E-Mail")

    class Meta:
        verbose_name = "Lieferantenbestellung"
        verbose_name_plural = "Lieferantenbestellungen"
        ordering = ["-datum", "-id"]
        constraints = [models.UniqueConstraint(fields=["mandant", "nummer"], name="bestellnummer_eindeutig")]

    def __str__(self):
        return f"Bestellung {self.nummer_text}"

    @property
    def nummer_text(self) -> str:
        """Nummer für die Anzeige; eine vorläufige Bestellung hat noch keine."""
        return self.nummer or "(nicht gespeichert)"

    @property
    def summe_netto(self) -> Decimal:
        return sum((p.netto for p in self.positionen.all()), Decimal("0"))

    @property
    def nebenkosten(self) -> Decimal:
        return self.versandkosten + self.verpackung + self.versicherung

    def alle_wareneingaenge(self):
        """Wareneingänge zu dieser Bestellung: mit ihr als Bezug oder mit übernommenen Positionen (Tab „Bestellungen“ im
        Wareneingang kann Positionen mehrerer Bestellungen eines Lieferanten zusammenfassen)."""
        return Wareneingang.objects.filter(models.Q(bestellung=self) | models.Q(positionen__bestellposition__bestellung=self)).distinct()

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
    artikel = models.ForeignKey(Artikel, on_delete=models.PROTECT, null=True, blank=True)
    preisliste_artikel = models.ForeignKey("stammdaten.Preislistenartikel", on_delete=models.SET_NULL, null=True, blank=True,
                                           related_name="+", help_text="Artikel aus der Lieferantenpreisliste; wird beim Wareneingang zum eigenen Artikel")
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
    def nummer(self) -> str:
        if self.artikel_id:
            return self.artikel.nummer
        return self.preisliste_artikel.nummer if self.preisliste_artikel_id else ""

    @property
    def offen(self) -> Decimal:
        return self.menge - self.geliefert

    @property
    def netto(self) -> Decimal:
        return (self.menge * self.ek).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    def __str__(self):
        return f"{self.menge:f} × {self.nummer}"


class Wareneingang(Zeitstempel):
    mandant = models.ForeignKey(Mandant, on_delete=models.PROTECT, related_name="wareneingaenge")
    lieferant = models.ForeignKey(Lieferant, on_delete=models.PROTECT, related_name="wareneingaenge")
    bestellung = models.ForeignKey(Bestellung, on_delete=models.SET_NULL, null=True, blank=True, related_name="wareneingaenge")
    nummer = models.CharField("Wareneingang-Nr.", max_length=20, db_index=True)
    datum = models.DateField(default=timezone.localdate)
    lieferschein = models.CharField("Lieferschein des Lieferanten", max_length=60, blank=True)
    versandkosten = models.DecimalField("Versandkosten netto", max_digits=10, decimal_places=2, default=Decimal("0"))
    versicherung = models.DecimalField("Versicherungskosten netto", max_digits=10, decimal_places=2, default=Decimal("0"))
    verpackung = models.DecimalField("Verpackungskosten netto", max_digits=10, decimal_places=2, default=Decimal("0"))
    nebenkosten_extern = models.BooleanField("Nebenkosten auf eigener Rechnung", default=False,
                                             help_text="z. B. Spedition bei Auslandsware: zählen im Kalkulations-EK, nicht im Abgleich mit der Warenrechnung")
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

    @property
    def nebenkosten(self) -> Decimal:
        return self.versandkosten + self.versicherung + self.verpackung

    @property
    def gesamt_netto(self) -> Decimal:
        return self.summe_netto + self.nebenkosten

    @property
    def rechnungswert(self) -> Decimal:
        """Was auf der Rechnung des Lieferanten stehen muss."""
        return self.summe_netto if self.nebenkosten_extern else self.gesamt_netto

    def kalkulation(self) -> dict:
        """Kalkulations-EK je Position: EK zuzüglich Nebenkosten, verteilt nach Warenwert (ohne Warenwert nach Menge)."""
        positionen = list(self.positionen.all())
        wert = sum((p.menge * p.ek for p in positionen), Decimal("0"))
        menge = sum((p.menge for p in positionen), Decimal("0"))
        neben = self.nebenkosten
        ergebnis = {}
        for p in positionen:
            if not neben:
                kalk = p.ek
            elif wert > 0:
                kalk = p.ek * (wert + neben) / wert
            else:
                kalk = neben / menge if menge else p.ek
            ergebnis[p.pk] = kalk.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
        return ergebnis


class WareneingangPosition(models.Model):
    wareneingang = models.ForeignKey(Wareneingang, on_delete=models.CASCADE, related_name="positionen")
    bestellposition = models.ForeignKey(BestellPosition, on_delete=models.SET_NULL, null=True, blank=True, related_name="eingaenge")
    artikel = models.ForeignKey(Artikel, on_delete=models.PROTECT)
    menge = models.DecimalField(max_digits=10, decimal_places=3)
    ek = models.DecimalField("EK netto", **EK)
    kalk_ek = models.DecimalField("Kalkulations-EK", null=True, blank=True, editable=False, help_text="EK inkl. anteiliger Nebenkosten, beim Buchen festgeschrieben", **EK)
    seriennummern = models.TextField("Seriennummern (eine pro Zeile)", blank=True)
    esd_keys = models.TextField("ESD-Keys (eine pro Zeile)", blank=True)

    class Meta:
        verbose_name = "Wareneingangsposition"
        verbose_name_plural = "Wareneingangspositionen"
        ordering = ["id"]

    @property
    def netto(self) -> Decimal:
        return (self.menge * self.ek).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


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
    bemerkung = models.CharField("Notiz", max_length=200, blank=True)
    abweichung_bestaetigt = models.DecimalField(
        "bestätigte Abweichung", **GELD, null=True, blank=True, editable=False,
        help_text="Differenz zum Wareneingang, die ein Benutzer bewusst bestätigt hat; ändert sich die Differenz, gilt die Bestätigung nicht mehr.")

    class Meta:
        verbose_name = "Lieferantenrechnung"
        verbose_name_plural = "Lieferantenrechnungen"
        ordering = ["faellig", "id"]
        constraints = [models.UniqueConstraint(fields=["mandant", "lieferant", "rechnungsnummer"],
                                               name="lieferantenrechnung_eindeutig")]

    def __str__(self):
        return f"{self.lieferant.name1} {self.rechnungsnummer}"

    def save(self, *args, **kwargs):
        if not self._state.adding:  # sonst entstünde eine inzwischen gelöschte Rechnung beim Speichern neu
            if not type(self).objects.filter(pk=self.pk).exists():
                raise ValueError("Die Rechnung wurde inzwischen gelöscht.")
            kwargs["force_update"] = True
        super().save(*args, **kwargs)

    def zahlung_stornieren(self, grund, benutzer=None):
        """Zahlungsausgang rückgängig machen: Rechnung ist wieder offen; Grund und Benutzer bleiben in der Historie."""
        grund = (grund or "").strip()
        if not grund:
            raise ValueError("Bitte einen Stornogrund angeben.")
        if self.status != RechnungStatus.BEZAHLT:
            raise ValueError("Die Rechnung ist nicht als bezahlt gebucht.")
        if self.durch_verrechnung_bezahlt:
            raise ValueError("Die Rechnung ist durch Verrechnung mit einer Gutschrift bezahlt – bitte die Verrechnung rückgängig machen.")
        zeile =(f"{timezone.localtime():%d.%m.%Y %H:%M} {benutzer or '-'}: Zahlung vom "
                 f"{self.bezahlt_am:%d.%m.%Y} über {self.bezahlter_betrag} € storniert – {grund}")
        self.historie = (self.historie + chr(10) + zeile).strip()
        self.status, self.bezahlt_am, self.bezahlter_betrag = RechnungStatus.OFFEN, None, None
        self.save()

    @property
    def verrechnet(self) -> Decimal:
        """Mit Gutschriften des Lieferanten verrechnet (ohne rückgängig gemachte)."""
        if self.pk is None:
            return Decimal("0")
        return sum((v.betrag for v in self.verrechnungen.all() if not v.aufgehoben), Decimal("0"))

    @property
    def offener_betrag(self) -> Decimal:
        """Brutto minus Verrechnungen (ohne Skonto): bis zu diesem Betrag lässt sich noch verrechnen."""
        return max(self.brutto - self.verrechnet, Decimal("0"))

    @property
    def durch_verrechnung_bezahlt(self) -> bool:
        """Bezahlt allein durch Verrechnung (kein Zahlungsausgang gebucht: Betrag 0)."""
        return self.status == RechnungStatus.BEZAHLT and self.bezahlter_betrag == 0

    def zahlbetrag(self, am=None) -> Decimal:
        """Betrag für die Überweisung; Skonto nur, wenn die Skontofrist am Zahlungstag noch läuft. Verrechnungen mit
        Gutschriften des Lieferanten mindern ihn (nie unter 0); das Skonto bezieht sich auf das Rechnungsbrutto."""
        am = am or timezone.localdate()
        if self.skonto_prozent and self.skonto_bis and am <= self.skonto_bis:
            basis = (self.brutto * (Decimal("100") - self.skonto_prozent) / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        else:
            basis = self.brutto
        return max(basis - self.verrechnet, Decimal("0"))

    def abgleich(self) -> dict:
        """Netto der verknüpften Wareneingänge gegen Rechnung, Nebenkosten mitgerechnet, außer sie kommen auf eigener Rechnung:
        Differenz > 1 Cent ist eine Abweichung."""
        wareneingaenge = list(self.wareneingaenge.all())
        if not wareneingaenge:
            return {"verknuepft": False, "differenz": None, "ok": None, "bestaetigt": False, "offen": False}
        soll = sum((w.rechnungswert for w in wareneingaenge), Decimal("0"))
        differenz = self.netto - soll
        ok = abs(differenz) <= Decimal("0.01")
        bestaetigt = not ok and self.abweichung_bestaetigt is not None and self.abweichung_bestaetigt == differenz
        return {"verknuepft": True, "wareneingang_netto": soll, "differenz": differenz, "ok": ok,
                "bestaetigt": bestaetigt, "offen": not ok and not bestaetigt}

    def freigabe_pruefen(self):
        """Zahlung oder SEPA-Export nur ohne offene Abweichung: erst korrigieren oder mit Begründung bestätigen."""
        a = self.abgleich()
        if a["offen"]:
            raise ValueError(f"Rechnung {self.rechnungsnummer} ({self.lieferant.name1}) weicht um {a['differenz']} € vom Wareneingang ab – "
                             "bitte erst korrigieren oder die Abweichung mit Begründung bestätigen.")

    def historie_eintrag(self, text, benutzer=None):
        zeile = f"{timezone.localtime():%d.%m.%Y %H:%M} {benutzer or '-'}: {text}"
        self.historie = (self.historie + chr(10) + zeile).strip()

    def abweichung_bestaetigen(self, grund, benutzer=None):
        grund = (grund or "").strip()
        a = self.abgleich()
        if not a["verknuepft"] or a["ok"]:
            raise ValueError("Es gibt keine Abweichung zu bestätigen.")
        if not grund:
            raise ValueError("Bitte eine Begründung angeben.")
        self.abweichung_bestaetigt = a["differenz"]
        self.historie_eintrag(f"Abweichung {a['differenz']} € zum Wareneingang bestätigt – {grund}", benutzer)
        self.save()

    def betraege_korrigieren(self, netto, steuer, brutto, benutzer=None):
        if self.status == RechnungStatus.BEZAHLT:
            raise ValueError("Bezahlte Rechnungen lassen sich nicht korrigieren – erst die Zahlung stornieren.")
        if self.status == RechnungStatus.EXPORTIERT:
            raise ValueError("Die Rechnung wurde schon zur Zahlung exportiert (SEPA-Datei) – eine Korrektur könnte doppelt zahlen lassen. "
                             "Bitte erst mit der Bank klären.")
        if netto is None or brutto is None or steuer is None or min(netto, brutto) <= 0 or steuer < 0:
            raise ValueError("Netto, Steuer und Brutto müssen angegeben werden (Netto und Brutto größer 0).")
        if abs(netto + steuer - brutto) > Decimal("0.01"):
            raise ValueError("Netto plus Steuer ergibt nicht den Bruttobetrag.")
        if brutto < self.verrechnet:
            raise ValueError(f"Der Bruttobetrag liegt unter den schon verrechneten {self.verrechnet} € – bitte erst die Verrechnung rückgängig machen.")
        self.historie_eintrag(f"Beträge korrigiert: netto {self.netto} → {netto}, steuer {self.steuer} → {steuer}, "
                              f"brutto {self.brutto} → {brutto}", benutzer)
        self.netto, self.steuer, self.brutto = netto, steuer, brutto
        self.save()


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


class Quelle(models.TextChoices):
    UPLOAD = "upload", "Datei-Upload"
    FTP = "ftp", "FTP-Abruf"
    SFTP = "sftp", "SFTP-Abruf"
    URL = "url", "Download-Adresse (HTTP/HTTPS)"


class Dateityp(models.TextChoices):
    CSV = "csv", "CSV / TXT"
    XLSX = "xlsx", "Excel (XLSX)"
    XLS = "xls", "Excel (XLS)"
    XML = "xml", "XML"


class Preislistenprofil(Zeitstempel):
    """Importprofil für die Preisliste eines Lieferanten: einmal einrichten, danach automatisch nutzbar.
    Spaltenangaben: Spaltenüberschrift (bei Kopfzeile) oder Spaltennummer (1 = erste Spalte);
    bei XML der Name des Unterelements bzw. Attributs."""

    lieferant = models.ForeignKey(Lieferant, on_delete=models.PROTECT, related_name="preislistenprofile")
    name = models.CharField("Bezeichnung der Preisliste", max_length=100)
    aktiv = models.BooleanField(default=True)
    quelle = models.CharField(max_length=6, choices=Quelle.choices, default=Quelle.UPLOAD)
    adresse = models.CharField("FTP-Server bzw. URL", max_length=300, blank=True)
    port = models.PositiveIntegerField(null=True, blank=True)
    pfad = models.CharField("Datei auf dem Server", max_length=300, blank=True)
    benutzer = models.CharField("Benutzer", max_length=100, blank=True)
    zeitplan = models.CharField(
        "Abruf", max_length=12, default="intervall",
        choices=[("intervall", "alle … Stunden (siehe unten; 0 = nur manuell)"), ("taeglich", "täglich zur Uhrzeit"),
                 ("woechentlich", "wöchentlich am Wochentag zur Uhrzeit")])
    intervall_stunden = models.PositiveIntegerField(
        "automatisch abrufen alle … Stunden", default=0, help_text="0 = nur manuell. Läuft über den Befehl preislisten_abrufen.")
    abruf_uhrzeit = models.TimeField("Uhrzeit (täglich/wöchentlich)", default=datetime.time(2, 0), help_text="Serverzeit Europe/Berlin.")
    abruf_wochentag = models.PositiveSmallIntegerField(
        "Wochentag (wöchentlich)", default=0, choices=[(0, "Montag"), (1, "Dienstag"), (2, "Mittwoch"), (3, "Donnerstag"),
                                                      (4, "Freitag"), (5, "Samstag"), (6, "Sonntag")])
    dateityp = models.CharField(max_length=4, choices=Dateityp.choices, default=Dateityp.CSV)
    zeichensatz = models.CharField(max_length=10, default="utf-8", choices=[("utf-8", "UTF-8"), ("cp1252", "ANSI (Windows-1252)")])
    trennzeichen = models.CharField(max_length=3, default=";", help_text="Für Tabulator: \t")
    textbegrenzer = models.CharField(max_length=1, default='"', blank=True)
    dezimaltrennzeichen = models.CharField(max_length=1, default=",", choices=[(",", "Komma"), (".", "Punkt")])
    kopfzeile = models.BooleanField("Datei hat eine Kopfzeile", default=True)
    erste_datenzeile = models.PositiveIntegerField(default=2, help_text="Zeilennummer der ersten Datenzeile (bei XML ohne Bedeutung)")
    xml_satz = models.CharField("XML: Element je Artikel", max_length=60, blank=True)
    sp_bestellnummer = models.CharField("Spalte Bestellnummer des Lieferanten", max_length=60)
    sp_herstellernummer = models.CharField("Spalte Herstellernummer", max_length=60, blank=True)
    sp_ean = models.CharField("Spalte EAN", max_length=60, blank=True)
    sp_bezeichnung = models.CharField("Spalte Bezeichnung", max_length=60, blank=True)
    sp_hersteller = models.CharField("Spalte Hersteller", max_length=60, blank=True)
    sp_kategorie = models.CharField("Spalte Kategorie", max_length=60, blank=True)
    sp_ek = models.CharField("Spalte EK netto", max_length=60)
    sp_bestand = models.CharField("Spalte Bestand", max_length=60, blank=True)
    sp_lieferzeit = models.CharField("Spalte Lieferzeit (Tage)", max_length=60, blank=True)
    sp_gewicht = models.CharField("Spalte Gewicht (kg; g wird erkannt)", max_length=60, blank=True)
    sp_laenge = models.CharField("Spalte Länge (cm; mm/m werden erkannt)", max_length=60, blank=True)
    sp_breite = models.CharField("Spalte Breite (cm; mm/m werden erkannt)", max_length=60, blank=True)
    sp_hoehe = models.CharField("Spalte Höhe (cm; mm/m werden erkannt)", max_length=60, blank=True)
    neue_artikel = models.BooleanField(
        "alle Artikel der Liste in die Preislistentabelle übernehmen", default=True,
        help_text="Sonst werden nur Zeilen übernommen, die zu einem eigenen Artikel passen (Herstellernummer oder EAN).")
    entfernen_veraltete = models.BooleanField(
        "Artikel entfernen, die nicht mehr in der Liste stehen", default=True,
        help_text="Nur für vollständige Preislisten einschalten. Als Schutz vor unvollständigen Downloads wird nichts gelöscht, "
                  "wenn die neue Liste weniger als die Hälfte der bisherigen Artikel enthält.")
    letzter_lauf = models.DateTimeField(null=True, blank=True, editable=False)
    laeuft_seit = models.DateTimeField(null=True, blank=True, editable=False,
                                       help_text="Gesetzt, solange ein Import läuft (verhindert Doppelläufe)")

    class Meta:
        verbose_name = "Preislistenprofil"
        verbose_name_plural = "Preislistenprofile"
        ordering = ["lieferant", "name"]

    def __str__(self):
        return f"{self.lieferant.name1}: {self.name}"

    def faellig(self, jetzt=None) -> bool:
        """Ist der Abruf nach Zeitplan fällig? (Upload-Profile und inaktive nie.)"""
        if not self.aktiv or self.quelle == Quelle.UPLOAD:
            return False
        jetzt = timezone.localtime(jetzt or timezone.now())
        if self.zeitplan == "intervall":
            if self.intervall_stunden <= 0:
                return False
            return self.letzter_lauf is None or (jetzt - self.letzter_lauf).total_seconds() >= self.intervall_stunden * 3600
        termin = jetzt.replace(hour=self.abruf_uhrzeit.hour, minute=self.abruf_uhrzeit.minute, second=0, microsecond=0)
        if self.zeitplan == "woechentlich":
            termin -= datetime.timedelta(days=(jetzt.weekday() - self.abruf_wochentag) % 7)
            if termin > jetzt:
                termin -= datetime.timedelta(days=7)
        elif termin > jetzt:
            termin -= datetime.timedelta(days=1)
        return self.letzter_lauf is None or self.letzter_lauf < termin

    @property
    def passwort_variable(self) -> str:
        return f"PREISLISTE_PW_{self.pk}"

    @property
    def url_variable(self) -> str:
        return f"PREISLISTE_URL_{self.pk}"

    @property
    def url_adresse(self) -> str:
        """Enthält die Adresse einen Zugangsschlüssel, steht sie in der .env (PREISLISTE_URL_<ID>) und hat Vorrang.
        Sonst steht sie im Feld 'Adresse'; eine versehentlich im Feld 'Datei auf dem Server' eingetragene URL wird mit genutzt."""
        import os
        aus_env = os.environ.get(self.url_variable, "").strip() if self.pk else ""
        if aus_env:
            return aus_env
        adresse = (self.adresse or "").strip()
        return adresse if adresse else (self.pfad or "").strip()

    def clean(self):
        from django.core.exceptions import ValidationError
        if self.quelle == Quelle.URL and not self.url_adresse.lower().startswith(("http://", "https://")):
            raise ValidationError({"adresse": "Bei „Download-Adresse“ bitte die vollständige Adresse mit https:// eintragen."})
        if self.quelle in (Quelle.FTP, Quelle.SFTP) and (not self.adresse.strip() or not self.pfad.strip()):
            raise ValidationError("Bei FTP/SFTP bitte Server und Dateipfad eintragen.")


class Preislistenimport(models.Model):
    """Protokoll eines Imports: Anzahl neu/geändert/Fehler, fehlerhafte Zeilen einsehbar."""

    profil = models.ForeignKey(Preislistenprofil, on_delete=models.CASCADE, related_name="importe")
    zeitpunkt = models.DateTimeField(default=timezone.now)
    dateiname = models.CharField(max_length=200, blank=True)
    gelesen = models.PositiveIntegerField(default=0)
    neu = models.PositiveIntegerField(default=0)
    geaendert = models.PositiveIntegerField(default=0)
    unveraendert = models.PositiveIntegerField(default=0)
    uebersprungen = models.PositiveIntegerField(default=0)
    geloescht = models.PositiveIntegerField("entfernt (nicht mehr gelistet)", default=0)
    fehler = models.PositiveIntegerField(default=0)
    fehlerzeilen = models.JSONField(default=list, blank=True)
    meldung = models.CharField(max_length=300, blank=True)
    benutzer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    # Quarantäne (Issue 12, Schritt 2): auffällige Imports werden nicht übernommen, bis jemand sie freigibt oder verwirft
    status = models.CharField(max_length=12, default="uebernommen", db_index=True, choices=[
        ("uebernommen", "übernommen"), ("zur_pruefung", "zur Prüfung"), ("verworfen", "verworfen")])
    datei = models.BinaryField("zurückgehaltene Datei", null=True, blank=True, editable=False,
                               help_text="Nur bei Status „zur Prüfung“; wird nach Freigabe oder Verwerfen gelöscht.")
    zur_pruefung = models.PositiveIntegerField("Preise zur Prüfung", default=0)

    class Meta:
        verbose_name = "Preislistenimport"
        verbose_name_plural = "Preislistenimporte"
        ordering = ["-zeitpunkt"]


class Preispruefung(models.Model):
    """Einzelpreis, der im Import um mehr als die Grenze (50 %) vom bisherigen abwich: nicht übernommen, Übernahme per Klick.
    Zugleich das Protokoll alter und neuer Preise."""

    importlauf = models.ForeignKey(Preislistenimport, on_delete=models.CASCADE, related_name="pruefungen")
    artikel = models.ForeignKey("stammdaten.Artikel", on_delete=models.CASCADE, related_name="preispruefungen")
    lieferant = models.ForeignKey(Lieferant, on_delete=models.CASCADE, related_name="preispruefungen")
    bestellnummer = models.CharField(max_length=100)
    ek_alt = models.DecimalField(max_digits=12, decimal_places=4)
    ek_neu = models.DecimalField(max_digits=12, decimal_places=4)
    status = models.CharField(max_length=20, default="offen", db_index=True, choices=[
        ("offen", "offen"), ("uebernommen", "übernommen"), ("verworfen", "verworfen")])
    bearbeitet_am = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Preisprüfung"
        verbose_name_plural = "Preisprüfungen"
        ordering = ["-importlauf__zeitpunkt", "artikel__nummer"]

    @property
    def abweichung(self):
        from apps.stammdaten.ekkalk import abweichung_prozent
        return abweichung_prozent(self.ek_alt, self.ek_neu)


class BestpreisRhythmus(models.TextChoices):
    AUS = "aus", "nur manuell (aus)"
    NACH_LAUF = "nach_lauf", "nach jedem Preislisten-Abruf"
    TAEGLICH = "taeglich", "täglich zur Uhrzeit"
    WOECHENTLICH = "woechentlich", "wöchentlich am Wochentag zur Uhrzeit"


class Bestpreisplan(Zeitstempel):
    """Einstellung (nur ein Datensatz): wann der günstigste Lieferanten-EK je Herstellernummer in die A-Artikel übernommen wird."""

    rhythmus = models.CharField("Übernahme", max_length=12, choices=BestpreisRhythmus.choices, default=BestpreisRhythmus.AUS)
    uhrzeit = models.TimeField("Uhrzeit", default=datetime.time(3, 0), help_text="Gilt für täglich und wöchentlich (Serverzeit Europe/Berlin).")
    wochentag = models.PositiveSmallIntegerField(
        "Wochentag", default=0, choices=[(0, "Montag"), (1, "Dienstag"), (2, "Mittwoch"), (3, "Donnerstag"), (4, "Freitag"),
                                        (5, "Samstag"), (6, "Sonntag")], help_text="Nur für wöchentlich.")
    letzter_lauf = models.DateTimeField(null=True, blank=True, editable=False)
    letzte_meldung = models.CharField(max_length=200, blank=True, editable=False)

    class Meta:
        verbose_name = "Zeitplan: Günstigster EK in Artikel übernehmen"
        verbose_name_plural = "Zeitplan: Günstigster EK in Artikel übernehmen"

    def __str__(self):
        return f"Günstigster EK: {self.get_rhythmus_display()}"

    @classmethod
    def holen(cls):
        return cls.objects.first() or cls.objects.create()

    def faellig(self, jetzt=None, preislisten_importiert=False) -> bool:
        """Täglich/wöchentlich: fällig, wenn der letzte Plantermin seit dem letzten Lauf verstrichen ist."""
        jetzt = timezone.localtime(jetzt or timezone.now())
        if self.rhythmus == BestpreisRhythmus.NACH_LAUF:
            return preislisten_importiert
        if self.rhythmus not in (BestpreisRhythmus.TAEGLICH, BestpreisRhythmus.WOECHENTLICH):
            return False
        termin = jetzt.replace(hour=self.uhrzeit.hour, minute=self.uhrzeit.minute, second=0, microsecond=0)
        if self.rhythmus == BestpreisRhythmus.TAEGLICH:
            if termin > jetzt:
                termin -= datetime.timedelta(days=1)
        else:
            termin -= datetime.timedelta(days=(jetzt.weekday() - self.wochentag) % 7)
            if termin > jetzt:
                termin -= datetime.timedelta(days=7)
        return self.letzter_lauf is None or self.letzter_lauf < termin


class RuecksendungArt(models.TextChoices):
    GUTSCHRIFT = "gutschrift", "zur Gutschrift"
    AUSTAUSCH = "austausch", "zum Austausch"


class RuecksendungStatus(models.TextChoices):
    ANGELEGT = "angelegt", "gebucht, noch nicht versendet"
    VERSENDET = "versendet", "an Lieferanten versendet"
    GUTSCHRIFT = "gutschrift", "Gutschrift erhalten"
    ERSATZ = "ersatz", "Ersatz erhalten"
    ABGELEHNT = "abgelehnt", "abgelehnt – Ware zurück im Lager"


RUECKSENDUNG_OFFEN = (RuecksendungStatus.ANGELEGT, RuecksendungStatus.VERSENDET)


class Ruecksendung(Zeitstempel):
    """WE-Rücksendung (Wunsch des Nutzers 18.11.2026): schon gebuchte Ware aus einem Wareneingang geht an den Lieferanten
    zurück, zur Gutschrift oder zum Austausch (z. B. Überlieferung oder falsche Ware). Mit dem Anlegen wird der Bestand
    abgebucht; die Lieferantenbestellung bleibt unverändert. Defekte Einzelgeräte laufen weiter über die RMA."""

    mandant = models.ForeignKey(Mandant, on_delete=models.PROTECT, related_name="ruecksendungen")
    lieferant = models.ForeignKey(Lieferant, on_delete=models.PROTECT, related_name="ruecksendungen")
    wareneingang = models.ForeignKey(Wareneingang, on_delete=models.PROTECT, related_name="ruecksendungen")
    nummer = models.CharField("Rücksendung-Nr.", max_length=20, db_index=True)
    datum = models.DateField(default=timezone.localdate)
    art = models.CharField("Rücksendung", max_length=10, choices=RuecksendungArt.choices)
    status = models.CharField(max_length=10, choices=RuecksendungStatus.choices, default=RuecksendungStatus.ANGELEGT, db_index=True)
    grund = models.TextField("Grund")
    versendet_am = models.DateField(null=True, blank=True)
    erledigt_am = models.DateField(null=True, blank=True)
    gutschrift_betrag = models.DecimalField("Gutschrift netto", **GELD, null=True, blank=True)
    ersatz_wareneingang = models.OneToOneField(Wareneingang, on_delete=models.SET_NULL, null=True, blank=True,
                                               related_name="ersatz_fuer", verbose_name="Wareneingang der Ersatzlieferung")
    historie = models.TextField("Verlauf", blank=True)
    benutzer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")

    class Meta:
        verbose_name = "WE-Rücksendung"
        verbose_name_plural = "WE-Rücksendungen"
        ordering = ["-datum", "-id"]
        constraints = [models.UniqueConstraint(fields=["mandant", "nummer"], name="ruecksendung_nummer_eindeutig")]

    def __str__(self):
        return f"Rücksendung {self.nummer}"

    @property
    def summe_netto(self) -> Decimal:
        """Erwartete Gutschrift: EK des Wareneingangs × zurückgesendete Menge."""
        return sum((p.netto for p in self.positionen.all()), Decimal("0")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    @property
    def offen(self) -> bool:
        return self.status in RUECKSENDUNG_OFFEN

    def historie_eintrag(self, text, benutzer=None):
        zeile = f"{timezone.localtime():%d.%m.%Y %H:%M} {benutzer or '-'}: {text}"
        self.historie = (self.historie + chr(10) + zeile).strip()


class RuecksendungPosition(models.Model):
    ruecksendung = models.ForeignKey(Ruecksendung, on_delete=models.CASCADE, related_name="positionen")
    we_position = models.ForeignKey(WareneingangPosition, on_delete=models.PROTECT, related_name="ruecksendungen")
    artikel = models.ForeignKey(Artikel, on_delete=models.PROTECT)
    menge = models.DecimalField(max_digits=10, decimal_places=3)
    ek = models.DecimalField("EK netto", **EK)
    seriennummern = models.TextField("Seriennummern (eine pro Zeile)", blank=True)

    class Meta:
        verbose_name = "Rücksendungsposition"
        verbose_name_plural = "Rücksendungspositionen"
        ordering = ["id"]

    @property
    def netto(self) -> Decimal:
        return self.menge * self.ek


class Lieferantengutschrift(Zeitstempel):
    """Gutschrift eines Lieferanten (Entscheidung Nutzer 19.11.2026), meist aus einer WE-Rücksendung. Sie wird gegen offene
    Lieferantenrechnungen verrechnet (auch teilweise) oder als Zahlung des Lieferanten gebucht; was bleibt, ist Restguthaben."""

    mandant = models.ForeignKey(Mandant, on_delete=models.PROTECT, related_name="lieferantengutschriften")
    lieferant = models.ForeignKey(Lieferant, on_delete=models.PROTECT, related_name="gutschriften")
    ruecksendung = models.OneToOneField(Ruecksendung, on_delete=models.PROTECT, null=True, blank=True, related_name="lieferantengutschrift")
    nummer = models.CharField("Gutschrift-Nr. des Lieferanten", max_length=60)
    datum = models.DateField("Gutschriftdatum")
    netto = models.DecimalField(**GELD)
    steuer = models.DecimalField("USt", **GELD, default=Decimal("0"))
    brutto = models.DecimalField(**GELD)
    notiz = models.CharField("Notiz", max_length=200, blank=True)
    historie = models.TextField("Verlauf", blank=True)
    benutzer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")

    class Meta:
        verbose_name = "Lieferanten-Gutschrift"
        verbose_name_plural = "Lieferanten-Gutschriften"
        ordering = ["-datum", "-id"]
        constraints = [models.UniqueConstraint(fields=["mandant", "lieferant", "nummer"], name="lieferantengutschrift_eindeutig")]

    def __str__(self):
        return f"{self.lieferant.name1} Gutschrift {self.nummer}"

    @property
    def verrechnet(self) -> Decimal:
        return sum((v.betrag for v in self.verrechnungen.all() if not v.aufgehoben), Decimal("0"))

    @property
    def eingegangen(self) -> Decimal:
        """Vom Lieferanten ausgezahlt (Zahlungseingänge ohne stornierte)."""
        return sum((z.betrag for z in self.zahlungen.all() if not z.storniert), Decimal("0"))

    @property
    def restguthaben(self) -> Decimal:
        return self.brutto - self.verrechnet - self.eingegangen

    def historie_eintrag(self, text, benutzer=None):
        zeile = f"{timezone.localtime():%d.%m.%Y %H:%M} {benutzer or '-'}: {text}"
        self.historie = (self.historie + chr(10) + zeile).strip()


class LieferantengutschriftVerrechnung(models.Model):
    """Verrechnung einer Lieferanten-Gutschrift mit einer offenen Lieferantenrechnung. Rückgängig machen nur mit Grund
    (die Zeile bleibt als „aufgehoben“ stehen)."""

    gutschrift = models.ForeignKey(Lieferantengutschrift, on_delete=models.PROTECT, related_name="verrechnungen")
    rechnung = models.ForeignKey(Lieferantenrechnung, on_delete=models.PROTECT, related_name="verrechnungen")
    betrag = models.DecimalField(**GELD)
    datum = models.DateField(default=timezone.localdate)
    benutzer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    angelegt_am = models.DateTimeField(default=timezone.now)
    aufgehoben = models.BooleanField(default=False, db_index=True)
    aufgehoben_am = models.DateTimeField(null=True, blank=True)
    aufgehoben_grund = models.CharField(max_length=300, blank=True)
    aufgehoben_von = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")

    class Meta:
        verbose_name = "Gutschrift-Verrechnung (Lieferant)"
        verbose_name_plural = "Gutschrift-Verrechnungen (Lieferanten)"
        ordering = ["id"]

    def __str__(self):
        return f"{self.betrag} € {self.gutschrift.nummer} → {self.rechnung.rechnungsnummer}"


class LieferantengutschriftZahlung(models.Model):
    """Zahlung des Lieferanten auf eine Gutschrift (Eingang). Unveränderlich; Korrektur nur per Storno mit Grund."""

    gutschrift = models.ForeignKey(Lieferantengutschrift, on_delete=models.PROTECT, related_name="zahlungen")
    datum = models.DateField(default=timezone.localdate)
    betrag = models.DecimalField(**GELD)
    bankumsatz = models.ForeignKey("finanzen.Bankumsatz", on_delete=models.PROTECT, null=True, blank=True, related_name="gutschrift_zahlungen")
    benutzer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    angelegt_am = models.DateTimeField(default=timezone.now)
    storniert = models.BooleanField(default=False, db_index=True)
    storno_grund = models.CharField(max_length=300, blank=True)
    storno_am = models.DateTimeField(null=True, blank=True)
    storno_von = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")

    class Meta:
        verbose_name = "Zahlung auf Lieferanten-Gutschrift"
        verbose_name_plural = "Zahlungen auf Lieferanten-Gutschriften"
        ordering = ["-datum", "-id"]
