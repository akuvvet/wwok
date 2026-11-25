"""
Kern: Mandanten, gemeinsame Basisklassen und Änderungsprotokoll (GoBD).
"""
import re

from django.conf import settings
from django.core.exceptions import ValidationError
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
    standard_vk = models.PositiveSmallIntegerField(
        "Standard-VK", default=3, choices=[(1, "VK1 – Endkunden"), (2, "VK2 – gewerbliche Endkunden"), (3, "VK3 – Händler")],
        help_text="Diese Preisstufe zeigen die Artikellisten dieses Mandanten (Artikel & Bestand, Artikelsuche, Verwaltung). "
                  "Der Preis in Belegen richtet sich weiterhin nach der Preisstufe des Kunden.")
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
    # E-Mail-Versand. Das Passwort wird in der Verwaltung eingetragen und verschlüsselt gespeichert (kern/geheim.py);
    # nicht editierbar = erscheint weder im Formular noch im Änderungsprotokoll. Rückfall: .env SMTP_PASSWORD_<KURZNAME>.
    smtp_passwort_geheim = models.TextField("SMTP-Passwort (verschlüsselt)", blank=True, editable=False)
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
    datev_zahlungen = models.BooleanField(
        "Zahlungen im DATEV-Export", default=False,
        help_text="Aus lassen, wenn der Steuerberater das Bankkonto selbst aus den Kontoauszügen bucht (sonst doppelt). "
                  "Eingeschaltet: Bank/Kasse/Skonto an Debitor, Zahlung und ihr Storno im selben Zeitraum entfallen.")
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

    def smtp_passwort_setzen(self, klartext: str):
        """Speichert das Passwort verschlüsselt; leer = entfernen (dann gilt wieder die .env)."""
        from .geheim import verschluesseln

        self.smtp_passwort_geheim = verschluesseln(klartext)

    @property
    def smtp_passwort_quelle(self) -> str:
        """"verwaltung", "env" oder "" (kein Passwort hinterlegt)."""
        import os

        from .geheim import entschluesseln

        if entschluesseln(self.smtp_passwort_geheim):
            return "verwaltung"
        return "env" if os.environ.get(f"SMTP_PASSWORD_{self.kurzname.upper()}") else ""

    @property
    def smtp_passwort(self) -> str:
        import os

        from .geheim import entschluesseln

        return entschluesseln(self.smtp_passwort_geheim) or os.environ.get(f"SMTP_PASSWORD_{self.kurzname.upper()}", "")

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


# ---- Zahlungskonten und Online-Plattformen je Mandant (Wunsch des Nutzers 25.11.2026) ----------------

KARTENNUMMER = re.compile(r"(?:\d[ -]?){13,19}")


def keine_kartennummer(wert):
    """Volle Kartennummern dürfen nirgends gespeichert werden (PCI DSS) – nur die letzten 4 Ziffern."""
    if wert and KARTENNUMMER.search(str(wert)):
        raise ValidationError("Bitte keine vollständige Kartennummer speichern – nur die letzten 4 Ziffern.")


class Zahlungskonto(Zeitstempel):
    """Weiteres Zahlungskonto des Mandanten neben der Bank: PayPal oder Kreditkarte. Das PayPal-Secret wird verschlüsselt
    gespeichert und nie angezeigt; von Kreditkarten nur Kartenart, Inhaber, letzte 4 Ziffern und Ablauf – nie Kartennummer oder CVC."""

    class Art(models.TextChoices):
        PAYPAL = "paypal", "PayPal"
        KREDITKARTE = "karte", "Kreditkarte"
        SONSTIGES = "sonst", "Sonstiges (z. B. Stripe, SumUp)"

    class Umgebung(models.TextChoices):
        LIVE = "live", "Live"
        SANDBOX = "sandbox", "Sandbox (Test)"

    mandant = models.ForeignKey(Mandant, on_delete=models.CASCADE, related_name="zahlungskonten")
    art = models.CharField("Art", max_length=6, choices=Art.choices)
    bezeichnung = models.CharField("Bezeichnung", max_length=60, help_text="z. B. „PayPal OKAY“ oder „Firmen-Visa“")
    aktiv = models.BooleanField(default=True)
    datev_konto = models.CharField("DATEV-Konto", max_length=10, blank=True,
                                   help_text="Finanzkonto für dieses Zahlungsmittel – mit dem Steuerberater abstimmen.")
    # PayPal
    paypal_email = models.EmailField("PayPal-E-Mail", blank=True)
    paypal_haendler_id = models.CharField("PayPal-Händler-ID", max_length=20, blank=True,
                                          help_text="PayPal → Kontoeinstellungen → Geschäftsinformationen (Merchant-ID)")
    paypal_client_id = models.CharField("REST-API Client-ID", max_length=120, blank=True,
                                        help_text="developer.paypal.com → Apps & Credentials (Live) → eigene App")
    paypal_umgebung = models.CharField("Umgebung", max_length=7, choices=Umgebung.choices, default=Umgebung.LIVE)
    paypal_secret_geheim = models.TextField(blank=True, editable=False)
    # Kreditkarte
    karte_anbieter = models.CharField("Kartenart", max_length=20, blank=True,
                                      choices=[("visa", "Visa"), ("master", "Mastercard"), ("amex", "American Express"), ("sonst", "andere")])
    karte_inhaber = models.CharField("Karteninhaber", max_length=80, blank=True, validators=[keine_kartennummer])
    karte_letzte4 = models.CharField("letzte 4 Ziffern", max_length=4, blank=True,
                                     validators=[RegexValidator(r"^\d{4}$", "Genau 4 Ziffern.")])
    karte_ablauf = models.CharField("gültig bis (MM/JJ)", max_length=5, blank=True,
                                    validators=[RegexValidator(r"^(0[1-9]|1[0-2])/\d{2}$", "Format MM/JJ, z. B. 08/28.")])
    karte_bank = models.CharField("ausgebende Bank", max_length=80, blank=True, validators=[keine_kartennummer])
    karte_abrechnung_iban = models.CharField("Abrechnung über IBAN", max_length=34, blank=True,
                                             help_text="Konto, von dem die Kartenabrechnung abgebucht wird")
    bemerkung = models.CharField("Notiz", max_length=200, blank=True, validators=[keine_kartennummer])

    class Meta:
        verbose_name = "Zahlungskonto (PayPal/Kreditkarte)"
        verbose_name_plural = "Zahlungskonten (PayPal/Kreditkarte)"
        ordering = ["mandant", "art", "bezeichnung"]
        constraints = [models.UniqueConstraint(fields=["mandant", "bezeichnung"], name="zahlungskonto_bezeichnung_je_mandant")]

    def __str__(self):
        zusatz = f" ••••{self.karte_letzte4}" if self.art == self.Art.KREDITKARTE and self.karte_letzte4 else ""
        return f"{self.bezeichnung}{zusatz}"

    def secret_setzen(self, klartext: str):
        from .geheim import verschluesseln
        self.paypal_secret_geheim = verschluesseln(klartext)

    @property
    def secret(self) -> str:
        from .geheim import entschluesseln
        return entschluesseln(self.paypal_secret_geheim)

    def fehlende_angaben(self) -> list:
        if self.art == self.Art.PAYPAL:
            pflicht = [("PayPal-E-Mail", self.paypal_email), ("REST-API Client-ID", self.paypal_client_id), ("Secret", self.secret)]
        elif self.art == self.Art.KREDITKARTE:
            pflicht = [("Kartenart", self.karte_anbieter), ("letzte 4 Ziffern", self.karte_letzte4), ("gültig bis", self.karte_ablauf)]
        else:
            pflicht = []
        return [name for name, wert in pflicht if not wert]


class Plattformkonto(Zeitstempel):
    """Verkaufskonto auf einer Online-Plattform (mehrere je Plattform möglich, z. B. eBay Account 1 und 2) als Grundlage für das
    spätere Einstellen von Artikeln. Client-Secret und Refresh-Token werden verschlüsselt gespeichert und nie angezeigt.
    Shopware-Shops bleiben unter Shop (eigene Anbindung mit Bestand, Preisen und Bestellimport)."""

    class Plattform(models.TextChoices):
        EBAY = "ebay", "eBay"
        AMAZON = "amazon", "Amazon (Seller Central)"
        KAUFLAND = "kaufland", "Kaufland Marktplatz"

    class Umgebung(models.TextChoices):
        LIVE = "live", "Live"
        SANDBOX = "sandbox", "Sandbox (Test)"

    MARKTPLATZ_VORGABE = {"ebay": "EBAY_DE", "amazon": "A1PA6795UKMFR9", "kaufland": "de"}

    mandant = models.ForeignKey(Mandant, on_delete=models.CASCADE, related_name="plattformkonten")
    plattform = models.CharField("Plattform", max_length=10, choices=Plattform.choices)
    bezeichnung = models.CharField("Bezeichnung", max_length=60, help_text="z. B. „eBay Account 1“")
    aktiv = models.BooleanField(default=True)
    umgebung = models.CharField("Umgebung", max_length=7, choices=Umgebung.choices, default=Umgebung.LIVE)
    kontoname = models.CharField("Benutzer-/Shopname", max_length=100, blank=True,
                                 help_text="eBay-Benutzername, Amazon-Shopname bzw. Kaufland-Shopname")
    haendler_id = models.CharField("Händler-ID", max_length=40, blank=True,
                                   help_text="Amazon: Händler-ID (Merchant Token, Seller Central → Einstellungen → Kontoinformationen). "
                                             "Kaufland: Partner-ID. eBay: leer lassen.")
    marktplatz = models.CharField("Marktplatz", max_length=20, blank=True,
                                  help_text="Leer = Deutschland (eBay EBAY_DE, Amazon A1PA6795UKMFR9, Kaufland de)")
    client_id = models.CharField("Client-ID / Client Key", max_length=200, blank=True,
                                 help_text="eBay: App-ID (developer.ebay.com → Application Keys). Amazon: LWA Client-ID "
                                           "(Solution Provider Portal → App). Kaufland: Client Key (Seller Portal → Einstellungen → API).")
    client_secret_geheim = models.TextField(blank=True, editable=False)
    refresh_token_geheim = models.TextField(blank=True, editable=False)
    refresh_token_bis = models.DateField("Refresh-Token gültig bis", null=True, blank=True,
                                         help_text="eBay: rund 18 Monate nach der Freigabe; danach neu freigeben.")
    # nur eBay
    ebay_dev_id = models.CharField("eBay Dev-ID", max_length=100, blank=True)
    ebay_runame = models.CharField("eBay RuName", max_length=200, blank=True,
                                   help_text="developer.ebay.com → User Tokens → „Get a Token from eBay via Your Application“")
    ebay_richtlinie_versand = models.CharField("Versand-Richtlinie (ID)", max_length=30, blank=True,
                                               help_text="eBay → Konto → Geschäftsrichtlinien; die ID steht in der Adresse")
    ebay_richtlinie_zahlung = models.CharField("Zahlungs-Richtlinie (ID)", max_length=30, blank=True)
    ebay_richtlinie_ruecknahme = models.CharField("Rücknahme-Richtlinie (ID)", max_length=30, blank=True)
    ebay_lagerort = models.CharField("Lagerort-Schlüssel", max_length=36, blank=True,
                                     help_text="merchantLocationKey: Versandort der Artikel, wird einmal bei eBay angelegt")
    # Angebot
    preisstufe = models.PositiveSmallIntegerField("Preisstufe", default=1, choices=[(1, "VK1"), (2, "VK2"), (3, "VK3")])
    preisaufschlag = models.DecimalField("Aufschlag %", max_digits=5, decimal_places=2, default=0,
                                         help_text="z. B. für Plattformgebühren; auf den VK der Preisstufe")
    bestand_puffer = models.PositiveIntegerField("Sicherheitsbestand", default=0,
                                                 help_text="Dieser Bestand wird nicht gemeldet (Schutz vor Doppelverkauf).")
    bemerkung = models.CharField("Notiz", max_length=200, blank=True)

    class Meta:
        verbose_name = "Plattformkonto (eBay/Amazon/Kaufland)"
        verbose_name_plural = "Plattformkonten (eBay/Amazon/Kaufland)"
        ordering = ["mandant", "plattform", "bezeichnung"]
        constraints = [models.UniqueConstraint(fields=["mandant", "bezeichnung"], name="plattformkonto_bezeichnung_je_mandant")]

    def __str__(self):
        return f"{self.bezeichnung} ({self.get_plattform_display()})"

    @property
    def marktplatz_wert(self) -> str:
        return self.marktplatz or self.MARKTPLATZ_VORGABE.get(self.plattform, "")

    def geheimnis_setzen(self, feld: str, klartext: str):
        from .geheim import verschluesseln
        setattr(self, f"{feld}_geheim", verschluesseln(klartext))

    def geheimnis(self, feld: str) -> str:
        from .geheim import entschluesseln
        return entschluesseln(getattr(self, f"{feld}_geheim"))

    def fehlende_angaben(self) -> list:
        """Was für die Anbindung noch fehlt (Stand der Recherche 25.11.2026)."""
        pflicht = {
            "ebay": [("App-ID", self.client_id), ("Cert-ID (Client Secret)", self.geheimnis("client_secret")),
                     ("RuName", self.ebay_runame), ("Refresh-Token", self.geheimnis("refresh_token")),
                     ("Versand-Richtlinie", self.ebay_richtlinie_versand), ("Zahlungs-Richtlinie", self.ebay_richtlinie_zahlung),
                     ("Rücknahme-Richtlinie", self.ebay_richtlinie_ruecknahme), ("Lagerort-Schlüssel", self.ebay_lagerort)],
            "amazon": [("Händler-ID", self.haendler_id), ("LWA Client-ID", self.client_id),
                       ("LWA Client Secret", self.geheimnis("client_secret")), ("Refresh-Token", self.geheimnis("refresh_token"))],
            "kaufland": [("Client Key", self.client_id), ("Secret Key", self.geheimnis("client_secret"))],
        }.get(self.plattform, [])
        return [name for name, wert in pflicht if not wert]
