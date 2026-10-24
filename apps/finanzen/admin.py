from django.contrib import admin

from .models import Bankumsatz, Kassenbuch, Mahnstufe, Mahnung, Zahlung


class NurLesen(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Zahlung)
class ZahlungAdmin(NurLesen):
    """Zahlungen werden in der Maske der Rechnung gebucht und sind unveränderlich."""

    list_display = ["datum", "mandant", "beleg", "betrag", "zahlart", "benutzer"]
    list_filter = ["mandant", "zahlart"]
    search_fields = ["beleg__nummer", "beleg__kunde__name1", "bemerkung"]


@admin.register(Mahnung)
class MahnungAdmin(NurLesen):
    list_display = ["datum", "beleg", "bezeichnung", "offener_betrag", "gebuehr", "frist"]
    list_filter = ["stufe"]


@admin.register(Mahnstufe)
class MahnstufeAdmin(admin.ModelAdmin):
    list_display = ["mandant", "stufe", "bezeichnung", "tage_seit_vorheriger", "zahlungsfrist_tage", "gebuehr"]
    list_filter = ["mandant"]


@admin.register(Bankumsatz)
class BankumsatzAdmin(NurLesen):
    list_display = ["buchungsdatum", "mandant", "betrag", "name", "status"]
    list_filter = ["mandant", "status"]
    search_fields = ["name", "zweck", "iban"]


@admin.register(Kassenbuch)
class KassenbuchAdmin(NurLesen):
    list_display = ["mandant", "nummer", "datum", "art", "betrag", "text"]
    list_filter = ["mandant", "art"]
