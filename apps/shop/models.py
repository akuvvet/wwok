"""
Shop-Anbindung (Pflichtenheft Abschnitt 13): Shopware 6, je Shop einem Mandanten zugeordnet.
Bestellimport als Beleg Bestellung, Bestands- und Preisabgleich (nur verfügbarer Bestand wird gemeldet).
"""
import re

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from apps.kern.models import Mandant, Zeitstempel
from apps.stammdaten.models import Artikel, Zahlungsbedingung


class Shop(Zeitstempel):
    mandant = models.ForeignKey(Mandant, on_delete=models.PROTECT, related_name="shops")
    name = models.CharField("Name des Shops", max_length=100)
    url = models.URLField("Shop-Adresse", help_text="z. B. https://shop.okaycomputer.de (ohne /api)")
    client_id = models.CharField("Zugangs-ID (Access Key ID)", max_length=200,
                                 help_text="Shopware: Einstellungen → System → Integrationen")
    aktiv = models.BooleanField(default=True)
    bestand_puffer = models.PositiveIntegerField("Sicherheitsbestand", default=0,
                                                 help_text="Dieser Bestand wird nicht an den Shop gemeldet (Schutz vor Doppelverkauf).")
    preisstufe = models.PositiveSmallIntegerField("Preisstufe für den Shop", default=1, choices=[(1, "VK1"), (2, "VK2"), (3, "VK3")])
    preisstufe_kunden = models.PositiveSmallIntegerField(
        "Preisstufe für angemeldete Kunden", default=0, choices=[(0, "kein eigener Preis"), (1, "VK1"), (2, "VK2"), (3, "VK3")],
        help_text="Gilt für Kunden, auf die die Regel unten zutrifft; alle anderen sehen die Preisstufe für den Shop.")
    regel_kunden_id = models.CharField(
        "Regel-ID für angemeldete Kunden", max_length=64, blank=True,
        help_text="ID der Regel aus dem Rule Builder in Shopware (steht in der Adresse, wenn die Regel geöffnet ist).")
    standard_zahlungsbedingung = models.ForeignKey(Zahlungsbedingung, on_delete=models.SET_NULL, null=True, blank=True,
                                                   related_name="+", help_text="Falls die Zahlart nicht zugeordnet ist")
    zahlungszuordnung = models.JSONField(
        "Zuordnung Zahlart → Zahlungsbedingung", default=dict, blank=True,
        help_text='z. B. {"Vorkasse": 8, "PayPal": 10, "Lastschrift": 12}: Name der Zahlart im Shop → Nummer der Zahlungsbedingung')
    letzter_bestellimport = models.DateTimeField(null=True, blank=True, editable=False)
    letzter_abgleich = models.DateTimeField(null=True, blank=True, editable=False)
    artikeldaten_takt = models.PositiveIntegerField(
        "Artikeldaten automatisch senden", default=0, choices=[(0, "nur per Knopf"), (10, "alle 10 Minuten"), (60, "stündlich"), (1440, "täglich")],
        help_text="Texte, Bilder und neue Artikel. Bestand und Preise laufen davon unabhängig bei jedem Abgleich.")
    verkaufskanal_id = models.CharField(
        "Verkaufskanal-ID", max_length=64, blank=True,
        help_text="Nur nötig, wenn der Shop mehrere Verkaufskanäle hat: Dort werden neue Artikel sichtbar geschaltet.")
    letzter_artikelabgleich = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta:
        verbose_name = "Shop"
        verbose_name_plural = "Shops"
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.mandant})"

    def clean(self):
        if self.preisstufe_kunden and not re.fullmatch(r"[0-9a-f]{32}", self.regel_kunden_id.strip()):
            raise ValidationError({"regel_kunden_id": "Für den Preis angemeldeter Kunden braucht Shopware eine Regel: 32 Zeichen, nur 0-9 und a-f."})

    @property
    def secret_variable(self) -> str:
        return f"SHOP_SECRET_{self.pk}"


class ShopBestellung(models.Model):
    """Importierte Shop-Bestellung; verhindert doppelten Import."""

    shop = models.ForeignKey(Shop, on_delete=models.CASCADE, related_name="bestellungen")
    shop_id_extern = models.CharField(max_length=64)
    bestellnummer = models.CharField("Bestellnummer im Shop", max_length=60)
    beleg = models.ForeignKey("verkauf.Beleg", on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    importiert_am = models.DateTimeField(default=timezone.now)
    hinweis = models.CharField(max_length=300, blank=True)

    class Meta:
        verbose_name = "Shop-Bestellung"
        verbose_name_plural = "Shop-Bestellungen"
        ordering = ["-importiert_am"]
        constraints = [models.UniqueConstraint(fields=["shop", "shop_id_extern"], name="shopbestellung_eindeutig")]

    def __str__(self):
        return f"{self.shop.name} {self.bestellnummer}"


class ShopArtikel(models.Model):
    """Zuordnung Artikel ↔ Produkt im Shop und zuletzt gemeldete Werte (es werden nur Änderungen gesendet)."""

    shop = models.ForeignKey(Shop, on_delete=models.CASCADE, related_name="artikel")
    artikel = models.ForeignKey(Artikel, on_delete=models.CASCADE, related_name="shop_zuordnungen")
    produkt_id = models.CharField(max_length=64)
    gemeldeter_bestand = models.IntegerField(null=True, blank=True)
    gemeldeter_preis = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    gemeldeter_kundenpreis = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    gemeldete_regel = models.CharField(max_length=64, blank=True)
    gesendet_am = models.DateTimeField(null=True, blank=True)
    daten_stand = models.CharField(max_length=64, blank=True, help_text="Prüfsumme der zuletzt gesendeten Artikeldaten")
    bild_ids = models.JSONField(default=list, blank=True, help_text="Von wwok gesendete Produktbilder im Shop")
    bilder_stand = models.CharField(max_length=64, blank=True, help_text="Prüfsumme der zuletzt gesendeten Bilder")
    kategorie_id = models.CharField(max_length=64, blank=True, help_text="Von wwok zugeordnete Kategorie im Shop")
    im_shop_aktiv = models.BooleanField(default=True)

    class Meta:
        verbose_name = "Shop-Artikel"
        verbose_name_plural = "Shop-Artikel"
        constraints = [models.UniqueConstraint(fields=["shop", "artikel"], name="shopartikel_eindeutig")]


class SyncProtokoll(models.Model):
    shop = models.ForeignKey(Shop, on_delete=models.CASCADE, related_name="protokolle")
    zeitpunkt = models.DateTimeField(default=timezone.now)
    art = models.CharField(max_length=20)  # bestellungen | abgleich
    erfolgreich = models.BooleanField(default=True)
    meldung = models.CharField(max_length=500)

    class Meta:
        verbose_name = "Shop-Protokoll"
        verbose_name_plural = "Shop-Protokoll"
        ordering = ["-zeitpunkt"]
        indexes = [models.Index(fields=["shop", "-zeitpunkt"], name="syncprotokoll_shop_zeit")]
