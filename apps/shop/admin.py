from django.contrib import admin

from .models import Shop, ShopBestellung, SyncProtokoll


@admin.register(Shop)
class ShopAdmin(admin.ModelAdmin):
    list_display = ["name", "mandant", "url", "aktiv", "letzter_bestellimport", "letzter_abgleich"]
    list_filter = ["mandant", "aktiv"]
    fieldsets = [
        ("Shop", {"fields": ["name", "mandant", "url", "aktiv"]}),
        ("Zugang", {"fields": ["client_id"], "description": "Das Geheimnis (Secret Access Key) steht nicht hier, sondern in der .env als SHOP_SECRET_<ID des Shops>."}),
        ("Abgleich", {"fields": ["bestand_puffer", "preisstufe"]}),
        ("Bestellungen", {"fields": ["standard_zahlungsbedingung", "zahlungszuordnung"]}),
    ]


class NurLesen(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(ShopBestellung)
class ShopBestellungAdmin(NurLesen):
    list_display = ["importiert_am", "shop", "bestellnummer", "beleg", "hinweis"]
    list_filter = ["shop"]


@admin.register(SyncProtokoll)
class SyncProtokollAdmin(NurLesen):
    list_display = ["zeitpunkt", "shop", "art", "erfolgreich", "meldung"]
    list_filter = ["shop", "art", "erfolgreich"]
