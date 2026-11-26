"""Vorläufige Pflegemasken über den Django-Admin – bis die eigenen Masken stehen."""
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.templatetags.static import static
from django.urls import path, reverse
from django.utils.html import format_html

from apps.kern.seiten import BlaetternAdminMixin

from . import kopie, leistung_import
from . import models as m
from .forms import VkVorschlagForm


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


class VerwendungsschutzMixin:
    """Verwendete Datensätze erscheinen in der Löschbestätigung als „geschützt“ – Löschen ist dann nicht möglich (25.11.2026)."""

    def get_deleted_objects(self, objs, request):
        from .verwendung import geprueft, text, verwendungen
        geloescht, anzahl, rechte, geschuetzt = super().get_deleted_objects(objs, request)
        gruende = [text(obj, treffer) for obj in objs if geprueft(obj) and (treffer := verwendungen(obj))]
        if gruende:  # klare Begründung statt Djangos „fehlende Rechte für Belege …“
            return geloescht, anzahl, set(), gruende
        return geloescht, anzahl, rechte, geschuetzt


class StammImportMixin:
    """Import aus CSV/Excel mit Beispieldatei zum Herunterladen (Wunsch des Nutzers 26.11.2026): Knopf „Importieren“ über der
    Liste, Importseite mit Hinweisen und Beispieldatei (CSV oder Excel: Kopfzeile und eine Beispielzeile)."""
    change_list_template = "admin/stammdaten/import_change_list.html"
    import_art = ""  # Schlüssel in stamm_import.BEISPIELE
    import_titel = ""
    import_hinweis = ""  # HTML: Spalten und Regeln
    import_rechte = ()  # zusätzliche Rechte, z. B. weil fehlende Kategorien angelegt werden

    def import_ausfuehren(self, dateiname, inhalt) -> str:
        raise NotImplementedError

    def _import_name(self, teil):
        return f"{self.opts.app_label}_{self.opts.model_name}_{teil}"

    def get_urls(self):
        return [path("import/", self.admin_site.admin_view(self.datei_import), name=self._import_name("import")),
                path("import/beispiel.<str:format_>", self.admin_site.admin_view(self.import_beispiel),
                     name=self._import_name("beispiel")),
                ] + super().get_urls()

    def darf_importieren(self, request):
        return (self.has_add_permission(request) and self.has_change_permission(request)
                and all(request.user.has_perm(r) for r in self.import_rechte))

    def changelist_view(self, request, extra_context=None):
        return super().changelist_view(request, {**(extra_context or {}), "darf_importieren": self.darf_importieren(request),
                                                 "import_url": reverse(f"admin:{self._import_name('import')}")})

    def import_beispiel(self, request, format_):
        from django.http import Http404, HttpResponse
        from . import stamm_import
        if not self.darf_importieren(request):
            raise PermissionDenied
        if format_ not in ("csv", "xlsx"):
            raise Http404
        typ = "text/csv; charset=utf-8" if format_ == "csv" else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        antwort = HttpResponse(stamm_import.beispiel(self.import_art, format_), content_type=typ)
        antwort["Content-Disposition"] = f'attachment; filename="beispiel-{self.import_art}.{format_}"'
        return antwort

    def datei_import(self, request):
        from .leistung_import import ImportFehler
        if not self.darf_importieren(request):
            raise PermissionDenied
        fehler = []
        if request.method == "POST":
            datei = request.FILES.get("datei")
            if datei is None or datei.size > 5 * 1024 * 1024:
                fehler = ["Bitte eine CSV- oder Excel-Datei bis 5 MB auswählen."]
            else:
                try:
                    messages.success(request, self.import_ausfuehren(datei.name, datei.read()))
                    return redirect(reverse(f"admin:{self.opts.app_label}_{self.opts.model_name}_changelist"))
                except ImportFehler as e:
                    fehler = [str(e)] + e.fehler[:30] + (["…"] if len(e.fehler) > 30 else [])
        return TemplateResponse(request, "admin/stammdaten/import.html", {
            **self.admin_site.each_context(request), "title": self.import_titel, "opts": self.opts, "fehler": fehler,
            "hinweis": self.import_hinweis,
            "beispiel_csv": reverse(f"admin:{self._import_name('beispiel')}", args=["csv"]),
            "beispiel_xlsx": reverse(f"admin:{self._import_name('beispiel')}", args=["xlsx"]),
            "liste_url": reverse(f"admin:{self.opts.app_label}_{self.opts.model_name}_changelist")})


@admin.register(m.Kunde)
class KundeAdmin(VerwendungsschutzMixin, BlaetternAdminMixin, admin.ModelAdmin):
    list_display = ["nummer", "name1", "plz", "ort", "kundenart", "preisstufe", "gesperrt"]
    list_filter = ["kundenart", "preisstufe", "steuerart", "gesperrt", "land"]
    search_fields = ["nummer", "name1", "name2", "ort", "email", "ust_id"]
    readonly_fields = ["erstkontakt", "angelegt_am", "geaendert_am", "geaendert_von"]
    inlines = [KundeMandantInline, KundenanschriftInline, AnsprechpartnerKundeInline, KundenpreisInline]
    fieldsets = [
        ("Grunddaten", {"fields": [("nummer", "anrede"), ("name1", "name2", "name3"), "strasse", ("plz", "ort", "land"),
                                   ("telefon", "email", "webseite"), ("gesellschaftsform", "inhaber_gf")]}),
        ("Einstufung", {"fields": [("kundenart", "preisstufe"), ("zahlungsbedingung", "standard_versandart"), "gesperrt"]}),
        ("Hinweis", {"fields": ["hinweis"]}),
        ("Steuer und Bank", {"fields": ["steuerart", ("ust_id", "ust_id_geprueft_am"), ("kontoinhaber", "bank"),
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
class LieferantAdmin(VerwendungsschutzMixin, BlaetternAdminMixin, admin.ModelAdmin):
    list_display = ["nummer", "kurzname", "name1", "ort", "lieferzeit_tage", "gesperrt"]
    search_fields = ["nummer", "kurzname", "name1", "ort", "ust_id"]
    readonly_fields = ["angelegt_am", "geaendert_am", "geaendert_von"]
    inlines = [LieferantMandantInline, AnsprechpartnerLieferantInline]


@admin.register(m.UstIdPruefung)
class UstIdPruefungAdmin(admin.ModelAdmin):
    """Nachweis: nur lesen."""
    list_display = ["zeitpunkt", "ust_id", "dienst", "gueltig", "kunde", "lieferant", "mandant", "benutzer"]
    list_filter = ["dienst", "gueltig", "mandant"]
    search_fields = ["ust_id", "firmenname", "referenz"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


def kopieren_daten(request, artikel, ziel_name):
    """Angaben für den Knopf „Kopieren“ im geöffneten Artikel (Verwaltung); nur mit Recht „Artikel anlegen“."""
    if not request.user.has_perm("stammdaten.add_artikel"):
        return None
    return {"url": reverse(ziel_name), "pk": artikel.pk, "name": f"{artikel.nummer} – {artikel.bezeichnung}"}


class KopierteZeilenMixin:
    """Beim Kopieren (?kopie_von=…) so viele Zeilen anbieten, wie die Vorlage Werte liefert (Initialwerte setzt ArtikelAdmin)."""

    def get_extra(self, request, obj=None, **kwargs):
        if obj is None and request.method == "GET" and request.GET.get("kopie_von", "").isdigit():
            return max(self.extra, 1 if self.model is m.ArtikelMandant else
                       m.GruppenKomponente.objects.filter(gruppe_id=request.GET["kopie_von"]).count())
        return self.extra


class ArtikelMandantInline(KopierteZeilenMixin, admin.TabularInline):
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


class GruppenKomponenteInline(KopierteZeilenMixin, admin.TabularInline):
    model = m.GruppenKomponente
    fk_name = "gruppe"
    extra = 0
    autocomplete_fields = ["komponente"]
    verbose_name_plural = "Komponenten (nur bei Typ G)"


AMPEL_SYMBOL = {"rot": "🔴", "gelb": "🟡", "blau": "🔵", "gruen": "🟢", "grau": "⚪"}


class AuffaelligFilter(admin.SimpleListFilter):
    """Preis-Radar: nur Artikel mit rotem, gelbem, blauem oder grünem Befund (Mandant der Sitzung)."""

    title = "Preis-Radar"
    parameter_name = "radar"

    def lookups(self, request, model_admin):
        return [("auffaellig", "nur auffällige")]

    def queryset(self, request, queryset):
        if self.value() != "auffaellig":
            return queryset
        from apps.kern.kontext import mandant_oder_none
        from . import radar
        mandant = mandant_oder_none(request)
        if mandant is None:
            return queryset
        # wie im Radar: Artikel mit Bestand oder offenen Belegen; Ampel grau zählt nicht als auffällig
        ids = [e.artikel.pk for e in radar.eintraege(mandant) if e.ampel in ("rot", "gelb", "blau", "gruen")]
        return queryset.filter(pk__in=ids)


class StandardVkMixin:
    """Die Preisspalte der Liste zeigt den Standard-VK des gewählten Mandanten (Verwaltung → Mandanten)."""

    def get_list_display(self, request):
        from apps.kern.kontext import mandant_oder_none
        mandant = mandant_oder_none(request)
        feld = f"vk{mandant.standard_vk if mandant else 3}"

        @admin.display(description=feld.upper(), ordering=feld)
        def standard_vk(obj):
            return getattr(obj, feld)
        return [standard_vk if spalte == "vk1" else spalte for spalte in super().get_list_display(request)]


@admin.register(m.Artikel)
class ArtikelAdmin(StammImportMixin, VerwendungsschutzMixin, BlaetternAdminMixin, StandardVkMixin, admin.ModelAdmin):
    form = VkVorschlagForm
    import_art = "artikel"
    import_titel = "Artikel importieren"
    import_rechte = ("stammdaten.add_kategorie", "stammdaten.add_hersteller")
    import_hinweis = (
        "Importiert Artikel (Typ A, Lagerartikel). Pflicht: <strong>Nummer; Bezeichnung</strong>, dazu wahlweise <strong>Erweiterte "
        "Bezeichnung; Kategorie; Hersteller; Herstellernummer; EAN; EK; VK1; VK2; VK3; UVP; Einheit; Steuersatz; Gewicht (kg); "
        "Garantie (Monate); Online; nicht aktiv; Kostenstelle</strong>.<ul>"
        "<li>Vorhandene Nummer: Der Artikel wird aktualisiert, leere Zellen lassen den alten Wert stehen. Leistungen und Gruppen werden nicht verändert.</li>"
        "<li>Kategorie über Nummer oder Namen; fehlende Kategorien werden auf oberster Ebene angelegt (offline). Fehlende Hersteller werden angelegt.</li>"
        "<li>EK wird als manueller EK eingetragen. Ohne Steuersatz gilt der höchste Satz (19 oder 1 = 19 %, 7 oder 2 = 7 %).</li>"
        "<li>Der Bestand wird nicht importiert, dafür die Inventur verwenden.</li>"
        "<li>Bei einem Fehler in einer Zeile wird nichts importiert.</li></ul>")

    def import_ausfuehren(self, dateiname, inhalt):
        neu, geaendert, gleich, kategorien = leistung_import.importieren(dateiname, inhalt, m.Artikeltyp.A)
        return f"{neu} neu, {geaendert} geändert, {gleich} unverändert, {kategorien} neue Kategorie(n)."
    list_display = ["nummer", "typ", "bezeichnung", "hersteller", "herstellernummer", "vk1", "radar_ampel", "online", "aktiv"]
    list_filter = [AuffaelligFilter, "typ", "online", "nicht_aktiv", "kategorie", "hersteller"]
    search_fields = ["nummer", "bezeichnung", "herstellernummer", *m.KENNUNGEN]
    filter_horizontal = ["weitere_kategorien"]
    list_select_related = ["hersteller", "kategorie__eltern__eltern"]
    readonly_fields = ["ek_anzeige", "shop_hinweis", "angelegt_am", "geaendert_am", "geaendert_von"]
    inlines = [ArtikelMandantInline, ArtikelLieferantInline, GruppenKomponenteInline, ArtikelBildInline]

    def get_readonly_fields(self, request, obj=None):
        felder = [*super().get_readonly_fields(request, obj), *(["typ"] if obj else [])]  # Typwechsel würde Bestand und Belege verfälschen
        return [*felder, "kategorie"] if obj and obj.typ == m.Artikeltyp.L else felder  # Leistungen: Verwaltung → Leistungsartikel

    # --- Kopieren (Issue 15): „Neuer Artikel“ mit den Werten einer Vorlage, gespeichert wird erst mit „Speichern“ ---
    def kopie_vorlage(self, request):
        """Vorlage aus ?kopie_von=<id> (nur beim Neuanlegen); dieselbe Kopierlogik wie in der Artikelmaske (kopie.py)."""
        pk = request.GET.get("kopie_von", "")
        return m.Artikel.objects.filter(pk=pk).first() if pk.isdigit() else None

    def get_changeform_initial_data(self, request):
        vorlage = self.kopie_vorlage(request)
        if vorlage is None:
            return super().get_changeform_initial_data(request)
        neu = kopie.neuer_artikel(vorlage)
        werte = {**kopie.kopie_initial(vorlage), "typ": neu.typ, "nummer": neu.nummer}
        return {**super().get_changeform_initial_data(request), **werte}

    def get_formset_kwargs(self, request, obj, inline, prefix):
        kwargs = super().get_formset_kwargs(request, obj, inline, prefix)
        vorlage = self.kopie_vorlage(request) if (obj is None or obj.pk is None) and request.method == "GET" else None
        if vorlage is not None:
            if isinstance(inline, ArtikelMandantInline):
                from apps.kern.kontext import mandant_oder_none
                mandant = mandant_oder_none(request)
                werte = kopie.mandant_werte(vorlage, mandant) if mandant else {}
                kwargs["initial"] = [{**werte, "mandant": mandant.pk}] if mandant else []
            elif isinstance(inline, GruppenKomponenteInline):
                kwargs["initial"] = [{"komponente": k.pk, "menge": menge} for k, menge in kopie.komponenten_werte(vorlage)]
        return kwargs

    def save_model(self, request, obj, form, change):
        vorlage = None if change else self.kopie_vorlage(request)
        if vorlage is not None:
            m.Stammnummer.belegen(obj.typ, obj.nummer)
        super().save_model(request, obj, form, change)

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        vorlage = None if change else self.kopie_vorlage(request)
        if vorlage is not None and request.GET.get("kopie_bilder"):
            kopie.bilder_duplizieren(vorlage, form.instance)

    def render_change_form(self, request, context, add=False, change=False, form_url="", obj=None):
        if obj is not None and not add:
            ziel = "admin:stammdaten_leistungsartikel_add" if obj.typ == m.Artikeltyp.L else "admin:stammdaten_artikel_add"
            context["kopieren"] = kopieren_daten(request, obj, ziel)
        return super().render_change_form(request, context, add, change, form_url, obj)

    def formfield_for_choice_field(self, db_field, request, **kwargs):
        if db_field.name == "typ":  # neue Leistungen entstehen unter Leistungsartikel, mit Kategorie und Kosten
            kwargs["choices"] = [(w, n) for w, n in m.Artikeltyp.choices if w != m.Artikeltyp.L]
        return super().formfield_for_choice_field(db_field, request, **kwargs)

    @admin.display(description="Ampel")
    def radar_ampel(self, obj):
        return AMPEL_SYMBOL.get(getattr(obj, "_radar_farbe", ""), "")

    def changelist_view(self, request, extra_context=None):
        from apps.kern.kontext import mandant_oder_none
        from . import radar
        antwort = super().changelist_view(request, extra_context)
        cl = getattr(antwort, "context_data", None) and antwort.context_data.get("cl")
        mandant = mandant_oder_none(request)  # Ampel gilt für den gewählten Mandanten
        if cl is not None and mandant is not None:
            seite = list(cl.result_list)  # die Seite wird einmal geladen; die Ampeln für alle Zeilen auf einmal berechnen
            farben = radar.ampeln(seite, mandant)
            for artikel in seite:
                artikel._radar_farbe = farben.get(artikel.pk, "")
        return antwort

    @admin.display(description="Aktiv", boolean=True, ordering="-nicht_aktiv")
    def aktiv(self, obj):
        return not obj.nicht_aktiv  # grünes Häkchen, wenn „nicht aktiv“ nicht gesetzt ist

    @admin.display(description="EK netto (errechnet)")
    def ek_anzeige(self, obj):
        ek, quelle = m._ek_quelle(obj) if obj.pk else (None, "")
        return f"{ek:.2f} € ({quelle})".replace(".", ",") if ek is not None else "–"
    fieldsets = [
        ("Grunddaten", {"fields": [("nummer", "typ"), "bezeichnung", "bezeichnung_erweitert", "kategorie", "weitere_kategorien",
                                   ("hersteller", "herstellernummer", "ean"), ("garantie_monate", "nicht_aktiv"), "esd_key"]}),
        ("Kennungen", {"fields": [("amazon_asin", "amazon_sku", "amazon_fnsku"), ("isbn", "upc", "ebay_id")]}),
        ("Maße", {"fields": [("laenge_cm", "breite_cm", "hoehe_cm", "gewicht_kg")]}),
        # wie die Artikelmaske: EK und Häkchen, dann VK1–VK3, dann Aufschlag VK1–VK3; „fix“ und Umrechnung per change_form.html
        ("Preise", {"fields": ["steuersatz", ("ek_manuell", "vk1_fuer_alle", "aufschlag_vk1_fuer_alle"), ("vk1", "vk2", "vk3"),
                               ("aufschlag_vk1", "aufschlag_vk2", "aufschlag_vk3"),
                               ("uvp", "ek3", "preiseinheit"), ("sonder_ek", "sonder_ek_von", "sonder_ek_bis"), ("vk_min", "vk_max"),
                               ("grundpreis_faktor", "grundpreis_einheit")]}),
        ("Online", {"fields": ["online", "shop_hinweis", "kurzbeschreibung", "keywords", "meta_titel",
                               "meta_description", "meta_keywords"]}),
    ]

    @admin.display(description="Shop-Status")
    def shop_hinweis(self, obj):
        grund = obj.kategorie.offline_grund() if obj and obj.pk and obj.kategorie_id else None
        if grund is not None and obj.online and not obj.nicht_aktiv:
            return f"Im Shop offline wegen Kategorie „{grund.name}“ (Kategorie ist offline)."
        return "–"


@admin.register(m.Leistungskategorie)
class LeistungskategorieAdmin(StammImportMixin, admin.ModelAdmin):
    list_display = ["name", "nummer"]
    search_fields = ["name", "nummer"]
    import_art = "leistungskategorie"
    import_titel = "Leistungskategorien importieren"
    import_hinweis = ("Pflicht: <strong>Name</strong>, dazu wahlweise <strong>Nummer</strong>.<ul>"
                      "<li>Vorhandene Nummer: Der Name wird übernommen. Ohne Nummer wird über den Namen verglichen.</li>"
                      "<li>Bei einem Fehler in einer Zeile wird nichts importiert.</li></ul>")

    def import_ausfuehren(self, dateiname, inhalt):
        from .stamm_import import leistungskategorien_importieren
        neu, geaendert, gleich = leistungskategorien_importieren(dateiname, inhalt)
        return f"{neu} neu, {geaendert} geändert, {gleich} unverändert."


@admin.register(m.Leistungsartikel)
class LeistungsartikelAdmin(StammImportMixin, VerwendungsschutzMixin, BlaetternAdminMixin, StandardVkMixin, admin.ModelAdmin):
    import_art = "leistungsartikel"
    import_titel = "Leistungsartikel importieren"
    import_rechte = ("stammdaten.add_leistungskategorie",)
    import_hinweis = (
        "Pflicht: <strong>Nummer; Bezeichnung</strong>, dazu wahlweise <strong>Erweiterte Bezeichnung; Kategorie; Kosten; VK1; VK2; "
        "VK3; Einheit; Steuersatz; nicht aktiv</strong>.<ul>"
        "<li>Vorhandene Nummer: Die Leistung wird aktualisiert, leere Zellen lassen den alten Wert stehen. Lagerartikel und Gruppen werden nicht verändert.</li>"
        "<li>Fehlende Kategorien werden angelegt. Ohne Steuersatz gilt der höchste Satz.</li>"
        "<li>Bei einem Fehler in einer Zeile wird nichts importiert.</li></ul>")
    """Leistungen (Typ L) mit eigenen Kategorien und Kosten, dazu der Import aus CSV oder Excel."""

    list_display = ["nummer", "bezeichnung", "leistungskategorie", "kosten", "vk1", "mengeneinheit", "aktiv"]
    list_filter = ["leistungskategorie", "nicht_aktiv"]
    search_fields = ["nummer", "bezeichnung"]
    list_select_related = ["leistungskategorie"]
    fieldsets = [
        ("Grunddaten", {"fields": ["nummer", "bezeichnung", "bezeichnung_erweitert", "leistungskategorie",
                                   ("mengeneinheit", "steuersatz"), "kostenstelle", "nicht_aktiv"]}),
        ("Preise", {"fields": ["kosten", ("vk1", "vk2", "vk3")]}),
    ]

    @admin.display(description="Aktiv", boolean=True, ordering="-nicht_aktiv")
    def aktiv(self, obj):
        return not obj.nicht_aktiv

    def get_queryset(self, request):
        return super().get_queryset(request).filter(typ=m.Artikeltyp.L)

    def get_changeform_initial_data(self, request):
        daten = {**super().get_changeform_initial_data(request), "nummer": m.Stammnummer.vorschlag(m.Stammnummer.Art.LEISTUNG),
                 "steuersatz": m.Steuersatz.standard()}
        vorlage = request.GET.get("kopie_von", "")
        vorlage = m.Artikel.objects.filter(pk=vorlage, typ=m.Artikeltyp.L).first() if vorlage.isdigit() else None
        if vorlage is not None:  # Kopie (Issue 15): dieselbe Logik wie bei den anderen Artikeln
            felder = {"bezeichnung", "bezeichnung_erweitert", "leistungskategorie", "mengeneinheit", "steuersatz", "nicht_aktiv", "kosten",
                      "vk1", "vk2", "vk3"}
            daten.update({n: w for n, w in kopie.kopie_initial(vorlage).items() if n in felder})
        return daten

    def render_change_form(self, request, context, add=False, change=False, form_url="", obj=None):
        if obj is not None and not add:
            context["kopieren"] = kopieren_daten(request, obj, "admin:stammdaten_leistungsartikel_add")
        return super().render_change_form(request, context, add, change, form_url, obj)

    def save_model(self, request, obj, form, change):
        obj.typ = m.Artikeltyp.L
        if not change:
            m.Stammnummer.belegen(m.Stammnummer.Art.LEISTUNG, obj.nummer)
        super().save_model(request, obj, form, change)

    def import_ausfuehren(self, dateiname, inhalt):
        neu, geaendert, gleich, kategorien = leistung_import.importieren(dateiname, inhalt)
        return f"{neu} neu, {geaendert} geändert, {gleich} unverändert, {kategorien} neue Kategorie(n)."


admin.site.register([m.Steuersatz])


@admin.register(m.Anrede)
class AnredeAdmin(admin.ModelAdmin):
    """Anreden für Kunden, Lieferanten und Ansprechpartner; neue Einträge mit eigener Nummer anlegen."""

    list_display = ["nummer", "bezeichnung"]
    list_display_links = ["nummer"]
    list_editable = ["bezeichnung"]
    search_fields = ["bezeichnung"]


@admin.register(m.Zahlungsbedingung)
class ZahlungsbedingungAdmin(admin.ModelAdmin):
    """Zahlungsbedingungen („zabe“): real = fällig, intern = Mahnung, stop = Lieferstopp (Tage nach Belegdatum)."""

    list_display = ["nummer", "bezeichnung", "tage_netto", "tage_mahnung", "tage_lieferstop", "vorkasse", "lastschrift"]
    list_display_links = ["nummer", "bezeichnung"]
    list_editable = ["tage_netto", "tage_mahnung", "tage_lieferstop"]
    search_fields = ["bezeichnung", "nummer"]


class OnlineStatusFilter(admin.SimpleListFilter):
    title = "Shop-Status"
    parameter_name = "shop"

    def lookups(self, request, model_admin):
        return [("online", "online"), ("offline", "offline (auch über übergeordnete)")]

    def queryset(self, request, queryset):
        offline = m.Kategorie.offline_ids()
        if self.value() == "online":
            return queryset.exclude(pk__in=offline)
        if self.value() == "offline":
            return queryset.filter(pk__in=offline)
        return queryset


@admin.register(m.Kategorie)
class KategorieAdmin(StammImportMixin, admin.ModelAdmin):
    import_art = "kategorie"
    import_titel = "Kategorien importieren"
    import_hinweis = (
        "Pflicht: <strong>Name</strong>, dazu wahlweise <strong>Nummer; Übergeordnet; Aufschlag VK1; Aufschlag VK2; Aufschlag VK3; "
        "Online</strong>.<ul>"
        "<li>Übergeordnet: Nummer oder Name einer vorhandenen Kategorie oder einer, die weiter oben in der Datei steht; leer = oberste Ebene.</li>"
        "<li>Vorhandene Kategorie (gleiche Nummer, sonst gleicher Name unter derselben übergeordneten) wird aktualisiert; leere Zellen lassen den alten Wert stehen.</li>"
        "<li>Neue Kategorien sind offline, solange Online nicht „ja“ ist. Online „nein“ bei einer vorhandenen Kategorie nimmt ihre Artikel aus dem Shop.</li>"
        "<li>Bei einem Fehler in einer Zeile wird nichts importiert.</li></ul>")

    def import_ausfuehren(self, dateiname, inhalt):
        from .stamm_import import kategorien_importieren
        neu, geaendert, gleich = kategorien_importieren(dateiname, inhalt)
        return f"{neu} neu, {geaendert} geändert, {gleich} unverändert."
    list_display = ["name", "nummer", "eltern", "online_status", "artikel_im_shop", "im_shop"]
    list_filter = [OnlineStatusFilter, "online"]
    search_fields = ["name", "nummer"]
    list_select_related = ["eltern__eltern"]  # der Name zeigt die übergeordneten Ebenen
    readonly_fields = ["im_shop"]  # nur lesbar: festgelegt wird die Zuordnung unter Shops → „Kategorien zuordnen“

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related("shop_zuordnungen__shop")

    @admin.display(description="Kategorie im Shop")
    def im_shop(self, obj):
        """Je Shop: von wwok angelegt oder einer vorhandenen Shop-Kategorie zugeordnet (Issue 59)."""
        return ", ".join(f"{z.shop.name}: {'angelegt' if z.angelegt else 'zugeordnet'} (Shop-ID {z.kategorie_id_extern})" for z in obj.shop_zuordnungen.all()) or "–"
    change_form_template = "admin/stammdaten/kategorie/change_form.html"

    @admin.display(description="Online", ordering="online")
    def online_status(self, obj):
        """Eine Spalte für Häkchen und Wirkung im Shop: grün = online, rot = offline, gelb = Häkchen gesetzt, aber
        über eine übergeordnete Kategorie offline."""
        grund = obj.offline_grund()
        if grund is None:
            bild, text, zusatz = "yes", "online", ""
        elif grund.pk == obj.pk:
            bild, text, zusatz = "no", "offline", ""
        else:
            bild, text, zusatz = "alert", f"offline über {grund.name}", f" über {grund.name}"
        return format_html('<img src="{}" alt="{}" title="{}">{}', static(f"admin/img/icon-{bild}.svg"), text, text, zusatz)

    def betroffene_artikel(self, obj):
        """Artikel, die im Shop stehen und beim Ausschalten dieser Kategorie (samt Unterkategorien) deaktiviert würden."""
        # betroffen: sichtbare Artikel dieser Kategorie (Haupt- oder weitere), die danach keine Online-Kategorie mehr hätten
        from django.db.models import Q
        ids = obj.mit_unterkategorien()
        kandidaten = m.Artikel.objects.shop_sichtbar().filter(Q(kategorie_id__in=ids) | Q(weitere_kategorien__in=ids)).distinct()
        offline = set(m.Kategorie.offline_ids()) | set(ids)
        return sum(1 for a in kandidaten.prefetch_related("weitere_kategorien")
                   if all(k in offline for k in [a.kategorie_id, *[w.pk for w in a.weitere_kategorien.all()]] if k))

    @admin.display(description="Artikel im Shop")
    def artikel_im_shop(self, obj):
        return self.betroffene_artikel(obj)

    def render_change_form(self, request, context, add=False, change=False, form_url="", obj=None):
        context["betroffen_artikel"] = self.betroffene_artikel(obj) if obj and obj.pk and obj.online else 0
        context["kategorie_war_online"] = bool(obj and obj.pk and obj.online)
        return super().render_change_form(request, context, add, change, form_url, obj)

    def save_model(self, request, obj, form, change):
        war_online = bool(change) and m.Kategorie.objects.filter(pk=obj.pk, online=True).exists()
        betroffen = self.betroffene_artikel(obj) if war_online and not obj.online else None
        super().save_model(request, obj, form, change)
        if betroffen is not None:
            messages.warning(request, f"{betroffen} Artikel werden im Shop deaktiviert (beim nächsten Abgleich, oder sofort mit „Bestand & Preise senden“).")


@admin.register(m.Hersteller)
class HerstellerAdmin(StammImportMixin, admin.ModelAdmin):
    list_filter = ["online"]
    search_fields = ["name", "kennzeichen"]
    import_art = "hersteller"
    import_titel = "Hersteller importieren"
    import_hinweis = ("Pflicht: <strong>Name</strong>, dazu wahlweise <strong>Kennzeichen; Online</strong>.<ul>"
                      "<li>Vorhandenes Kennzeichen: Name und Online werden übernommen. Ohne Kennzeichen wird über den Namen verglichen.</li>"
                      "<li>Bei einem Fehler in einer Zeile wird nichts importiert.</li></ul>")

    def import_ausfuehren(self, dateiname, inhalt):
        from .stamm_import import hersteller_importieren
        neu, geaendert, gleich = hersteller_importieren(dateiname, inhalt)
        return f"{neu} neu, {geaendert} geändert, {gleich} unverändert."


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

    list_display = ["lieferant_kurz", "msku", "sku", "bezeichnung", "hersteller", "ek_anzeige", "lager", "a_artikel"]
    list_display_links = ["lieferant_kurz"]
    list_filter = ["lieferant"]
    search_fields = ["bezeichnung", "herstellernummer", "bestellnummer", "ean", "hersteller"]
    raw_id_fields = ["artikel"]
    list_select_related = ["lieferant", "artikel"]
    show_full_result_count = False  # bei Hunderttausenden Zeilen kein teures COUNT(*)

    @admin.display(description="Lieferant", ordering="lieferant__name1")
    def lieferant_kurz(self, obj):
        """Kurzname (sonst erstes Wort von Name 1, sonst Nummer); voller Text als Tooltip."""
        lf = obj.lieferant
        return format_html('<span title="{}">{}</span>', str(lf), lf.kurz)

    # Kurze Spaltenköpfe nur in der Liste (voller Begriff als Tooltip); Formulare behalten die langen Feldnamen.
    @admin.display(description=format_html('<span title="{}">{}</span>', "Herstellernummer", "MSKU"), ordering="herstellernummer")
    def msku(self, obj):
        return obj.herstellernummer

    @admin.display(description=format_html('<span title="{}">{}</span>', "Bestellnummer des Lieferanten", "SKU"), ordering="bestellnummer")
    def sku(self, obj):
        return obj.bestellnummer

    @admin.display(description=format_html('<span title="{}">{}</span>', "Einkaufspreis netto", "EK"), ordering="ek")
    def ek_anzeige(self, obj):
        return f"{obj.ek:.2f}".replace(".", ",")

    @admin.display(description=format_html('<span title="{}">{}</span>', "Bestand beim Lieferanten", "Lager"), ordering="bestand")
    def lager(self, obj):
        return obj.bestand

    @admin.display(description=format_html('<span title="{}">{}</span>', "Verknüpfter eigener Artikel", "A-Artikel"), ordering="artikel")
    def a_artikel(self, obj):
        return obj.artikel
