from django.contrib import admin

from .models import (BestellPosition, Bestellung, Lieferantenrechnung, Rma, Wareneingang, WareneingangPosition)


class BestellPositionInline(admin.TabularInline):
    model = BestellPosition
    extra = 0


@admin.register(Bestellung)
class BestellungAdmin(admin.ModelAdmin):
    list_display = ["nummer", "mandant", "lieferant", "datum", "status"]
    list_filter = ["mandant", "status"]
    search_fields = ["nummer", "lieferant__name1"]
    inlines = [BestellPositionInline]


class WareneingangPositionInline(admin.TabularInline):
    model = WareneingangPosition
    extra = 0


@admin.register(Wareneingang)
class WareneingangAdmin(admin.ModelAdmin):
    list_display = ["nummer", "mandant", "lieferant", "datum", "gebucht"]
    list_filter = ["mandant", "gebucht"]
    inlines = [WareneingangPositionInline]


@admin.register(Lieferantenrechnung)
class LieferantenrechnungAdmin(admin.ModelAdmin):
    list_display = ["rechnungsnummer", "lieferant", "mandant", "datum", "faellig", "brutto", "status"]
    list_filter = ["mandant", "status"]
    search_fields = ["rechnungsnummer", "lieferant__name1"]


@admin.register(Rma)
class RmaAdmin(admin.ModelAdmin):
    list_display = ["nummer", "mandant", "lieferant", "bezeichnung", "status"]
    list_filter = ["mandant", "status"]
