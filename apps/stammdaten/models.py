"""
Stammdaten: Kunden, Lieferanten, Artikel – gemäß Pflichtenheft Abschnitt 9.

Gemeinsam für beide Mandanten: Kunden, Lieferanten, Artikel, Kategorien.
Pro Mandant getrennt: Kreditlimit, Kundennummer beim Lieferanten, Lieferanten-
Ansprechpartner, letzter EK.
"""
import logging
import os
import uuid
from decimal import Decimal

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models, transaction
from django.db.models.signals import post_delete
from django.dispatch import receiver

from apps.kern.models import Mandant, Zeitstempel

GELD = {"max_digits": 12, "decimal_places": 2}
EK_GELD = {"max_digits": 12, "decimal_places": 4}


# ---------------------------------------------------------------------------
# Hilfstabellen
# ---------------------------------------------------------------------------

class Zahlungsbedingung(Zeitstempel):
    nummer = models.PositiveSmallIntegerField("Nummer", unique=True, null=True, blank=True)
    bezeichnung = models.CharField("Bezeichnung", max_length=100, unique=True)
    tage_netto = models.PositiveSmallIntegerField(
        "Rechnung fällig (Tage nach Belegdatum)", default=0,
        help_text="Belegdatum + diese Tage = Fälligkeitsdatum; ersetzt ff.ff.ffff im Belegtext.")
    tage_mahnung = models.PositiveSmallIntegerField(
        "Mahnung fällig (Tage nach Belegdatum)", default=0, help_text="Interne Frist für die Mahnung.")
    tage_lieferstop = models.PositiveSmallIntegerField(
        "Lieferstopp (Tage nach Belegdatum)", default=0, help_text="Interne Frist für den Lieferstopp.")
    skonto_tage = models.PositiveSmallIntegerField("Skontofrist (Tage)", default=0)
    skonto_prozent = models.DecimalField("Skonto %", max_digits=5, decimal_places=2, default=Decimal("0"))
    vorkasse = models.BooleanField("Vorkasse", default=False)
    lastschrift = models.BooleanField("SEPA-Lastschrift", default=False)
    druck_text = models.CharField("Text auf Beleg", max_length=255, blank=True)

    class Meta:
        verbose_name = "Zahlungsbedingung"
        verbose_name_plural = "Zahlungsbedingungen"
        ordering = ["bezeichnung"]

    def __str__(self):
        return self.bezeichnung


class Steuersatz(Zeitstempel):
    nummer = models.PositiveSmallIntegerField("ID", unique=True, null=True, blank=True)
    gueltig_ab = models.DateField("gültig ab", null=True, blank=True)
    bezeichnung = models.CharField("Bezeichnung", max_length=50, unique=True)
    prozent = models.DecimalField("Prozent", max_digits=5, decimal_places=2)

    class Meta:
        verbose_name = "Steuersatz"
        verbose_name_plural = "Steuersätze"
        ordering = ["-prozent"]

    def __str__(self):
        return f"{self.bezeichnung} ({self.prozent} %)"


class Kategorie(Zeitstempel):
    nummer = models.CharField("Nummer", max_length=20, unique=True, null=True, blank=True)
    name = models.CharField("Name", max_length=150)
    eltern = models.ForeignKey("self", verbose_name="übergeordnet", on_delete=models.PROTECT,
                               null=True, blank=True, related_name="unterkategorien")
    aufschlag_vk1 = models.DecimalField("Aufschlag VK1 %", max_digits=6, decimal_places=2, null=True, blank=True)
    aufschlag_vk2 = models.DecimalField("Aufschlag VK2 %", max_digits=6, decimal_places=2, null=True, blank=True)
    aufschlag_vk3 = models.DecimalField("Aufschlag VK3 %", max_digits=6, decimal_places=2, null=True, blank=True)

    class Meta:
        verbose_name = "Kategorie"
        verbose_name_plural = "Kategorien"
        ordering = ["name"]

    def __str__(self):
        return f"{self.eltern} › {self.name}" if self.eltern_id else self.name


class Leistungskategorie(Zeitstempel):
    """Eigene Kategorien für Leistungsartikel (Typ L), getrennt von den Artikelkategorien."""

    nummer = models.CharField("Nummer", max_length=20, unique=True, null=True, blank=True)
    name = models.CharField("Name", max_length=150, unique=True)

    class Meta:
        verbose_name = "Leistungskategorie"
        verbose_name_plural = "Leistungskategorien"
        ordering = ["name"]

    def __str__(self):
        return self.name


class Hersteller(Zeitstempel):
    kennzeichen = models.CharField("ID / Kennzeichen", max_length=20, unique=True, null=True, blank=True)
    name = models.CharField("Name", max_length=150)
    online = models.BooleanField("Online", default=False)

    class Meta:
        verbose_name = "Hersteller"
        verbose_name_plural = "Hersteller"
        ordering = ["name"]

    def __str__(self):
        return self.name


# ---------------------------------------------------------------------------
# Gemeinsame Adress-/Firmenfelder für Kunde und Lieferant
# ---------------------------------------------------------------------------

class Gesellschaftsform(models.TextChoices):
    PRIVAT = "privat", "Privatperson"
    EINZEL = "einzel", "Einzelunternehmen"
    GBR = "gbr", "GbR"
    OHG = "ohg", "OHG"
    KG = "kg", "KG"
    GMBH = "gmbh", "GmbH"
    UG = "ug", "UG (haftungsbeschränkt)"
    GMBHCOKG = "gmbhcokg", "GmbH & Co. KG"
    AG = "ag", "AG"
    EV = "ev", "e. V."
    BEHOERDE = "behoerde", "Behörde / öffentlich"
    SONSTIGE = "sonstige", "Sonstige"


class Partner(Zeitstempel):
    """Abstrakte Basis: gemeinsame Felder von Kunde und Lieferant."""

    name1 = models.CharField("Name 1", max_length=150, db_index=True)
    name2 = models.CharField("Name 2", max_length=150, blank=True)
    name3 = models.CharField("Name 3", max_length=150, blank=True)
    strasse = models.CharField("Straße", max_length=200, blank=True)
    plz = models.CharField("PLZ", max_length=10, blank=True, db_index=True)
    ort = models.CharField("Ort", max_length=100, blank=True)
    land = models.CharField("Land (ISO)", max_length=2, default="DE")
    telefon = models.CharField("Telefon", max_length=50, blank=True)
    email = models.EmailField("E-Mail", blank=True)
    webseite = models.URLField("Webseite", blank=True)
    gesellschaftsform = models.CharField("Gesellschaftsform", max_length=20,
                                         choices=Gesellschaftsform.choices, blank=True)
    inhaber_gf = models.CharField("Inhaber / Geschäftsführer", max_length=200, blank=True)
    ust_id = models.CharField("USt-ID", max_length=20, blank=True)
    ust_id_geprueft_am = models.DateField("USt-ID geprüft am", null=True, blank=True)
    zahlungsbedingung = models.ForeignKey(Zahlungsbedingung, verbose_name="Zahlungsbedingung",
                                          on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    email_weitere = models.TextField(
        "weitere E-Mail-Adressen", blank=True,
        help_text="Komma oder Zeilenumbruch; werden bei Rechnungen, Gutschriften und Mahnungen mit angeschrieben.")
    hinweis = models.TextField("Hinweis (wird beim Öffnen angezeigt)", blank=True)
    # Bankverbindung
    kontoinhaber = models.CharField("Kontoinhaber", max_length=150, blank=True)
    iban = models.CharField("IBAN", max_length=34, blank=True)
    bic = models.CharField("BIC", max_length=11, blank=True)
    bank = models.CharField("Bank", max_length=100, blank=True)
    gesperrt = models.BooleanField("gesperrt", default=False)
    anonymisiert = models.BooleanField("anonymisiert (DSGVO)", default=False, editable=False)

    class Meta:
        abstract = True

    def __str__(self):
        return f"{self.nummer} – {self.name1}"


# ---------------------------------------------------------------------------
# Kunden
# ---------------------------------------------------------------------------

class Kundenart(models.TextChoices):
    ENDKUNDE = "endkunde", "Endkunde"
    GEWERBLICH = "gewerblich", "gewerblicher Endkunde"
    WIEDERVERKAEUFER = "wiederverkaeufer", "Wiederverkäufer"


class Preisstufe(models.IntegerChoices):
    VK1 = 1, "VK1 – Endkunden"
    VK2 = 2, "VK2 – gewerbliche Endkunden"
    VK3 = 3, "VK3 – Händler"


class Kunde(Partner):
    nummer = models.CharField("Kundennummer", max_length=20, unique=True)
    kundenart = models.CharField("Kundenart", max_length=20, choices=Kundenart.choices,
                                 default=Kundenart.ENDKUNDE)
    preisstufe = models.PositiveSmallIntegerField("Preisstufe", choices=Preisstufe.choices,
                                                  default=Preisstufe.VK1)
    erstkontakt = models.DateField("Erstkontakt", auto_now_add=True)
    leitweg_id = models.CharField("Leitweg-ID (XRechnung)", max_length=46, blank=True,
                                  help_text="Nur für Behörden/öffentliche Auftraggeber: Pflichtangabe der XRechnung.")
    # SEPA-Lastschriftmandat
    sepa_mandatsreferenz = models.CharField("SEPA-Mandatsreferenz", max_length=35, blank=True)
    sepa_mandat_datum = models.DateField("SEPA-Mandat Datum", null=True, blank=True)

    class Meta:
        verbose_name = "Kunde"
        verbose_name_plural = "Kunden"
        ordering = ["nummer"]


class KundeMandant(Zeitstempel):
    """Mandantenbezogene Kundendaten (Kreditlimit, Newsletter-Einwilligung)."""

    kunde = models.ForeignKey(Kunde, on_delete=models.CASCADE, related_name="mandantendaten")
    mandant = models.ForeignKey(Mandant, on_delete=models.PROTECT)
    kreditlimit = models.DecimalField("Kreditlimit", null=True, blank=True, **GELD)
    newsletter = models.BooleanField("Newsletter-Einwilligung", default=False)
    newsletter_bestaetigt_am = models.DateTimeField("Double-Opt-in bestätigt am", null=True, blank=True)

    class Meta:
        verbose_name = "Kundendaten pro Mandant"
        verbose_name_plural = "Kundendaten pro Mandant"
        constraints = [models.UniqueConstraint(fields=["kunde", "mandant"], name="kunde_mandant_eindeutig")]

    def __str__(self):
        return f"{self.kunde.nummer} @ {self.mandant}"


class Anschriftart(models.TextChoices):
    RECHNUNG = "rechnung", "Rechnungsanschrift"
    VERSAND = "versand", "Versandanschrift"


class Kundenanschrift(Zeitstempel):
    kunde = models.ForeignKey(Kunde, on_delete=models.CASCADE, related_name="anschriften")
    art = models.CharField("Art", max_length=10, choices=Anschriftart.choices)
    standard = models.BooleanField("Standard", default=False)
    name1 = models.CharField("Name 1", max_length=150)
    name2 = models.CharField("Name 2", max_length=150, blank=True)
    name3 = models.CharField("Name 3", max_length=150, blank=True)
    strasse = models.CharField("Straße", max_length=200)
    plz = models.CharField("PLZ", max_length=10)
    ort = models.CharField("Ort", max_length=100)
    land = models.CharField("Land (ISO)", max_length=2, default="DE")

    class Meta:
        verbose_name = "Anschrift"
        verbose_name_plural = "Anschriften"
        ordering = ["art", "-standard", "name1"]

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if self.standard:  # nur eine Standardanschrift je Art
            Kundenanschrift.objects.filter(kunde=self.kunde, art=self.art, standard=True) \
                .exclude(pk=self.pk).update(standard=False)

    def __str__(self):
        return f"{self.get_art_display()}: {self.name1}, {self.plz} {self.ort}"


class Ansprechpartner(Zeitstempel):
    """Ansprechpartner eines Kunden (gemeinsam) oder eines Lieferanten (pro Mandant)."""

    kunde = models.ForeignKey(Kunde, on_delete=models.CASCADE, null=True, blank=True,
                              related_name="ansprechpartner")
    lieferant = models.ForeignKey("Lieferant", on_delete=models.CASCADE, null=True, blank=True,
                                  related_name="ansprechpartner")
    mandant = models.ForeignKey(Mandant, on_delete=models.PROTECT, null=True, blank=True,
                                help_text="Nur bei Lieferanten: für welchen Mandanten.")
    anrede = models.CharField("Anrede", max_length=20, blank=True)
    vorname = models.CharField("Vorname", max_length=100, blank=True)
    nachname = models.CharField("Nachname", max_length=100)
    abteilung = models.CharField("Abteilung / Position", max_length=100, blank=True)
    email = models.EmailField("E-Mail", blank=True)
    telefon = models.CharField("Telefon", max_length=50, blank=True)
    durchwahl = models.CharField("Durchwahl", max_length=20, blank=True)
    mobil = models.CharField("Mobil", max_length=50, blank=True)
    hauptkontakt = models.BooleanField("Hauptkontakt", default=False)
    # Onlineshop-Zugang: Konto wird in Shopware angelegt, Passwort setzt der Kunde per Link.
    shop_zugang = models.BooleanField("Onlineshop-Zugang", default=False)
    shopware_kunden_id = models.CharField("Shopware-Kunden-ID", max_length=64, blank=True, editable=False)

    class Meta:
        verbose_name = "Ansprechpartner"
        verbose_name_plural = "Ansprechpartner"
        ordering = ["-hauptkontakt", "nachname"]
        constraints = [
            models.CheckConstraint(
                condition=(models.Q(kunde__isnull=False, lieferant__isnull=True)
                           | models.Q(kunde__isnull=True, lieferant__isnull=False)),
                name="ansprechpartner_genau_ein_partner",
            )
        ]

    def __str__(self):
        return f"{self.vorname} {self.nachname}".strip()


# ---------------------------------------------------------------------------
# Lieferanten
# ---------------------------------------------------------------------------

class Lieferant(Partner):
    nummer = models.CharField("Lieferantennummer", max_length=20, unique=True)
    kurzname = models.CharField("Kurzname", max_length=30, blank=True,
                                help_text="Steht in Suchlisten neben den Preislistenartikeln, z. B. API, Kosatec, Ingram.")
    mindestbestellwert = models.DecimalField("Mindestbestellwert netto", null=True, blank=True, **GELD)
    versandkosten = models.DecimalField("Versandkosten netto je Bestellung", null=True, blank=True, **GELD)
    versandkostenfrei_ab = models.DecimalField("versandkostenfrei ab netto", null=True, blank=True, **GELD)
    lieferzeit_tage = models.PositiveSmallIntegerField("Lieferzeit (Tage)", null=True, blank=True)

    class Meta:
        verbose_name = "Lieferant"
        verbose_name_plural = "Lieferanten"
        ordering = ["nummer"]

    @property
    def kurz(self) -> str:
        """Kurzname für Listen; ohne Eintrag das erste Wort des Namens."""
        return self.kurzname or (self.name1.split() or [self.nummer])[0]


class LieferantMandant(Zeitstempel):
    """Pro Mandant: eigene Kundennummer beim Lieferanten."""

    lieferant = models.ForeignKey(Lieferant, on_delete=models.CASCADE, related_name="mandantendaten")
    mandant = models.ForeignKey(Mandant, on_delete=models.PROTECT)
    unsere_kundennummer = models.CharField("unsere Kundennummer", max_length=50, blank=True)

    class Meta:
        verbose_name = "Lieferantendaten pro Mandant"
        verbose_name_plural = "Lieferantendaten pro Mandant"
        constraints = [models.UniqueConstraint(fields=["lieferant", "mandant"], name="lieferant_mandant_eindeutig")]

    def __str__(self):
        return f"{self.lieferant.nummer} @ {self.mandant}"


# ---------------------------------------------------------------------------
# Artikel
# ---------------------------------------------------------------------------

class Artikeltyp(models.TextChoices):
    A = "A", "A – Lagerartikel"
    P = "P", "P – Lieferantenprodukt"
    L = "L", "L – Leistung"
    G = "G", "G – Gruppe"


class EkHerkunft(models.TextChoices):
    MANUELL = "manuell", "manuell"
    IMPORT = "import", "Import"
    WARENEINGANG = "wareneingang", "Wareneingang"


class Artikel(Zeitstempel):
    nummer = models.CharField("Artikelnummer", max_length=40, unique=True)
    typ = models.CharField("Typ", max_length=1, choices=Artikeltyp.choices, default=Artikeltyp.A)
    bezeichnung = models.CharField("Bezeichnung", max_length=255, db_index=True)
    bezeichnung_erweitert = models.TextField(
        "erweiterte Bezeichnung", blank=True,
        help_text="Wird zugleich als Langbeschreibung im Shop verwendet.")
    kategorie = models.ForeignKey(Kategorie, on_delete=models.PROTECT, null=True, blank=True)
    leistungskategorie = models.ForeignKey(Leistungskategorie, on_delete=models.PROTECT, null=True, blank=True, related_name="artikel",
                                           verbose_name="Leistungskategorie")
    kosten = models.DecimalField("Kosten (EK) netto", null=True, blank=True, validators=[MinValueValidator(0)],
                                 help_text="Eigener Aufwand je Einheit; zählt als EK für die Marge, steht nie auf dem Beleg.", **GELD)
    hersteller = models.ForeignKey(Hersteller, on_delete=models.PROTECT, null=True, blank=True)
    herstellernummer = models.CharField("Herstellernummer", max_length=100, blank=True, db_index=True)
    ean = models.CharField("EAN", max_length=14, blank=True, db_index=True)
    laenge_cm = models.DecimalField("Länge (cm)", max_digits=8, decimal_places=2, null=True, blank=True)
    breite_cm = models.DecimalField("Breite (cm)", max_digits=8, decimal_places=2, null=True, blank=True)
    hoehe_cm = models.DecimalField("Höhe (cm)", max_digits=8, decimal_places=2, null=True, blank=True)
    gewicht_kg = models.DecimalField("Gewicht (kg)", max_digits=9, decimal_places=3, null=True, blank=True)
    garantie_monate = models.PositiveSmallIntegerField("Garantie (Monate)", null=True, blank=True)
    nicht_aktiv = models.BooleanField("nicht aktiv", default=False)
    mengeneinheit = models.CharField("Mengeneinheit", max_length=20, default="Stück")
    eol_datum = models.DateField("EOL-Datum (Auslauf)", null=True, blank=True)
    nachfolger = models.ForeignKey("self", on_delete=models.SET_NULL, null=True, blank=True, related_name="vorgaenger",
                                   verbose_name="Nachfolger (ersetzen durch)")
    sperrgut = models.BooleanField("Sperrgut", default=False)
    zolltarifnummer = models.CharField("Zolltarifnummer", max_length=20, blank=True)
    esd_key = models.BooleanField("ESD-Key-Artikel (Lizenzschlüssel)", default=False,
                                  help_text="Beim Verkauf wird automatisch ein Key aus dem Key-Bestand zugeordnet.")
    steuersatz = models.ForeignKey(Steuersatz, on_delete=models.PROTECT)
    # Preise (netto). Leer = aus Aufschlag berechnet.
    # übernommener EK: Preisimport (Zeitplan „Günstigster EK“, Preis-Assistent) bzw. Altbestand aus Mention; der Wareneingang ändert ihn nicht
    durchschnitts_ek = models.DecimalField("EK netto (Import)", null=True, blank=True, editable=False, **EK_GELD)
    ek_manuell = models.DecimalField("EK-Preis netto", null=True, blank=True, validators=[MinValueValidator(0)],
                                     help_text="Basis, solange es keinen Import-EK aus einer Preisliste gibt.", **GELD)
    ek_herkunft = models.CharField("EK-Herkunft", max_length=12, choices=EkHerkunft.choices, blank=True, editable=False)
    ek_geaendert_am = models.DateTimeField("EK geändert am", null=True, blank=True, editable=False)

    def save(self, *args, **kwargs):
        if self.typ == Artikeltyp.L:  # Leistung: keine Maße, kein Gewicht, kein Versand, nicht im Shop
            self.laenge_cm = self.breite_cm = self.hoehe_cm = self.gewicht_kg = None
            self.sperrgut, self.esd_key, self.online = False, False, False
            self.zolltarifnummer = ""
            self.kategorie = None  # Leistungen haben eigene Kategorien
        else:
            self.leistungskategorie, self.kosten = None, None
        if self.typ != Artikeltyp.A:  # Gruppen rechnen aus den Komponenten, Leistungen haben Kosten
            self.ek_manuell = None
        super().save(*args, **kwargs)

    def ek_datum(self, quelle):
        """Datum der letzten EK-Änderung, nur wenn es zur angezeigten Quelle passt (sonst stünde ein fremdes Datum daneben)."""
        passend = {"manuell": EkHerkunft.MANUELL, "Wareneingang": EkHerkunft.WARENEINGANG, "Preisimport": EkHerkunft.IMPORT}
        return self.ek_geaendert_am if passend.get(quelle) == self.ek_herkunft else None

    def ek_herkunft_setzen(self, herkunft):
        """Merkt, wer den EK zuletzt gesetzt hat (manuell, Import; „Wareneingang“ nur noch bei Altdaten), mit Zeitpunkt."""
        from django.utils import timezone
        self.ek_herkunft, self.ek_geaendert_am = herkunft, timezone.now()

    @property
    def ek_errechnet(self):
        return _ek_quelle(self)[0]

    vk1 = models.DecimalField("VK1 netto", null=True, blank=True, **GELD)
    vk2 = models.DecimalField("VK2 netto", null=True, blank=True, **GELD)
    vk3 = models.DecimalField("VK3 netto", null=True, blank=True, **GELD)
    aufschlag_vk1 = models.DecimalField("Aufschlag VK1 %", max_digits=6, decimal_places=2, null=True, blank=True)
    aufschlag_vk2 = models.DecimalField("Aufschlag VK2 %", max_digits=6, decimal_places=2, null=True, blank=True)
    aufschlag_vk3 = models.DecimalField("Aufschlag VK3 %", max_digits=6, decimal_places=2, null=True, blank=True)
    # Online (eine Checkbox für Shop, eBay, Kaufland)
    online = models.BooleanField("online", default=False)
    kurzbeschreibung = models.TextField("Kurzbeschreibung", blank=True)
    keywords = models.TextField("Keywords", blank=True)
    meta_titel = models.TextField("Metatitel", blank=True)
    meta_description = models.TextField("Meta-Description", blank=True)
    meta_keywords = models.TextField("Meta-Keywords", blank=True)

    class Meta:
        verbose_name = "Artikel"
        verbose_name_plural = "Artikel"
        ordering = ["nummer"]

    def __str__(self):
        return f"{self.nummer} – {self.bezeichnung}"


class Leistungsartikel(Artikel):
    """Sicht auf die Artikel vom Typ L für die Verwaltung."""

    class Meta:
        proxy = True
        verbose_name = "Leistungsartikel"
        verbose_name_plural = "Leistungsartikel"


class ArtikelMandant(Zeitstempel):
    """Pro Mandant: letzter EK (Bestände folgen im Lagermodul)."""

    artikel = models.ForeignKey(Artikel, on_delete=models.CASCADE, related_name="mandantendaten")
    mandant = models.ForeignKey(Mandant, on_delete=models.PROTECT)
    letzter_ek = models.DecimalField("letzter EK", null=True, blank=True, **EK_GELD)
    letzter_ek_datum = models.DateField("letzter EK am", null=True, blank=True)
    lagerplatz = models.CharField("Lagerplatz (Regal/Fach)", max_length=40, blank=True)
    mindestbestand = models.PositiveIntegerField("Mindestbestand", null=True, blank=True)
    maximalbestand = models.PositiveIntegerField("Maximalbestand", null=True, blank=True)

    class Meta:
        verbose_name = "Artikeldaten pro Mandant"
        verbose_name_plural = "Artikeldaten pro Mandant"
        constraints = [models.UniqueConstraint(fields=["artikel", "mandant"], name="artikel_mandant_eindeutig")]


MAX_BILDER = 10
MAX_BILD_BYTES = 10 * 1024 * 1024
BILD_FORMATE = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}
VORSCHAU_KANTE = 320  # längste Kante der Vorschaubilder in Pixel


def _endung(dateiname, vorgabe=".jpg"):
    ende = os.path.splitext(dateiname)[1].lower()
    return ende if ende in (".jpg", ".jpeg", ".png", ".webp") else vorgabe


def bild_pfad(instanz, dateiname):
    """Eindeutiger Dateiname unter MEDIA_ROOT/artikel/<artikel-id>/."""
    return f"artikel/{instanz.artikel_id}/{uuid.uuid4().hex}{_endung(dateiname)}"


def vorschau_pfad(instanz, dateiname):
    return f"artikel/{instanz.artikel_id}/vorschau/{uuid.uuid4().hex}{_endung(dateiname)}"


class ArtikelBild(Zeitstempel):
    """Bis zu zehn Bilder je Artikel (gemeinsame Stammdaten, mandantenübergreifend). Position 1 ist das Hauptbild;
    Reihenfolge und Hauptbild sind die Grundlage für die spätere Übertragung an die Shops."""

    artikel = models.ForeignKey(Artikel, on_delete=models.CASCADE, related_name="bilder")
    bild = models.ImageField("Bild", upload_to=bild_pfad, max_length=255)
    vorschau = models.ImageField("Vorschaubild", upload_to=vorschau_pfad, max_length=255, blank=True, editable=False)
    position = models.PositiveSmallIntegerField(
        "Position", default=1, validators=[MinValueValidator(1), MaxValueValidator(MAX_BILDER)],
        help_text="1 = Hauptbild")
    alt_text = models.CharField("Alt-Text", max_length=150, blank=True, help_text="Beschreibung für Shop und Suchmaschinen.")

    class Meta:
        verbose_name = "Artikelbild"
        verbose_name_plural = "Artikelbilder"
        ordering = ["position"]
        constraints = [models.UniqueConstraint(fields=["artikel", "position"], name="bildposition_eindeutig")]

    def save(self, *args, **kwargs):
        neu = self._state.adding
        super().save(*args, **kwargs)
        if neu and self.bild and not self.vorschau:
            try:
                self.vorschau_erzeugen()
            except (OSError, ValueError):
                # Fehlende oder nicht lesbare Datei: Bild bleibt gespeichert, Vorschau fällt auf das Original zurück.
                logging.getLogger(__name__).warning("Keine Vorschau für %s erzeugt", self.bild.name, exc_info=True)

    @property
    def vorschau_url(self):
        """Verkleinertes Bild; ältere Bilder ohne Vorschau liefern das Original."""
        return (self.vorschau or self.bild).url

    def vorschau_erzeugen(self):
        from io import BytesIO

        from django.core.files.base import ContentFile
        from PIL import Image, ImageOps
        self.bild.open("rb")
        try:
            with Image.open(self.bild) as roh:
                bild = ImageOps.exif_transpose(roh)
                bild.thumbnail((VORSCHAU_KANTE, VORSCHAU_KANTE))
                transparent = bild.mode in ("RGBA", "LA") or "transparency" in bild.info
                bild = bild.convert("RGBA" if transparent else "RGB")
                puffer = BytesIO()
                bild.save(puffer, "PNG" if transparent else "JPEG", **({} if transparent else {"quality": 85}))
        finally:
            self.bild.close()
        self.vorschau.save("vorschau.png" if transparent else "vorschau.jpg", ContentFile(puffer.getvalue()), save=False)
        super().save(update_fields=["vorschau"])


@receiver(post_delete, sender=ArtikelBild)
def bild_dateien_entfernen(sender, instance, **kwargs):
    """Beim Löschen (auch über den Artikel) verschwinden Original und Vorschau von der Platte."""
    for datei in (instance.bild, instance.vorschau):
        if datei:
            datei.delete(save=False)


class ArtikelLieferant(Zeitstempel):
    """Welcher Lieferant führt den Artikel – aus Preisliste, verknüpft über Herstellernummer."""

    artikel = models.ForeignKey(Artikel, on_delete=models.CASCADE, related_name="lieferanten")
    lieferant = models.ForeignKey(Lieferant, on_delete=models.PROTECT, related_name="artikel")
    bestellnummer = models.CharField("Bestellnummer des Lieferanten", max_length=100)
    ek = models.DecimalField("EK netto", null=True, blank=True, **EK_GELD)
    bestand = models.IntegerField("Bestand beim Lieferanten", null=True, blank=True)
    lieferzeit_tage = models.PositiveSmallIntegerField("Lieferzeit (Tage)", null=True, blank=True)
    preisliste = models.CharField("aus Preisliste", max_length=150, blank=True)
    aktualisiert_am = models.DateTimeField("Preisliste eingelesen am", null=True, blank=True)

    class Meta:
        verbose_name = "Lieferant zum Artikel"
        verbose_name_plural = "Lieferanten zum Artikel"
        ordering = ["ek"]
        constraints = [models.UniqueConstraint(fields=["lieferant", "bestellnummer"], name="lieferant_bestellnr_eindeutig")]

    def __str__(self):
        return f"{self.lieferant.name1}: {self.bestellnummer}"


def keine_leistung(artikel):
    """Leistungen (Typ L) sind keine Ware: kein Lager, keine Bestellung, kein Wareneingang."""
    if artikel.typ == Artikeltyp.L:
        raise ValueError(f"{artikel.nummer} ist eine Leistung – dafür gibt es weder Lagerbestand noch Bestellung oder Wareneingang.")


def _ek_quelle(artikel, mit_kosten=True):
    """(EK, Quelle): maßgebend ist der Import-EK – erst der übernommene (Zeitplan „Günstigster EK“, Preis-Assistent), sonst der
    günstigste EK aus den Lieferantenpreislisten; der Wareneingang ändert den EK nicht (README 18). Ohne Import gilt der
    eingetragene EK-Preis (manuell), erst danach ein alter Wareneingangs-EK (Altdaten ohne Preisliste); bei Gruppenartikeln
    (Typ G) die Summe der Komponenten, bei Leistungen (Typ L) die eingetragenen Kosten.
    mit_kosten=False lässt Leistungskosten weg: Sie zählen für die Marge, aber nicht als Grundlage für Aufschläge."""
    if artikel.typ == Artikeltyp.L:
        return (artikel.kosten, "Kosten der Leistung") if artikel.kosten is not None else (None, "")
    if artikel.typ == Artikeltyp.G and artikel.pk:
        summe, quellen = Decimal("0"), set()
        for k in artikel.komponenten.select_related("komponente"):
            if k.komponente.typ == Artikeltyp.L and (k.komponente.kosten is None or not mit_kosten):  # zählt mit 0 € EK
                quellen.add("Leistung")
                continue
            ek, quelle = _ek_quelle(k.komponente)
            if ek is None:
                return None, ""
            summe += ek * k.menge
            quellen.add(quelle)
        if not quellen:
            return None, ""
        return summe, "Summe der Komponenten"
    if _uebernommener_ek(artikel):  # vom Zeitplan „Günstigster EK“ bzw. Preis-Assistenten übernommen (Altdaten: Mention)
        quelle = "Preisimport" if artikel.ek_herkunft == EkHerkunft.IMPORT else "Durchschnitts-EK"
        return artikel.durchschnitts_ek, quelle  # ohne bekannte Herkunft (z. B. aus Mention übernommen) neutral benannt
    if artikel.pk:
        al = artikel.lieferanten.filter(ek__gt=0).order_by("ek").select_related("lieferant").first()
        if al:
            return al.ek, f"Preisliste {al.lieferant.name1}"
    return _ek_ohne_import(artikel)


def _uebernommener_ek(artikel):
    """Übernommener Import-EK; ein alter Ø-EK aus dem Wareneingang zählt nicht dazu, er ist nur noch letzter Ersatz."""
    return bool(artikel.durchschnitts_ek) and artikel.ek_herkunft != EkHerkunft.WARENEINGANG


def _ek_ohne_import(artikel):
    if artikel.ek_manuell is not None:
        return artikel.ek_manuell, "manuell"
    if artikel.durchschnitts_ek:  # Altdaten: Ø-EK aus einem Wareneingang vor der Regel „Import-EK ist maßgebend“
        return artikel.durchschnitts_ek, "Wareneingang"
    return None, ""


def ek_je_artikel(artikel_liste):
    """{pk: (EK, Quelle)} für eine Artikelliste mit einer Abfrage für die Preislisten (Typ A); Gruppen und Leistungen einzeln."""
    artikel_liste = list(artikel_liste)
    guenstigst = {}
    for al in ArtikelLieferant.objects.filter(artikel__in=[a.pk for a in artikel_liste if a.typ == Artikeltyp.A], ek__gt=0) \
            .select_related("lieferant").order_by("artikel_id", "ek"):
        guenstigst.setdefault(al.artikel_id, al)
    ergebnis = {}
    for a in artikel_liste:
        if a.typ != Artikeltyp.A or _uebernommener_ek(a):
            ergebnis[a.pk] = _ek_quelle(a)
        elif a.pk in guenstigst:
            ergebnis[a.pk] = (guenstigst[a.pk].ek, f"Preisliste {guenstigst[a.pk].lieferant.name1}")
        else:
            ergebnis[a.pk] = _ek_ohne_import(a)
    return ergebnis


class GruppenKomponente(Zeitstempel):
    """Standardzusammensetzung eines G-Artikels (Gruppe); im Beleg frei änderbar."""

    gruppe = models.ForeignKey(Artikel, on_delete=models.CASCADE, related_name="komponenten",
                               limit_choices_to={"typ": Artikeltyp.G})
    komponente = models.ForeignKey(Artikel, on_delete=models.PROTECT, related_name="in_gruppen")
    menge = models.DecimalField("Menge", max_digits=10, decimal_places=3, default=Decimal("1"))

    class Meta:
        verbose_name = "Komponente"
        verbose_name_plural = "Komponenten"
        constraints = [models.UniqueConstraint(fields=["gruppe", "komponente"], name="gruppenkomponente_eindeutig")]

    def clean(self):
        from django.core.exceptions import ValidationError
        if self.gruppe_id and self.gruppe.typ != Artikeltyp.G:
            raise ValidationError("Komponenten gibt es nur bei Artikeln vom Typ G.")
        if self.komponente_id and self.komponente.typ == Artikeltyp.G:
            raise ValidationError("Eine Gruppe darf keine weitere Gruppe enthalten.")

    def __str__(self):
        return f"{self.gruppe.nummer}: {self.menge:f} × {self.komponente.nummer}"


class PreisVorgabe(models.Model):
    """Globale Aufschläge auf den EK je Preisstufe (gelten, wenn weder Artikel noch Kategorie einen eigenen haben)."""

    aufschlag_vk1 = models.DecimalField("Aufschlag VK1 %", max_digits=6, decimal_places=2, null=True, blank=True)
    aufschlag_vk2 = models.DecimalField("Aufschlag VK2 %", max_digits=6, decimal_places=2, null=True, blank=True)
    aufschlag_vk3 = models.DecimalField("Aufschlag VK3 %", max_digits=6, decimal_places=2, null=True, blank=True)

    class Meta:
        verbose_name = "Globale Preisvorgabe"
        verbose_name_plural = "Globale Preisvorgabe"

    def save(self, *args, **kwargs):
        if not self.pk and PreisVorgabe.objects.exists():
            raise ValueError("Es gibt nur eine globale Preisvorgabe – bitte die vorhandene bearbeiten.")
        super().save(*args, **kwargs)

    def __str__(self):
        return "Globale Aufschläge"


class Kundenpreis(Zeitstempel):
    """Sonderpreis (netto) eines Kunden für einen Artikel, optional befristet."""

    kunde = models.ForeignKey(Kunde, on_delete=models.CASCADE, related_name="sonderpreise")
    artikel = models.ForeignKey(Artikel, on_delete=models.CASCADE, related_name="kundenpreise")
    preis = models.DecimalField("Preis netto", **GELD)
    gueltig_ab = models.DateField("gültig ab", null=True, blank=True)
    gueltig_bis = models.DateField("gültig bis", null=True, blank=True)
    bemerkung = models.CharField("Bemerkung", max_length=200, blank=True)

    class Meta:
        verbose_name = "Kundensonderpreis"
        verbose_name_plural = "Kundensonderpreise"
        ordering = ["kunde", "artikel"]
        constraints = [models.UniqueConstraint(fields=["kunde", "artikel"], name="kundenpreis_eindeutig")]

    def __str__(self):
        return f"{self.kunde.nummer} / {self.artikel.nummer}: {self.preis}"


class Preislistenartikel(models.Model):
    """Artikel aus den Preislisten der Lieferanten (Typ P) – eigene Tabelle, getrennt von den eigenen Artikeln (Typ A).
    Alle Preislistenimporte landen hier; der Lieferant steht dabei, damit API, Kosatec, Ingram usw. unterscheidbar sind.
    Beim ersten Wareneingang entsteht daraus ein eigener Artikel (Typ A), der hier verknüpft wird."""

    lieferant = models.ForeignKey(Lieferant, on_delete=models.PROTECT, related_name="preislistenartikel")
    bestellnummer = models.CharField("Bestellnummer des Lieferanten (SKU)", max_length=100)
    herstellernummer = models.CharField("Herstellernummer (MSKU)", max_length=100, blank=True, db_index=True)
    ean = models.CharField("EAN", max_length=14, blank=True, db_index=True)
    bezeichnung = models.CharField("Bezeichnung", max_length=255)
    hersteller = models.CharField("Hersteller", max_length=150, blank=True)
    kategorie = models.CharField("Kategorie des Lieferanten", max_length=150, blank=True)
    ek = models.DecimalField("EK netto", **EK_GELD)
    bestand = models.IntegerField("Bestand beim Lieferanten", null=True, blank=True)
    lieferzeit_tage = models.PositiveSmallIntegerField("Lieferzeit (Tage)", null=True, blank=True)
    gewicht_kg = models.DecimalField("Gewicht (kg)", max_digits=9, decimal_places=3, null=True, blank=True)
    artikel = models.ForeignKey(Artikel, on_delete=models.SET_NULL, null=True, blank=True, related_name="preislisteneintraege",
                                verbose_name="verknüpfter eigener Artikel")
    preisliste = models.CharField("aus Preisliste", max_length=150, blank=True)
    aktualisiert_am = models.DateTimeField("zuletzt aktualisiert", null=True, blank=True)

    class Meta:
        verbose_name = "Preislistenartikel"
        verbose_name_plural = "Preislistenartikel"
        ordering = ["lieferant", "bezeichnung"]
        constraints = [models.UniqueConstraint(fields=["lieferant", "bestellnummer"], name="preislistenartikel_eindeutig")]
        indexes = [models.Index(fields=["bezeichnung"])]

    def __str__(self):
        return f"[{self.lieferant.kurz}] {self.herstellernummer or self.bestellnummer} – {self.bezeichnung}"

    @property
    def nummer(self) -> str:
        """Nummer für die Anzeige: MSKU, sonst Bestellnummer des Lieferanten."""
        return self.herstellernummer or self.bestellnummer


# ---------------------------------------------------------------------------
# Laufende Nummern der Stammdaten
# ---------------------------------------------------------------------------

class Stammnummer(models.Model):
    """Zähler für Nummern, die beide Mandanten teilen. Belegte Nummern werden übersprungen, auch eingetippte oder importierte."""

    class Art(models.TextChoices):
        ARTIKEL = "A", "Artikel (Typ A)"
        LEISTUNG = "L", "Leistungen (Typ L)"
        GRUPPE = "G", "Gruppenartikel (Typ G)"
        KUNDE = "K", "Kunden"
        LIEFERANT = "LF", "Lieferanten"

    HOECHSTWERT = 10 ** 18 - 1

    art = models.CharField("Art", max_length=2, choices=Art.choices, unique=True)
    letzte_nummer = models.PositiveBigIntegerField("zuletzt vergeben", default=0)

    class Meta:
        verbose_name = "Stammnummer"
        verbose_name_plural = "Stammnummern"

    def __str__(self):
        return f"{self.get_art_display()}: {self.letzte_nummer}"

    @classmethod
    def vorschlag(cls, art: str) -> str:
        """Nächste freie Nummer, ohne sie zu verbrauchen: Eine geöffnete und wieder abgebrochene Maske kostet keine Nummer."""
        modell = {cls.Art.KUNDE: Kunde, cls.Art.LIEFERANT: Lieferant}.get(art, Artikel)
        belegt = set(modell.objects.values_list("nummer", flat=True))
        zahl = (cls.objects.filter(art=art).values_list("letzte_nummer", flat=True).first() or 0) + 1
        while str(zahl) in belegt:
            zahl += 1
        return str(zahl)

    @classmethod
    def belegen(cls, art: str, nummer: str):
        """Vor dem Speichern aufrufen: Der Zähler rückt nur bei der vorgeschlagenen Nummer vor, eine eingetippte lässt ihn stehen."""
        with transaction.atomic():
            zaehler, _ = cls.objects.select_for_update().get_or_create(art=art)
            if str(nummer) == cls.vorschlag(art):
                zaehler.letzte_nummer = int(nummer)
                zaehler.save(update_fields=["letzte_nummer"])

    @classmethod
    def naechste(cls, art: str) -> str:
        """Vergibt sofort die nächste Nummer, für Abläufe ohne Maske."""
        with transaction.atomic():
            zaehler, _ = cls.objects.select_for_update().get_or_create(art=art)
            nummer = cls.vorschlag(art)
            zaehler.letzte_nummer = int(nummer)
            zaehler.save(update_fields=["letzte_nummer"])
        return nummer

    @classmethod
    def setzen(cls, art: str, naechste: int):
        if not 1 <= naechste <= cls.HOECHSTWERT:
            raise ValueError("Die Nummer muss größer als 0 und höchstens 18-stellig sein.")
        with transaction.atomic():
            zaehler, _ = cls.objects.select_for_update().get_or_create(art=art)
            zaehler.letzte_nummer = naechste - 1
            zaehler.save(update_fields=["letzte_nummer"])


# ---------------------------------------------------------------------------
# USt-ID-Prüfung
# ---------------------------------------------------------------------------

class UstIdPruefung(models.Model):
    """Nachweis einer Online-Prüfung mit den tatsächlich gesendeten Daten; bleibt erhalten, auch wenn Kunde oder Lieferant gelöscht wird."""

    ABGLEICH = {"A": "stimmt", "B": "weicht ab", "C": "nicht angefragt", "D": "vom EU-Land nicht mitgeteilt"}

    class Dienst(models.TextChoices):
        BZST = "bzst", "BZSt"
        VIES = "vies", "VIES"

    mandant = models.ForeignKey(Mandant, on_delete=models.PROTECT)
    kunde = models.ForeignKey(Kunde, on_delete=models.SET_NULL, null=True, blank=True, related_name="ust_pruefungen")
    lieferant = models.ForeignKey(Lieferant, on_delete=models.SET_NULL, null=True, blank=True, related_name="ust_pruefungen")
    zeitpunkt = models.DateTimeField("geprüft am", auto_now_add=True, db_index=True)
    benutzer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    ust_id = models.CharField("USt-ID", max_length=20)
    anfragende_ust_id = models.CharField("eigene USt-ID", max_length=20, blank=True)
    firmenname = models.CharField("angefragter Name", max_length=150, blank=True)
    strasse = models.CharField("angefragte Straße", max_length=200, blank=True)
    plz = models.CharField("angefragte PLZ", max_length=10, blank=True)
    ort = models.CharField("angefragter Ort", max_length=100, blank=True)
    dienst = models.CharField("Dienst", max_length=4, choices=Dienst.choices)
    gueltig = models.BooleanField("gültig")
    status = models.CharField("Status des Dienstes", max_length=20)
    meldung = models.CharField("Meldung", max_length=200)
    referenz = models.CharField("Abfragenummer", max_length=100, blank=True)
    erg_name = models.CharField("Name", max_length=1, blank=True)
    erg_strasse = models.CharField("Straße", max_length=1, blank=True)
    erg_plz = models.CharField("PLZ", max_length=1, blank=True)
    erg_ort = models.CharField("Ort", max_length=1, blank=True)
    auskunft = models.TextField("Auskunft des Dienstes", blank=True)

    class Meta:
        verbose_name = "USt-ID-Prüfung"
        verbose_name_plural = "USt-ID-Prüfungen"
        ordering = ["-zeitpunkt", "-pk"]

    def save(self, *args, **kwargs):
        if self.pk:
            raise PermissionError("Der Nachweis einer USt-ID-Prüfung ist unveränderlich.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise PermissionError("Der Nachweis einer USt-ID-Prüfung ist unveränderlich.")

    def __str__(self):
        return f"{self.ust_id} – {'gültig' if self.gueltig else 'nicht gültig'}"

    @property
    def abgleich(self) -> list:
        """Vergleich der angefragten Daten als Text, nur bei der qualifizierten Abfrage."""
        felder = (("Name", self.erg_name), ("Straße", self.erg_strasse), ("PLZ", self.erg_plz), ("Ort", self.erg_ort))
        return [f"{name}: {self.ABGLEICH.get(wert, wert)}" for name, wert in felder if wert and wert != "C"]

    @property
    def abweichung(self) -> bool:
        return "B" in (self.erg_name, self.erg_strasse, self.erg_plz, self.erg_ort)
