from django.contrib import admin

from .models import Reparatur, ReparaturPosition


class PositionInline(admin.TabularInline):
    model = ReparaturPosition
    extra = 0


@admin.register(Reparatur)
class ReparaturAdmin(admin.ModelAdmin):
    list_display = ["nummer", "mandant", "kunde", "geraet", "seriennummer", "status", "angenommen_am"]
    list_filter = ["mandant", "status", "garantie"]
    search_fields = ["nummer", "geraet", "seriennummer", "kunde__name1"]
    inlines = [PositionInline]
