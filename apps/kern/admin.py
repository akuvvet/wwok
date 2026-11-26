from django.contrib import admin, messages
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin as BasisUserAdmin

from django import forms
from django.core.exceptions import PermissionDenied
from django.http import HttpResponseRedirect
from django.utils.html import format_html

from .layout import aussehen_zuruecksetzen, hausfarbe
from .models import AenderungsProtokoll, Listenansicht, Mandant, Plattformkonto, Zahlungskonto

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

    smtp_passwort_neu = forms.CharField(
        label="SMTP-Passwort", required=False, strip=False,
        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "new-password", "size": 40}),
        help_text="Passwort des Mail-Postfachs. Wird verschlüsselt gespeichert und nie wieder angezeigt. "
                  "Leer lassen = das hinterlegte Passwort bleibt unverändert.")
    smtp_passwort_loeschen = forms.BooleanField(
        label="Hinterlegtes Passwort entfernen", required=False,
        help_text="Danach gilt wieder ein Passwort aus der .env auf dem Server, falls dort eines steht.")

    def save(self, commit=True):
        if self.cleaned_data.get("smtp_passwort_neu"):
            self.instance.smtp_passwort_setzen(self.cleaned_data["smtp_passwort_neu"])
        elif self.cleaned_data.get("smtp_passwort_loeschen"):
            self.instance.smtp_passwort_setzen("")
        return super().save(commit)

    def clean_kostenstellen_konten(self):
        text = self.cleaned_data.get("kostenstellen_konten", "")
        for nummer, zeile in enumerate(text.splitlines(), start=1):
            if not zeile.strip():
                continue
            stelle, _, konto = zeile.partition("=")
            if not stelle.strip() or not konto.strip().isdigit():
                raise forms.ValidationError(f"Zeile {nummer}: bitte „Kostenstelle = Kontonummer“, z. B. „Apetito = 8401“.")
        return text

    def clean_logo(self):
        logo = self.cleaned_data.get("logo")
        if logo and hasattr(logo, "size") and logo.size > LOGO_MAX_BYTES:
            raise forms.ValidationError("Das Logo ist größer als 2 MB.")
        return logo



def _geheimfeld(label, hilfe=""):
    return forms.CharField(label=label, required=False, strip=True,
                           widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "new-password", "size": 40}),
                           help_text=(hilfe + " " if hilfe else "") + "Wird verschlüsselt gespeichert und nie wieder angezeigt; leer = unverändert.")


class ZahlungskontoForm(forms.ModelForm):
    class Meta:
        model = Zahlungskonto
        fields = "__all__"

    paypal_secret_neu = _geheimfeld("REST-API Secret", "developer.paypal.com → Apps & Credentials (Live) → Secret.")
    paypal_secret_loeschen = forms.BooleanField(label="Secret entfernen", required=False)

    def clean(self):
        daten = super().clean()
        if daten.get("art") != Zahlungskonto.Art.KREDITKARTE:
            for feld in ("karte_letzte4", "karte_ablauf", "karte_inhaber"):
                if daten.get(feld):
                    self.add_error(feld, "Nur bei Art „Kreditkarte“.")
        return daten

    def save(self, commit=True):
        if self.cleaned_data.get("paypal_secret_neu"):
            self.instance.secret_setzen(self.cleaned_data["paypal_secret_neu"])
        elif self.cleaned_data.get("paypal_secret_loeschen"):
            self.instance.secret_setzen("")
        return super().save(commit)


class ZahlungskontoInline(admin.StackedInline):
    model = Zahlungskonto
    form = ZahlungskontoForm
    extra = 0
    readonly_fields = ["status"]
    fieldsets = [
        (None, {"fields": ["art", "bezeichnung", "aktiv", "datev_konto", "status"]}),
        ("Bankkonto", {"fields": ["bank_name", "iban", "bic", "kontonummer"], "classes": ["collapse"]}),
        ("PayPal", {"fields": ["paypal_email", "paypal_haendler_id", "paypal_umgebung", "paypal_client_id",
                               "paypal_secret_neu", "paypal_secret_loeschen"], "classes": ["collapse"]}),
        ("Kreditkarte (nie die volle Nummer oder die Prüfziffer eintragen)", {
            "fields": ["karte_anbieter", "karte_inhaber", "karte_letzte4", "karte_ablauf", "karte_bank", "karte_abrechnung_iban"],
            "classes": ["collapse"]}),
        (None, {"fields": ["bemerkung"]}),
    ]

    @admin.display(description="Stand")
    def status(self, obj):
        if not obj or not obj.pk:
            return "– nach dem Speichern –"
        fehlt = obj.fehlende_angaben()
        geheim = " · Secret hinterlegt (verschlüsselt)" if obj.art == Zahlungskonto.Art.PAYPAL and obj.secret else ""
        return ("vollständig ✓" if not fehlt else "fehlt: " + ", ".join(fehlt)) + geheim


class PlattformkontoForm(forms.ModelForm):
    class Meta:
        model = Plattformkonto
        fields = "__all__"

    client_secret_neu = _geheimfeld("Client Secret / Secret Key",
                                    "eBay: Cert-ID. Amazon: LWA Client Secret. Kaufland: Secret Key (nur einmal sichtbar beim Erzeugen).")
    refresh_token_neu = _geheimfeld("Refresh-Token",
                                    "eBay: aus der Freigabe (User Token). Amazon: Seller Central → Apps → App autorisieren. Kaufland: nicht nötig.")
    geheimnisse_loeschen = forms.BooleanField(label="Secret und Token entfernen", required=False)

    def save(self, commit=True):
        if self.cleaned_data.get("geheimnisse_loeschen"):
            self.instance.geheimnis_setzen("client_secret", "")
            self.instance.geheimnis_setzen("refresh_token", "")
        for feld in ("client_secret", "refresh_token"):
            if self.cleaned_data.get(f"{feld}_neu"):
                self.instance.geheimnis_setzen(feld, self.cleaned_data[f"{feld}_neu"])
        return super().save(commit)


class PlattformkontoInline(admin.StackedInline):
    model = Plattformkonto
    form = PlattformkontoForm
    extra = 0
    readonly_fields = ["status"]
    fieldsets = [
        (None, {"fields": ["plattform", "bezeichnung", "aktiv", "umgebung", "datev_konto", "status"]}),
        ("Konto und Zugang", {"fields": ["kontoname", "haendler_id", "marktplatz", "client_id", "client_secret_neu",
                                         "refresh_token_neu", "refresh_token_bis", "geheimnisse_loeschen"]}),
        ("Nur eBay", {"fields": ["ebay_dev_id", "ebay_runame", "ebay_richtlinie_versand", "ebay_richtlinie_zahlung",
                                 "ebay_richtlinie_ruecknahme", "ebay_lagerort"], "classes": ["collapse"]}),
        ("Angebot", {"fields": ["preisstufe", "preisaufschlag", "bestand_puffer", "bemerkung"]}),
    ]

    @admin.display(description="Stand")
    def status(self, obj):
        if not obj or not obj.pk:
            return "– nach dem Speichern –"
        fehlt = obj.fehlende_angaben()
        teile = ["bereit für die Anbindung ✓" if not fehlt else "fehlt: " + ", ".join(fehlt)]
        if obj.refresh_token_bis:
            from django.utils import timezone
            tage = (obj.refresh_token_bis - timezone.localdate()).days
            if tage < 30:
                teile.append(f"Refresh-Token läuft {'ab' if tage < 0 else f'in {tage} Tagen ab'} – neu freigeben")
        return " · ".join(teile)


@admin.register(Mandant)
class MandantAdmin(admin.ModelAdmin):
    inlines = [ZahlungskontoInline, PlattformkontoInline]
    form = MandantForm
    list_display = ["kurzname", "firmenname", "ort", "aktiv"]
    readonly_fields = ["erechnung_bereit", "shopware_shops", "logo_vorschau", "aussehen_zuruecksetzen_knopf", "smtp_passwort_status", "testmail_knopf", "angelegt_am", "geaendert_am", "geaendert_von"]
    actions = ["aussehen_zuruecksetzen"]

    @admin.display(description="E-Rechnung")
    def erechnung_bereit(self, obj):
        """Was dem Mandanten für ZUGFeRD und XRechnung fehlt (Pflichtangaben des Verkäufers)."""
        if not obj or not obj.pk:
            return "– nach dem ersten Speichern –"
        from apps.verkauf.zugferd import mandant_luecken
        zugferd, xrechnung = mandant_luecken(obj)
        if not zugferd and not xrechnung:
            return "bereit für ZUGFeRD und XRechnung ✓"
        teile = []
        if zugferd:
            teile.append("ZUGFeRD fehlt: " + ", ".join(zugferd))
        if xrechnung:
            teile.append("XRechnung zusätzlich: " + ", ".join(xrechnung))
        return " · ".join(teile)

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

    def get_actions(self, request):
        aktionen = super().get_actions(request)
        aktionen.pop("delete_selected", None)  # Mandanten nur einzeln und mit Masterpasswort löschen
        return aktionen

    def delete_view(self, request, object_id, extra_context=None):
        """Löschen nur mit Masterpasswort (Hash in der .env, Wunsch des Nutzers 25.11.2026)."""
        from . import masterpasswort
        if not masterpasswort.eingerichtet():
            messages.error(request, "Mandanten lassen sich nur mit Masterpasswort löschen – auf dem Server ist keines eingerichtet "
                                    "(python manage.py masterpasswort_hash, Zeile in die .env).")
            return HttpResponseRedirect(request.path.rsplit("/delete/", 1)[0] + "/change/")
        if request.method == "POST" and not masterpasswort.pruefen(request.POST.get("masterpasswort", "")):
            messages.error(request, "Masterpasswort falsch – nichts gelöscht.")
            return HttpResponseRedirect(request.path)
        return super().delete_view(request, object_id, {**(extra_context or {}), "masterpasswort_noetig": True})

    def change_view(self, request, object_id, form_url="", extra_context=None):
        if request.method == "POST" and "_aussehen_zuruecksetzen" in request.POST:
            mandant = self.get_object(request, object_id)
            if mandant is None or not self.has_change_permission(request, mandant):
                raise PermissionDenied
            aussehen_zuruecksetzen(mandant)
            self.message_user(request, f"Aussehen von {mandant.kurzname} zurückgesetzt.", messages.SUCCESS)
            return HttpResponseRedirect(request.path)
        if request.method == "POST" and "_testmail" in request.POST:
            mandant = self.get_object(request, object_id)
            if mandant is None or not self.has_change_permission(request, mandant):
                raise PermissionDenied
            self._testmail(request, mandant)
            return HttpResponseRedirect(request.path)
        return super().change_view(request, object_id, form_url, extra_context)

    def _testmail(self, request, mandant):
        from apps.verkauf.mail import MailFehler, senden_an  # verkauf baut auf kern auf, daher erst hier

        an = request.user.email or mandant.absender_email
        if not an:
            self.message_user(request, f"Testmail für {mandant.kurzname}: Bitte zuerst die Absenderadresse eintragen und speichern.",
                              messages.ERROR)
            return
        try:
            senden_an(mandant, an, f"Testmail {mandant.kurzname}",
                      f"Diese Testmail bestätigt, dass der E-Mail-Versand für {mandant.firmenname} eingerichtet ist.",
                      bezug="Testmail", benutzer=request.user)
        except MailFehler as fehler:
            self.message_user(request, f"Testmail für {mandant.kurzname}: {fehler}", messages.ERROR)
        else:
            self.message_user(request, f"Testmail für {mandant.kurzname} an {an} gesendet.", messages.SUCCESS)

    @admin.display(description="Passwort")
    def smtp_passwort_status(self, obj):
        quelle = obj.smtp_passwort_quelle if obj and obj.pk else ""
        if quelle == "verwaltung":
            return "hinterlegt (in der Verwaltung, verschlüsselt)"
        if quelle == "env":
            return "hinterlegt (in der .env auf dem Server)"
        if obj and obj.smtp_passwort_geheim:
            return "nicht lesbar – bitte neu eintragen (der Schlüssel der Anwendung wurde geändert)"
        return "nicht hinterlegt"

    @admin.display(description="Testen")
    def testmail_knopf(self, obj):
        if not obj or not obj.pk:
            return "– nach dem ersten Speichern verfügbar –"
        return format_html(
            '<button type="submit" name="_testmail" value="1" class="button" formnovalidate>Testmail senden</button> '
            '<span class="help">Sendet mit den gespeicherten Einstellungen an die eigene Adresse '
            '(sonst an die Absenderadresse). Geänderte Felder vorher speichern.</span>')
    fieldsets = [
        ("Firma", {"fields": ["kurzname", "firmenname", "logo", "logo_vorschau", "strasse", "plz", "ort", "land", "telefon", "email", "aktiv"]}),
        ("Artikel und Preise", {"fields": ["standard_vk"]}),
        ("Aussehen", {"fields": ["farbe", "flaechenmodus", "farbe_kopf", "farbe_leiste", "farbe_karte", "aussehen_zuruecksetzen_knopf"],
                      "description": "Layoutfarbe = Akzent (Links, Knöpfe, aktive Menüzeile). Flächenfarben = Hintergrund von Kopfbereich, "
                                     "Seitenleiste und Karten – neutral, in gleicher Tonart oder je Bereich frei gewählt."}),
        ("Steuer und Bank", {"fields": ["ust_id", "steuernummer", "iban", "bic", "glaeubiger_id", "erechnung_bereit"]}),
        ("E-Mail-Versand", {"fields": ["smtp_host", "smtp_port", "smtp_sicherheit", "smtp_benutzer", "smtp_passwort_status",
                                       "smtp_passwort_neu", "smtp_passwort_loeschen", "absender_email", "absender_name",
                                       "kopie_an", "testmail_knopf"],
                            "description": "Zugang zum Mail-Postfach dieses Mandanten. Alle Angaben einschließlich Passwort "
                                           "werden hier eingetragen; mit „Testmail senden“ lässt sich der Versand prüfen."}),
        ("DATEV und Konten", {"fields": ["datev_berater", "datev_mandant", "datev_zahlungen", "konto_erloes_19", "konto_erloes_7", "konto_erloes_0",
                                         "konto_eg_lieferung", "konto_eg_leistung", "konto_ausfuhr", "konto_bank", "konto_kasse", "konto_skonto", "kostenstellen_konten"]}),
        ("Online-Shops (Shopware)", {"fields": ["shopware_shops"],
                                     "description": "Shopware-Shops haben eine eigene Anbindung (Bestand, Preise, Bestellungen)."}),
        ("Verlauf", {"fields": ["angelegt_am", "geaendert_am", "geaendert_von"]}),
    ]

    @admin.display(description="Shopware")
    def shopware_shops(self, obj):
        from django.urls import reverse
        from django.utils.html import format_html_join
        neu = reverse("admin:shop_shop_add")
        if not obj or not obj.pk:
            return "– nach dem ersten Speichern –"
        shops = list(obj.shops.all())
        liste = format_html_join(" · ", '<a href="{}">{}</a>{}', ((reverse("admin:shop_shop_change", args=[s.pk]), s.name,
                                                                   "" if s.aktiv else " (inaktiv)") for s in shops))
        return format_html('{}{}<a href="{}?mandant={}">+ Shopware-Shop anlegen</a>', liste, " · " if shops else "", neu, obj.pk)

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
