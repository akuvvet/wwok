"""
Kern: Mandanten, gemeinsame Basisklassen und Änderungsprotokoll (GoBD).
"""
from django.conf import settings
from django.core.validators import RegexValidator
from django.db import models


FARBPRUEFER = RegexValidator(r"^#[0-9a-fA-F]{6}$", "Farbe als #RRGGBB, z. B. #15803d.")


class Zeitstempel(models.Model):
    """Basisklasse: Anlagedatum, letzte Änderung und Benutzer automatisch."""

    angelegt_am = models.DateTimeField("angelegt am", auto_now_add=True)
    geaendert_am = models.DateTimeField("letzte Änderung", auto_now=True)
    geaendert_von = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="geändert von",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        editable=False,
    )

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        from .middleware import aktueller_benutzer

        benutzer = aktueller_benutzer()
        if benutzer is not None and benutzer.is_authenticated:
            self.geaendert_von = benutzer
        super().save(*args, **kwargs)


class Mandant(Zeitstempel):
    """Firma, für die Belege, Bestände usw. getrennt geführt werden."""

    kurzname = models.CharField("Kurzname", max_length=30, unique=True)
    firmenname = models.CharField("Firmenname", max_length=200)
    strasse = models.CharField("Straße", max_length=200, blank=True)
    plz = models.CharField("PLZ", max_length=10, blank=True)
    ort = models.CharField("Ort", max_length=100, blank=True)
    land = models.CharField("Land", max_length=2, default="DE")
    ust_id = models.CharField("USt-ID", max_length=20, blank=True)
    steuernummer = models.CharField("Steuernummer", max_length=30, blank=True)
    telefon = models.CharField("Telefon", max_length=50, blank=True)
    email = models.EmailField("E-Mail", blank=True)
    iban = models.CharField("IBAN", max_length=34, blank=True)
    bic = models.CharField("BIC", max_length=11, blank=True)
    glaeubiger_id = models.CharField("Gläubiger-ID (SEPA)", max_length=35, blank=True)
    aktiv = models.BooleanField("aktiv", default=True)
    farbe = models.CharField(
        "Layoutfarbe", max_length=7, blank=True, validators=[FARBPRUEFER],
        help_text="Akzentfarbe der Oberfläche für diesen Mandanten (Menü, Knöpfe, Links, Kopfleiste), z. B. Grün für ZONE, Blau für OKAY. "
                  "Leer = Standardblau. Im dunklen Modus wird sie automatisch aufgehellt.")
    flaechenmodus = models.CharField(
        "Flächenfarben", max_length=8, default="standard",
        choices=[("standard", "Neutral (Standard)"),
                 ("ton", "Gleiche Tonart – Kopfbereich, Seitenleiste und Karten aus der Layoutfarbe abgeleitet"),
                 ("eigen", "Eigene Farbe je Bereich")],
        help_text="Neutral: weiße Flächen wie bisher. Gleiche Tonart: alle drei Bereiche in Abstufungen der Layoutfarbe. "
                  "Eigene Farbe: die drei Felder darunter gelten. Schrift und Ränder passen sich automatisch an, damit alles lesbar bleibt.")
    farbe_kopf = models.CharField("Farbe Kopfbereich", max_length=7, blank=True, validators=[FARBPRUEFER],
                                  help_text="Nur bei „Eigene Farbe je Bereich“. Leer = neutral.")
    farbe_leiste = models.CharField("Farbe Seitenleiste", max_length=7, blank=True, validators=[FARBPRUEFER],
                                    help_text="Nur bei „Eigene Farbe je Bereich“. Leer = neutral.")
    farbe_karte = models.CharField("Farbe Karten", max_length=7, blank=True, validators=[FARBPRUEFER],
                                   help_text="Nur bei „Eigene Farbe je Bereich“. Leer = neutral.")
    logo = models.ImageField("Logo", upload_to="logos/", blank=True,
                             help_text="PNG oder JPG, am besten quer und mit transparentem oder weißem Hintergrund (max. 2 MB). Erscheint oben rechts auf Belegen.")
    # E-Mail-Versand (das Passwort steht nicht hier, sondern in der .env als SMTP_PASSWORD_<KURZNAME>)
    smtp_host = models.CharField("SMTP-Server", max_length=100, blank=True)
    smtp_port = models.PositiveIntegerField("SMTP-Port", default=587)
    smtp_sicherheit = models.CharField(
        "Verschlüsselung", max_length=8, default="starttls",
        choices=[("starttls", "STARTTLS (Port 587)"), ("ssl", "SSL/TLS (Port 465)")])
    smtp_benutzer = models.CharField("SMTP-Benutzer", max_length=150, blank=True)
    absender_email = models.EmailField("Absenderadresse", blank=True)
    absender_name = models.CharField("Absendername", max_length=100, blank=True)
    # DATEV-Export (Kontenrahmen SKR03 als Vorgabe; mit dem Steuerberater abstimmen)
    datev_berater = models.CharField("DATEV Beraternummer", max_length=10, blank=True)
    datev_mandant = models.CharField("DATEV Mandantennummer", max_length=10, blank=True)
    konto_erloes_19 = models.CharField("Erlöskonto 19 %", max_length=8, default="8400")
    konto_erloes_7 = models.CharField("Erlöskonto 7 %", max_length=8, default="8300")
    konto_erloes_0 = models.CharField("Erlöskonto steuerfrei", max_length=8, default="8200")
    konto_bank = models.CharField("Bankkonto", max_length=8, default="1200")
    konto_kasse = models.CharField("Kassenkonto", max_length=8, default="1000")
    konto_skonto = models.CharField("Konto gewährte Skonti", max_length=8, default="8736")
    kopie_an = models.EmailField("Kopie (BCC) an", blank=True, help_text="Optional: jede Belegmail zusätzlich hierhin.")

    class Meta:
        verbose_name = "Mandant"
        verbose_name_plural = "Mandanten"
        ordering = ["kurzname"]

    def __str__(self):
        return self.kurzname

    @property
    def flaechen_css(self):
        """CSS für die Flächenfarben (Kopfbereich, Seitenleiste, Karten); leer bei „Neutral“."""
        from .layout import flaechen_css

        return flaechen_css(self)


class AenderungsProtokoll(models.Model):
    """Unveränderliches Protokoll jeder Änderung (GoBD)."""

    zeitpunkt = models.DateTimeField(auto_now_add=True, db_index=True)
    benutzer = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True
    )
    tabelle = models.CharField(max_length=100, db_index=True)
    datensatz_id = models.CharField(max_length=50, db_index=True)
    aktion = models.CharField(max_length=20)  # anlegen, aendern, loeschen
    vorher = models.JSONField(null=True, blank=True)
    nachher = models.JSONField(null=True, blank=True)

    class Meta:
        verbose_name = "Änderungsprotokoll"
        verbose_name_plural = "Änderungsprotokoll"
        ordering = ["-zeitpunkt"]

    def save(self, *args, **kwargs):
        if self.pk:
            raise PermissionError("Protokolleinträge dürfen nicht geändert werden.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise PermissionError("Protokolleinträge dürfen nicht gelöscht werden.")


class Listenansicht(models.Model):
    """Gespeicherte Ansicht einer Liste (Spalten, Reihenfolge, Breite, Dichte) je Benutzer."""

    benutzer = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="Benutzer", on_delete=models.CASCADE, related_name="listenansichten"
    )
    schluessel = models.CharField("Liste", max_length=200)
    einstellung = models.JSONField("Einstellung", default=dict)
    geaendert_am = models.DateTimeField("geändert", auto_now=True)

    class Meta:
        verbose_name = "Listenansicht"
        verbose_name_plural = "Listenansichten"
        ordering = ["benutzer", "schluessel"]
        constraints = [
            models.UniqueConstraint(fields=["benutzer", "schluessel"], name="listenansicht_je_benutzer_eindeutig"),
        ]

    def __str__(self):
        return f"{self.benutzer} – {self.schluessel}"
