from django.contrib import admin
from django.core.exceptions import ValidationError
from django.forms import BaseInlineFormSet

from .models import (Abo, AboPosition, Beleg, Briefprodukt, MailVersand, Mailvorlage, Mengenregel, Nummernkreis, Position,
                     Versandart, Wiedervorlage)


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


class AboPositionFormset(BaseInlineFormSet):
    def clean(self):
        """Mengenregeln rechnen nur halbmonatlich; sonst stünde die Regel da und es gälte still die feste Menge."""
        super().clean()
        for form in self.forms:
            daten = getattr(form, "cleaned_data", None) or {}
            if daten.get("DELETE"):
                continue
            if not self.instance.halbmonatlich and (daten.get("regel", Mengenregel.FEST) != Mengenregel.FEST or daten.get("nur_monatsende")):
                raise ValidationError("Mengenregel und „nur am Monatsende“ gibt es nur beim Rhythmus halbmonatlich.")
            if self.instance.halbmonatlich and daten.get("nur_monatsende") and self.instance.ende and self.instance.ende.day <= 15:
                raise ValidationError("Mit Positionen „nur am Monatsende“ muss das Ende ein Monatsletzter sein, sonst bliebe dieser Monat unberechnet.")


class AboPositionInline(admin.TabularInline):
    model = AboPosition
    formset = AboPositionFormset
    extra = 1
    autocomplete_fields = ["artikel"]
    fields = ["artikel", "bezeichnung", "regel", "menge", "nur_monatsende", "einzelpreis", "rabatt_prozent", "steuer_prozent"]


@admin.register(Abo)
class AboAdmin(admin.ModelAdmin):
    list_display = ["bezeichnung", "kunde", "mandant", "monate", "naechster_termin", "ende", "aktiv", "letzter_lauf"]
    list_filter = ["mandant", "aktiv", "monate"]
    search_fields = ["bezeichnung", "kunde__name1"]
    autocomplete_fields = ["kunde"]
    inlines = [AboPositionInline]

@admin.register(Versandart)
class VersandartAdmin(admin.ModelAdmin):
    list_display = ["name", "dienstleister", "versandkosten", "verpackung", "versicherung", "frachtfrei_ab", "nur_inland", "aktiv"]

@admin.register(Wiedervorlage)
class WiedervorlageAdmin(admin.ModelAdmin):
    list_display = ["faellig", "kunde", "text", "erledigt", "mandant"]
    list_filter = ["mandant", "erledigt"]


@admin.register(Briefprodukt)
class BriefproduktAdmin(admin.ModelAdmin):
    """Porto der Deutschen Post (INTERNETMARKE); bei einer Portoerhöhung den Preis hier anpassen."""

    list_display = ["name", "preis", "max_gewicht_g", "masse", "produktcode", "aktiv"]
    list_editable = ["preis", "aktiv"]
