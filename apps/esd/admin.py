from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.urls import path, reverse

from . import services

from .models import ESDKey, ESDVergabe


@admin.register(ESDKey)
class ESDKeyAdmin(admin.ModelAdmin):
    list_display = ["artikel", "key", "mandant", "max_vergaben", "vergeben", "status"]
    list_filter = ["mandant", "status"]
    search_fields = ["key", "artikel__nummer", "artikel__bezeichnung"]
    autocomplete_fields = ["artikel"]  # Suchfeld statt Liste mit allen Artikeln
    change_list_template = "admin/esd/esdkey/change_list.html"

    def get_urls(self):
        return [path("csv/", self.admin_site.admin_view(self.csv_import), name="esd_esdkey_csv")] + super().get_urls()

    def csv_import(self, request):
        """Keys per CSV: Mandant; Artikelnummer; Key; Anzahl (alles oder nichts)."""
        if not self.has_add_permission(request):
            raise PermissionDenied
        fehler = []
        if request.method == "POST":
            datei = request.FILES.get("datei")
            if datei is None or datei.size > 5 * 1024 * 1024:
                fehler = ["Bitte eine CSV-Datei bis 5 MB auswählen."]
            else:
                try:
                    neu, doppelt, artikel_anzahl = services.csv_importieren(datei.read())
                    messages.success(request, f"{neu} Key(s) für {artikel_anzahl} Artikel importiert, {doppelt} bereits vorhanden.")
                    return redirect(reverse("admin:esd_esdkey_changelist"))
                except services.CsvFehler as e:
                    fehler = [str(e)] + e.fehler[:30] + (["…"] if len(e.fehler) > 30 else [])
                except ValueError as e:
                    fehler = [str(e)]
        return TemplateResponse(request, "admin/esd/esdkey/csv_import.html", {
            **self.admin_site.each_context(request), "title": "ESD-Keys per CSV importieren", "fehler": fehler})


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
