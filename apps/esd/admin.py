from django.contrib import admin

from .models import ESDKey, ESDVergabe


@admin.register(ESDKey)
class ESDKeyAdmin(admin.ModelAdmin):
    list_display = ["artikel", "key", "mandant", "max_vergaben", "vergeben", "status"]
    list_filter = ["mandant", "status", "artikel"]
    search_fields = ["key", "artikel__nummer"]


@admin.register(ESDVergabe)
class ESDVergabeAdmin(admin.ModelAdmin):
    """Nur lesen – Rückgaben werden in der ESD-Maske des Belegs entschieden."""

    list_display = ["datum", "key", "kunde", "beleg", "status"]
    list_filter = ["status"]
    search_fields = ["key__key", "kunde__name1"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
