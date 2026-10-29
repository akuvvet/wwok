from django.contrib import admin

from django import forms
from django.utils.html import format_html

from .models import AenderungsProtokoll, Mandant

LOGO_MAX_BYTES = 2 * 1024 * 1024


class MandantForm(forms.ModelForm):
    class Meta:
        model = Mandant
        fields = "__all__"

    def clean_logo(self):
        logo = self.cleaned_data.get("logo")
        if logo and hasattr(logo, "size") and logo.size > LOGO_MAX_BYTES:
            raise forms.ValidationError("Das Logo ist größer als 2 MB.")
        return logo


@admin.register(Mandant)
class MandantAdmin(admin.ModelAdmin):
    form = MandantForm
    list_display = ["kurzname", "firmenname", "ort", "aktiv"]
    readonly_fields = ["logo_vorschau", "angelegt_am", "geaendert_am", "geaendert_von"]
    fieldsets = [
        ("Firma", {"fields": ["kurzname", "firmenname", "logo", "logo_vorschau", "strasse", "plz", "ort", "land", "telefon", "email", "aktiv"]}),
        ("Steuer und Bank", {"fields": ["ust_id", "steuernummer", "iban", "bic", "glaeubiger_id"]}),
        ("E-Mail-Versand", {"fields": ["smtp_host", "smtp_port", "smtp_sicherheit", "smtp_benutzer", "absender_email", "absender_name", "kopie_an"]}),
        ("DATEV und Konten", {"fields": ["datev_berater", "datev_mandant", "konto_erloes_19", "konto_erloes_7", "konto_erloes_0",
                                         "konto_bank", "konto_kasse"]}),
        ("Verlauf", {"fields": ["angelegt_am", "geaendert_am", "geaendert_von"]}),
    ]

    @admin.display(description="Vorschau")
    def logo_vorschau(self, obj):
        if obj and obj.logo:
            return format_html('<img src="{}" style="max-height:80px;max-width:240px;background:#fff;padding:4px;border:1px solid #ccc">', obj.logo.url)
        return "– noch kein Logo hochgeladen –"


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
