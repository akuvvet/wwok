from django.contrib import admin, messages
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin as BasisUserAdmin

from django import forms
from django.core.exceptions import PermissionDenied
from django.http import HttpResponseRedirect
from django.utils.html import format_html

from .layout import aussehen_zuruecksetzen, hausfarbe
from .models import AenderungsProtokoll, Listenansicht, Mandant

LOGO_MAX_BYTES = 2 * 1024 * 1024


class FarbfeldWidget(forms.TextInput):
    """Farbcode als Text (darf leer bleiben = neutral) mit Farbwähler und „Leeren“ daneben."""

    def render(self, name, value, attrs=None, renderer=None):
        feld = super().render(name, value, {**(attrs or {}), "size": 8, "maxlength": 7, "placeholder": "neutral",
                                            "pattern": "#[0-9a-fA-F]{6}", "data-farbfeld": "1"}, renderer)
        return format_html(
            '<span style="display:inline-flex;gap:8px;align-items:center">{}'
            '<input type="color" value="{}" aria-label="Farbe wählen" style="width:48px;height:32px;padding:2px" '
            'oninput="this.parentNode.querySelector(\'[data-farbfeld]\').value=this.value">'
            '<button type="button" class="button" style="padding:6px 10px" '
            'onclick="this.parentNode.querySelector(\'[data-farbfeld]\').value=\'\'">Leeren</button></span>',
            feld, value or "#ffffff")


class MandantForm(forms.ModelForm):
    class Meta:
        model = Mandant
        fields = "__all__"
        widgets = {"farbe": forms.TextInput(attrs={"type": "color", "style": "width:80px;height:34px;padding:2px"}),
                   "farbe_kopf": FarbfeldWidget, "farbe_leiste": FarbfeldWidget, "farbe_karte": FarbfeldWidget}

    def clean_logo(self):
        logo = self.cleaned_data.get("logo")
        if logo and hasattr(logo, "size") and logo.size > LOGO_MAX_BYTES:
            raise forms.ValidationError("Das Logo ist größer als 2 MB.")
        return logo


@admin.register(Mandant)
class MandantAdmin(admin.ModelAdmin):
    form = MandantForm
    list_display = ["kurzname", "firmenname", "ort", "aktiv"]
    readonly_fields = ["logo_vorschau", "aussehen_zuruecksetzen_knopf", "angelegt_am", "geaendert_am", "geaendert_von"]
    actions = ["aussehen_zuruecksetzen"]

    @admin.display(description="Zurücksetzen")
    def aussehen_zuruecksetzen_knopf(self, obj):
        if not obj or not obj.pk:
            return "– nach dem ersten Speichern verfügbar –"
        ziel = hausfarbe(obj) or "Standardblau"
        return format_html(
            '<button type="submit" name="_aussehen_zuruecksetzen" value="1" class="button" formnovalidate '
            'onclick="return confirm(\'Aussehen dieses Mandanten auf den Standard zurücksetzen?\')">Aussehen zurücksetzen</button> '
            '<span class="help">Layoutfarbe {}, Flächen neutral (Standardfarben).</span>', ziel)

    @admin.action(description="Aussehen zurücksetzen (Hausfarbe, Flächen neutral)", permissions=["change"])
    def aussehen_zuruecksetzen(self, request, queryset):
        for mandant in queryset:
            aussehen_zuruecksetzen(mandant)
        self.message_user(request, f"Aussehen von {queryset.count()} Mandant(en) zurückgesetzt.", messages.SUCCESS)

    def change_view(self, request, object_id, form_url="", extra_context=None):
        if request.method == "POST" and "_aussehen_zuruecksetzen" in request.POST:
            mandant = self.get_object(request, object_id)
            if mandant is None or not self.has_change_permission(request, mandant):
                raise PermissionDenied
            aussehen_zuruecksetzen(mandant)
            self.message_user(request, f"Aussehen von {mandant.kurzname} zurückgesetzt.", messages.SUCCESS)
            return HttpResponseRedirect(request.path)
        return super().change_view(request, object_id, form_url, extra_context)
    fieldsets = [
        ("Firma", {"fields": ["kurzname", "firmenname", "logo", "logo_vorschau", "strasse", "plz", "ort", "land", "telefon", "email", "aktiv"]}),
        ("Aussehen", {"fields": ["farbe", "flaechenmodus", "farbe_kopf", "farbe_leiste", "farbe_karte", "aussehen_zuruecksetzen_knopf"],
                      "description": "Layoutfarbe = Akzent (Links, Knöpfe, aktive Menüzeile). Flächenfarben = Hintergrund von Kopfbereich, "
                                     "Seitenleiste und Karten – neutral, in gleicher Tonart oder je Bereich frei gewählt."}),
        ("Steuer und Bank", {"fields": ["ust_id", "steuernummer", "iban", "bic", "glaeubiger_id"]}),
        ("E-Mail-Versand", {"fields": ["smtp_host", "smtp_port", "smtp_sicherheit", "smtp_benutzer", "absender_email", "absender_name", "kopie_an"]}),
        ("DATEV und Konten", {"fields": ["datev_berater", "datev_mandant", "konto_erloes_19", "konto_erloes_7", "konto_erloes_0",
                                         "konto_bank", "konto_kasse", "konto_skonto"]}),
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


@admin.register(Listenansicht)
class ListenansichtAdmin(admin.ModelAdmin):
    """Gespeicherte Listenansichten der Benutzer: ansehen und löschen (Zurücksetzen), nicht bearbeiten."""
    list_display = ["benutzer", "schluessel", "geaendert_am"]
    list_filter = ["benutzer"]
    search_fields = ["schluessel"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


Benutzer = get_user_model()
if admin.site.is_registered(Benutzer):
    admin.site.unregister(Benutzer)


@admin.register(Benutzer)
class BenutzerAdmin(BasisUserAdmin):
    """Wie die Standard-Benutzerverwaltung, dazu die Aktion zum Zurücksetzen der Listenansichten."""
    actions = [*BasisUserAdmin.actions, "listenansichten_zuruecksetzen"]

    def has_listenansichten_permission(self, request):
        return request.user.is_active and request.user.is_staff and request.user.has_perm("kern.delete_listenansicht")

    @admin.action(description="Listenansichten zurücksetzen", permissions=["listenansichten"])
    def listenansichten_zuruecksetzen(self, request, queryset):
        anzahl, _ = Listenansicht.objects.filter(benutzer__in=queryset).delete()
        self.message_user(
            request,
            f"{anzahl} gespeicherte Listenansicht(en) von {queryset.count()} Benutzer(n) zurückgesetzt.",
            messages.SUCCESS,
        )
