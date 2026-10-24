from django.contrib import admin

from .models import Beleg, MailVersand, Mailvorlage, Nummernkreis, Position


class PositionInline(admin.TabularInline):
    model = Position
    fk_name = "beleg"
    extra = 0
    can_delete = False
    readonly_fields = [f.name for f in Position._meta.fields if f.name not in ("id",)]

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Beleg)
class BelegAdmin(admin.ModelAdmin):
    """Belege nur ansehen; erfasst wird in den Verkaufsmasken (GoBD)."""

    list_display = ["nummer", "art", "kunde", "mandant", "belegdatum", "summe_brutto", "abgeschlossen"]
    list_filter = ["mandant", "art", "abgeschlossen"]
    search_fields = ["nummer", "kunde__name1", "kunde__nummer"]
    inlines = [PositionInline]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Mailvorlage)
class MailvorlageAdmin(admin.ModelAdmin):
    list_display = ["mandant", "art", "betreff"]
    list_filter = ["mandant"]


@admin.register(MailVersand)
class MailVersandAdmin(admin.ModelAdmin):
    """Nur lesen – Nachweis über versendete Belege."""

    list_display = ["zeitpunkt", "beleg", "bezug", "an", "erfolgreich", "benutzer"]
    list_filter = ["erfolgreich", "mandant"]
    search_fields = ["an", "beleg__nummer"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Nummernkreis)
class NummernkreisAdmin(admin.ModelAdmin):
    list_display = ["mandant", "art", "jahr", "letzte_nummer"]
    list_filter = ["mandant"]
