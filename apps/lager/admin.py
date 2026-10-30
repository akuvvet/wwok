from django.contrib import admin

from .models import Lagerbewegung, Reparaturbewegung, Seriennummer


@admin.register(Lagerbewegung)
class LagerbewegungAdmin(admin.ModelAdmin):
    """Nur lesen – Korrekturen als Gegenbuchung in der Lagermaske."""

    list_display = ["zeitpunkt", "mandant", "artikel", "art", "menge", "beleg", "benutzer"]
    list_filter = ["mandant", "art"]
    search_fields = ["artikel__nummer", "artikel__bezeichnung", "bemerkung"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Reparaturbewegung)
class ReparaturbewegungAdmin(LagerbewegungAdmin):
    pass


@admin.register(Seriennummer)
class SeriennummerAdmin(admin.ModelAdmin):
    list_display = ["nummer", "artikel", "mandant", "status", "eingang"]
    list_filter = ["mandant", "status"]
    search_fields = ["nummer", "artikel__nummer", "artikel__bezeichnung"]
