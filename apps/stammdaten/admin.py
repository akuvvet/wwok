"""Vorläufige Pflegemasken über den Django-Admin – bis die eigenen Masken stehen."""
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.urls import path, reverse

from . import leistung_import
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
    exclude = ["letzter_ek"]  # wird beim Wareneingang gesetzt, nicht von Hand
    readonly_fields = ["letzter_ek_anzeige", "letzter_ek_datum"]

    @admin.display(description="letzter EK")
    def letzter_ek_anzeige(self, obj):
        return f"{obj.letzter_ek:.2f}".replace(".", ",") if obj and obj.letzter_ek is not None else "–"


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
    readonly_fields = ["ek_anzeige", "angelegt_am", "geaendert_am", "geaendert_von"]
    inlines = [ArtikelMandantInline, ArtikelLieferantInline, GruppenKomponenteInline, ArtikelBildInline]

    def get_readonly_fields(self, request, obj=None):
        felder = [*super().get_readonly_fields(request, obj), *(["typ"] if obj else [])]  # Typwechsel würde Bestand und Belege verfälschen
        return [*felder, "kategorie"] if obj and obj.typ == m.Artikeltyp.L else felder  # Leistungen: Verwaltung → Leistungsartikel

    def formfield_for_choice_field(self, db_field, request, **kwargs):
        if db_field.name == "typ":  # neue Leistungen entstehen unter Leistungsartikel, mit Kategorie und Kosten
            kwargs["choices"] = [(w, n) for w, n in m.Artikeltyp.choices if w != m.Artikeltyp.L]
        return super().formfield_for_choice_field(db_field, request, **kwargs)

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


@admin.register(m.Leistungskategorie)
class LeistungskategorieAdmin(admin.ModelAdmin):
    list_display = ["name", "nummer"]
    search_fields = ["name", "nummer"]


@admin.register(m.Leistungsartikel)
class LeistungsartikelAdmin(admin.ModelAdmin):
    """Leistungen (Typ L) mit eigenen Kategorien und Kosten, dazu der Import aus CSV oder Excel."""

    list_display = ["nummer", "bezeichnung", "leistungskategorie", "kosten", "vk1", "mengeneinheit", "nicht_aktiv"]
    list_filter = ["leistungskategorie", "nicht_aktiv"]
    search_fields = ["nummer", "bezeichnung"]
    list_select_related = ["leistungskategorie"]
    change_list_template = "admin/stammdaten/leistungsartikel/change_list.html"
    fieldsets = [
        ("Grunddaten", {"fields": ["nummer", "bezeichnung", "bezeichnung_erweitert", "leistungskategorie",
                                   ("mengeneinheit", "steuersatz"), "nicht_aktiv"]}),
        ("Preise", {"fields": ["kosten", ("vk1", "vk2", "vk3")]}),
    ]

    def get_queryset(self, request):
        return super().get_queryset(request).filter(typ=m.Artikeltyp.L)

    def get_changeform_initial_data(self, request):
        from .masken import naechste_nummer
        return {**super().get_changeform_initial_data(request), "nummer": naechste_nummer(m.Artikel),
                "steuersatz": m.Steuersatz.objects.first()}

    def save_model(self, request, obj, form, change):
        obj.typ = m.Artikeltyp.L
        super().save_model(request, obj, form, change)

    def get_urls(self):
        return [path("import/", self.admin_site.admin_view(self.datei_import), name="stammdaten_leistungsartikel_import")] + super().get_urls()

    def darf_importieren(self, request):
        return (self.has_add_permission(request) and self.has_change_permission(request)
                and request.user.has_perm("stammdaten.add_leistungskategorie"))

    def changelist_view(self, request, extra_context=None):
        return super().changelist_view(request, {**(extra_context or {}), "darf_importieren": self.darf_importieren(request)})

    def datei_import(self, request):
        if not self.darf_importieren(request):
            raise PermissionDenied
        fehler = []
        if request.method == "POST":
            datei = request.FILES.get("datei")
            if datei is None or datei.size > 5 * 1024 * 1024:
                fehler = ["Bitte eine CSV- oder Excel-Datei bis 5 MB auswählen."]
            else:
                try:
                    neu, geaendert, gleich, kategorien = leistung_import.importieren(datei.name, datei.read())
                    messages.success(request, f"{neu} neu, {geaendert} geändert, {gleich} unverändert, {kategorien} neue Kategorie(n).")
                    return redirect(reverse("admin:stammdaten_leistungsartikel_changelist"))
                except leistung_import.ImportFehler as e:
                    fehler = [str(e)] + e.fehler[:30] + (["…"] if len(e.fehler) > 30 else [])
        return TemplateResponse(request, "admin/stammdaten/leistungsartikel/import.html", {
            **self.admin_site.each_context(request), "title": "Leistungsartikel importieren", "fehler": fehler})


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

    list_display = ["lieferant", "herstellernummer", "bestellnummer", "bezeichnung", "hersteller", "ek_anzeige", "bestand", "artikel"]
    list_filter = ["lieferant"]
    search_fields = ["bezeichnung", "herstellernummer", "bestellnummer", "ean", "hersteller"]
    raw_id_fields = ["artikel"]
    list_select_related = ["lieferant", "artikel"]
    show_full_result_count = False  # bei Hunderttausenden Zeilen kein teures COUNT(*)

    @admin.display(description="EK netto", ordering="ek")
    def ek_anzeige(self, obj):
        return f"{obj.ek:.2f}".replace(".", ",")
