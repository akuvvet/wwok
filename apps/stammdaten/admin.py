"""Vorläufige Pflegemasken über den Django-Admin – bis die eigenen Masken stehen."""
from django.contrib import admin

from . import models as m


class KundeMandantInline(admin.TabularInline):
    model = m.KundeMandant
    extra = 0


class KundenpreisInline(admin.TabularInline):
    model = m.Kundenpreis
    extra = 0


class KundenanschriftInline(admin.StackedInline):
    model = m.Kundenanschrift
    extra = 0


class AnsprechpartnerKundeInline(admin.TabularInline):
    model = m.Ansprechpartner
    fk_name = "kunde"
    exclude = ["lieferant", "mandant"]
    extra = 0


@admin.register(m.Kunde)
class KundeAdmin(admin.ModelAdmin):
    list_display = ["nummer", "name1", "plz", "ort", "kundenart", "preisstufe", "gesperrt"]
    list_filter = ["kundenart", "preisstufe", "gesperrt", "land"]
    search_fields = ["nummer", "name1", "name2", "ort", "email", "ust_id"]
    readonly_fields = ["erstkontakt", "angelegt_am", "geaendert_am", "geaendert_von"]
    inlines = [KundeMandantInline, KundenanschriftInline, AnsprechpartnerKundeInline, KundenpreisInline]
    fieldsets = [
        ("Grunddaten", {"fields": ["nummer", ("name1", "name2", "name3"), "strasse", ("plz", "ort", "land"),
                                   ("telefon", "email", "webseite"), ("gesellschaftsform", "inhaber_gf")]}),
        ("Einstufung", {"fields": [("kundenart", "preisstufe"), "zahlungsbedingung", "gesperrt"]}),
        ("Hinweis", {"fields": ["hinweis"]}),
        ("Steuer und Bank", {"fields": [("ust_id", "ust_id_geprueft_am"), ("kontoinhaber", "bank"),
                                        ("iban", "bic"), ("sepa_mandatsreferenz", "sepa_mandat_datum"), "leitweg_id"]}),
        ("Automatisch", {"fields": [("erstkontakt", "angelegt_am"), ("geaendert_am", "geaendert_von")]}),
    ]


class LieferantMandantInline(admin.TabularInline):
    model = m.LieferantMandant
    extra = 0


class AnsprechpartnerLieferantInline(admin.TabularInline):
    model = m.Ansprechpartner
    fk_name = "lieferant"
    exclude = ["kunde", "shop_zugang"]
    extra = 0


@admin.register(m.Lieferant)
class LieferantAdmin(admin.ModelAdmin):
    list_display = ["nummer", "name1", "ort", "lieferzeit_tage", "gesperrt"]
    search_fields = ["nummer", "name1", "ort", "ust_id"]
    readonly_fields = ["angelegt_am", "geaendert_am", "geaendert_von"]
    inlines = [LieferantMandantInline, AnsprechpartnerLieferantInline]


class ArtikelMandantInline(admin.TabularInline):
    model = m.ArtikelMandant
    extra = 0
    readonly_fields = ["letzter_ek", "letzter_ek_datum"]


class ArtikelBildInline(admin.TabularInline):
    model = m.ArtikelBild
    extra = 0
    max_num = 10


class ArtikelLieferantInline(admin.TabularInline):
    model = m.ArtikelLieferant
    extra = 0


class GruppenKomponenteInline(admin.TabularInline):
    model = m.GruppenKomponente
    fk_name = "gruppe"
    extra = 0
    autocomplete_fields = ["komponente"]
    verbose_name_plural = "Komponenten (nur bei Typ G)"


@admin.register(m.Artikel)
class ArtikelAdmin(admin.ModelAdmin):
    list_display = ["nummer", "typ", "bezeichnung", "hersteller", "herstellernummer", "vk1", "online", "nicht_aktiv"]
    list_filter = ["typ", "online", "nicht_aktiv", "kategorie", "hersteller"]
    search_fields = ["nummer", "bezeichnung", "herstellernummer", "ean"]
    readonly_fields = ["durchschnitts_ek", "ek_anzeige", "angelegt_am", "geaendert_am", "geaendert_von"]
    inlines = [ArtikelMandantInline, ArtikelLieferantInline, GruppenKomponenteInline, ArtikelBildInline]

    @admin.display(description="EK netto (errechnet)")
    def ek_anzeige(self, obj):
        ek, quelle = m._ek_quelle(obj) if obj.pk else (None, "")
        return f"{ek:.2f} € ({quelle})".replace(".", ",") if ek is not None else "–"
    fieldsets = [
        ("Grunddaten", {"fields": [("nummer", "typ"), "bezeichnung", "bezeichnung_erweitert", "kategorie",
                                   ("hersteller", "herstellernummer", "ean"), ("garantie_monate", "nicht_aktiv"), "esd_key"]}),
        ("Maße", {"fields": [("laenge_cm", "breite_cm", "hoehe_cm", "gewicht_kg")]}),
        ("Preise", {"fields": ["steuersatz", "ek_anzeige", ("vk1", "aufschlag_vk1"),
                               ("vk2", "aufschlag_vk2"), ("vk3", "aufschlag_vk3")]}),
        ("Online", {"fields": ["online", "kurzbeschreibung", "keywords", "meta_titel",
                               "meta_description", "meta_keywords"]}),
    ]


admin.site.register([m.Zahlungsbedingung, m.Steuersatz, m.Kategorie, m.Hersteller])


@admin.register(m.PreisVorgabe)
class PreisVorgabeAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return not m.PreisVorgabe.objects.exists()


@admin.register(m.Kundenpreis)
class KundenpreisAdmin(admin.ModelAdmin):
    list_display = ["kunde", "artikel", "preis", "gueltig_ab", "gueltig_bis"]
    search_fields = ["kunde__nummer", "kunde__name1", "artikel__nummer", "artikel__bezeichnung"]


@admin.register(m.Preislistenartikel)
class PreislistenartikelAdmin(admin.ModelAdmin):
    """Artikel aus den Lieferantenpreislisten (Typ P). Sie entstehen durch den Preislistenimport; hier nur zum Nachsehen."""

    list_display = ["lieferant", "herstellernummer", "bestellnummer", "bezeichnung", "hersteller", "ek", "bestand", "artikel"]
    list_filter = ["lieferant"]
    search_fields = ["bezeichnung", "herstellernummer", "bestellnummer", "ean", "hersteller"]
    raw_id_fields = ["artikel"]
    list_select_related = ["lieferant", "artikel"]
    show_full_result_count = False  # bei Hunderttausenden Zeilen kein teures COUNT(*)
