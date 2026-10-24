"""
Shop-Anbindung (Pflichtenheft Abschnitt 13): Shopware 6, je Shop einem Mandanten zugeordnet.
Bestellimport als Auftrag, Bestands- und Preisabgleich (nur verfügbarer Bestand wird gemeldet).
"""
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
    standard_zahlungsbedingung = models.ForeignKey(Zahlungsbedingung, on_delete=models.SET_NULL, null=True, blank=True,
                                                   related_name="+", help_text="Falls die Zahlart nicht zugeordnet ist")
    zahlungszuordnung = models.JSONField(
        "Zuordnung Zahlart → Zahlungsbedingung", default=dict, blank=True,
        help_text='z. B. {"Vorkasse": 8, "PayPal": 10, "Lastschrift": 12}: Name der Zahlart im Shop → Nummer der Zahlungsbedingung')
    letzter_bestellimport = models.DateTimeField(null=True, blank=True, editable=False)
    letzter_abgleich = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta:
        verbose_name = "Shop"
        verbose_name_plural = "Shops"
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.mandant})"

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
    gesendet_am = models.DateTimeField(null=True, blank=True)

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
