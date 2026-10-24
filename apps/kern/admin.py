from django.contrib import admin

from .models import AenderungsProtokoll, Mandant


@admin.register(Mandant)
class MandantAdmin(admin.ModelAdmin):
    list_display = ["kurzname", "firmenname", "ort", "aktiv"]
    readonly_fields = ["angelegt_am", "geaendert_am", "geaendert_von"]


@admin.register(AenderungsProtokoll)
class AenderungsProtokollAdmin(admin.ModelAdmin):
    """Nur lesen – Einträge sind unveränderlich."""
    list_display = ["zeitpunkt", "benutzer", "tabelle", "datensatz_id", "aktion"]
    list_filter = ["aktion", "tabelle"]
    search_fields = ["datensatz_id"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
