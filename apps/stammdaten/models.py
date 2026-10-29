"""
Stammdaten: Kunden, Lieferanten, Artikel – gemäß Pflichtenheft Abschnitt 9.

Gemeinsam für beide Mandanten: Kunden, Lieferanten, Artikel, Kategorien.
Pro Mandant getrennt: Kreditlimit, Kundennummer beim Lieferanten, Lieferanten-
Ansprechpartner, letzter EK.
"""
from decimal import Decimal

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

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


class Artikel(Zeitstempel):
    nummer = models.CharField("Artikelnummer", max_length=40, unique=True)
    typ = models.CharField("Typ", max_length=1, choices=Artikeltyp.choices, default=Artikeltyp.A)
    bezeichnung = models.CharField("Bezeichnung", max_length=255, db_index=True)
    bezeichnung_erweitert = models.TextField(
        "erweiterte Bezeichnung", blank=True,
        help_text="Wird zugleich als Langbeschreibung im Shop verwendet.")
    kategorie = models.ForeignKey(Kategorie, on_delete=models.PROTECT, null=True, blank=True)
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
    durchschnitts_ek = models.DecimalField("Durchschnitts-EK", null=True, blank=True, editable=False, **EK_GELD)
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


class ArtikelBild(Zeitstempel):
    artikel = models.ForeignKey(Artikel, on_delete=models.CASCADE, related_name="bilder")
    bild = models.ImageField("Bild", upload_to="artikel/%Y/%m/")
    position = models.PositiveSmallIntegerField(
        "Position", default=1, validators=[MinValueValidator(1), MaxValueValidator(10)],
        help_text="1 = Hauptbild")

    class Meta:
        verbose_name = "Artikelbild"
        verbose_name_plural = "Artikelbilder"
        ordering = ["position"]
        constraints = [models.UniqueConstraint(fields=["artikel", "position"], name="bildposition_eindeutig")]


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


def _ek_quelle(artikel):
    """(EK, Quelle): Wareneingang (Durchschnitts-EK) hat Vorrang, sonst günstigster EK aus den Lieferantenpreislisten;
    bei Gruppenartikeln (Typ G) die Summe der Komponenten."""
    if artikel.typ == Artikeltyp.G and artikel.pk:
        summe, quellen = Decimal("0"), set()
        for k in artikel.komponenten.select_related("komponente"):
            ek, quelle = _ek_quelle(k.komponente)
            if ek is None:
                return None, ""
            summe += ek * k.menge
            quellen.add(quelle)
        if not quellen:
            return None, ""
        return summe, "Summe der Komponenten"
    if artikel.durchschnitts_ek:
        return artikel.durchschnitts_ek, "Wareneingang"
    if artikel.pk:
        al = artikel.lieferanten.filter(ek__gt=0).order_by("ek").select_related("lieferant").first()
        if al:
            return al.ek, f"Preisliste {al.lieferant.name1}"
    return None, ""


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
