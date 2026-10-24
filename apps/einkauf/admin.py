from django.contrib import admin

from .models import (BestellPosition, Bestellung, Lieferantenrechnung, Preislistenimport, Preislistenprofil, Rma,
                     Wareneingang, WareneingangPosition)


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


@admin.register(Preislistenprofil)
class PreislistenprofilAdmin(admin.ModelAdmin):
    list_display = ["name", "lieferant", "quelle", "dateityp", "intervall_stunden", "aktiv", "letzter_lauf"]
    list_filter = ["quelle", "dateityp", "aktiv"]
    fieldsets = [
        ("Preisliste", {"fields": ["lieferant", "name", "aktiv", "neue_artikel"]}),
        ("Quelle und Zeitplan", {"fields": ["quelle", "adresse", "port", "pfad", "benutzer", "intervall_stunden"],
                                 "description": "Download-Adresse: die vollständige URL (https://…) ins Feld „Adresse“, Benutzer = z. B. Kundennummer. FTP/SFTP: Server in „Adresse“, Dateipfad in „Pfad“. Das Passwort steht nicht hier, sondern in der .env als PREISLISTE_PW_<ID des Profils>."}),
        ("Dateiformat", {"fields": ["dateityp", "zeichensatz", ("trennzeichen", "textbegrenzer"), "dezimaltrennzeichen",
                                    ("kopfzeile", "erste_datenzeile"), "xml_satz"]}),
        ("Feldzuordnung (Spaltenüberschrift oder Spaltennummer)", {"fields": [
            "sp_bestellnummer", "sp_herstellernummer", "sp_ean", "sp_bezeichnung", "sp_hersteller", "sp_kategorie",
            "sp_ek", "sp_bestand", "sp_lieferzeit", "sp_gewicht"]}),
    ]


@admin.register(Preislistenimport)
class PreislistenimportAdmin(admin.ModelAdmin):
    list_display = ["zeitpunkt", "profil", "gelesen", "neu", "geaendert", "fehler"]
    list_filter = ["profil"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
