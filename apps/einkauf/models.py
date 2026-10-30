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
        return (self.menge * self.ek).quantize(Decimal("0.01"))

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
        """Netto der verknüpften Wareneingänge inkl. Nebenkosten gegen Rechnung: Differenz > 1 Cent ist eine Abweichung."""
        wareneingaenge = list(self.wareneingaenge.all())
        if not wareneingaenge:
            return {"verknuepft": False, "differenz": None, "ok": None, "bestaetigt": False, "offen": False}
        soll = sum((w.gesamt_netto for w in wareneingaenge), Decimal("0"))  # Nebenkosten stehen mit auf der Rechnung
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
    sp_gewicht = models.CharField("Spalte Gewicht (kg)", max_length=60, blank=True)
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

    class Meta:
        verbose_name = "Preislistenimport"
        verbose_name_plural = "Preislistenimporte"
        ordering = ["-zeitpunkt"]


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
