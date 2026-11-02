from django.contrib import admin
from django.urls import reverse
from django.utils.html import format_html

from .models import Shop, ShopBestellung, SyncProtokoll


@admin.register(Shop)
class ShopAdmin(admin.ModelAdmin):
    list_display = ["name", "mandant", "url", "aktiv", "letzter_bestellimport", "letzter_abgleich"]
    list_filter = ["mandant", "aktiv"]
    readonly_fields = ["geheimnis", "senden"]
    fieldsets = [
        ("Shop", {"fields": ["name", "mandant", "url", "aktiv"]}),
        ("Zugang", {"fields": ["client_id", "geheimnis"]}),
        ("Abgleich", {"fields": ["bestand_puffer", "preisstufe", "preisstufe_kunden", "regel_kunden_id", "artikeldaten_takt", "verkaufskanal_id", "senden"]}),
        ("Bestellungen", {"fields": ["standard_zahlungsbedingung", "zahlungszuordnung"]}),
    ]

    @admin.display(description="Secret Access Key")
    def geheimnis(self, obj):
        return f"steht nicht hier, sondern in der .env als {obj.secret_variable}" if obj.pk else "steht nach dem ersten Sichern hier"

    @admin.display(description="Jetzt senden")
    def senden(self, obj):
        return format_html('<a href="{}">Zur Shop-Seite: Bestand, Preise und Artikeldaten senden</a>', reverse("shop:uebersicht"))


class NurLesen(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False  # sonst würde ein erneuter Import die Bestellung doppelt anlegen


@admin.register(ShopBestellung)
class ShopBestellungAdmin(NurLesen):
    list_display = ["importiert_am", "shop", "bestellnummer", "beleg", "hinweis"]
    list_filter = ["shop"]


@admin.register(SyncProtokoll)
class SyncProtokollAdmin(NurLesen):
    list_display = ["zeitpunkt", "shop", "art", "erfolgreich", "meldung"]
    list_filter = ["shop", "art", "erfolgreich"]
