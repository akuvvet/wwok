from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.forms import BaseInlineFormSet, ModelForm
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.utils import timezone

from apps.kern.models import Mandant
from apps.stammdaten.models import Stammnummer

from .mail import PLATZHALTER, STANDARD, vorlage_fehler, vorlagen_anlegen
from .models import (Abo, AboPosition, Beleg, BelegUebernahme, Briefprodukt, Mailart, MailVersand, Mailvorlage, Mengenregel, Nummernkreis,
                     Position, Versandart, Wiedervorlage)


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


class MailvorlageForm(ModelForm):
    def clean(self):
        daten = super().clean()
        fehler = vorlage_fehler(self.instance.art, daten.get("betreff", ""), daten.get("text", ""))
        if fehler:
            raise ValidationError(fehler)
        return daten


@admin.register(Mailvorlage)
class MailvorlageAdmin(admin.ModelAdmin):
    """Je Mandant steht jede Mailart genau einmal in der Liste; fehlende entstehen beim Öffnen mit dem Standardtext."""

    form = MailvorlageForm
    list_display = ["mandant", "art", "betreff"]
    list_filter = ["mandant"]
    ordering = ["mandant", "art"]
    fields = ["mandant", "art", "betreff", "text", "platzhalter"]
    readonly_fields = ["mandant", "art", "platzhalter"]
    actions = ["standard_setzen"]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        if self.has_change_permission(request):
            vorlagen_anlegen()
        return super().changelist_view(request, extra_context)

    @admin.display(description="Mögliche Platzhalter")
    def platzhalter(self, obj):
        liste = " ".join("{" + name + "}" for name in PLATZHALTER[obj.art])
        return liste + " – {mahntext} ist der Text der Mahnstufe" if obj.art == Mailart.MAHNUNG else liste

    @admin.action(description="Auf Standardtext zurücksetzen", permissions=["change"])
    def standard_setzen(self, request, queryset):
        for vorlage in queryset:
            vorlage.betreff, vorlage.text = STANDARD[vorlage.art]
            vorlage.save()
        self.message_user(request, f"{len(queryset)} Vorlagen zurückgesetzt.", messages.SUCCESS)


@admin.register(BelegUebernahme)
class BelegUebernahmeAdmin(admin.ModelAdmin):
    """Nur lesen – Verlauf der Teilübernahmen aus Bestellungen."""

    list_display = ["am", "quelle_nummer", "ziel_art", "artikelnummer", "bezeichnung", "menge", "richtung", "benutzer"]
    list_filter = ["richtung", "mandant"]
    search_fields = ["quelle_nummer", "artikelnummer", "bezeichnung"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


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
    """Eine Seite für alle laufenden Nummern; ein freies Bearbeiten des Zählers würde die Prüfungen in naechste_setzen umgehen."""

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def change_view(self, request, object_id, form_url="", extra_context=None):
        return redirect("admin:verkauf_nummernkreis_changelist")

    def zeilen(self, mandanten):
        stand = {(k.mandant_id, k.art): k.letzte_nummer for k in Nummernkreis.objects.all()}
        jahr = timezone.localdate().year
        belege = [{"titel": titel, "zellen": [
            {"name": f"beleg-{m.pk}-{art}", "wert": stand.get((m.pk, art), 0) + 1, "gesperrt": Nummernkreis.gesperrt(m, art, jahr),
             "muster": f"{art}{jahr}-{stand.get((m.pk, art), 0) + 1:05d}", "mandant": m, "art": art} for m in mandanten]}
            for art, titel in Nummernkreis.ARTEN.items()]
        stamm = [{"titel": titel, "name": f"stamm-{art}", "wert": Stammnummer.vorschlag(art), "art": art} for art, titel in Stammnummer.Art.choices]
        return belege, stamm

    def einstellen(self, request, titel, name, bisher, setzen) -> bool:
        """Setzt eine Zeile nur, wenn der Nutzer sie geändert hat: Die Seite kann veraltet sein und sendet immer alle Felder.
        Fehler stehen als Meldung über der Seite."""
        text, angezeigt = request.POST.get(name, "").strip(), request.POST.get(f"alt-{name}")
        if not text or text == angezeigt:
            return False
        if not (text.isascii() and text.isdigit() and len(text) <= 18):
            messages.error(request, f"{titel}: „{text[:30]}“ ist keine Nummer.")
            return False
        if int(text) == int(bisher):
            return False
        if angezeigt != str(bisher):
            messages.error(request, f"{titel}: Der Stand hat sich inzwischen geändert (jetzt {bisher}). Bitte neu eintragen.")
            return False
        try:
            setzen(int(text))
        except ValueError as fehler:
            messages.error(request, f"{titel}: {fehler}")
            return False
        return True

    def changelist_view(self, request, extra_context=None):
        if not self.has_change_permission(request):
            raise PermissionDenied
        mandanten = list(Mandant.objects.order_by("kurzname"))
        belege, stamm = self.zeilen(mandanten)
        if request.method == "POST":
            geaendert = 0
            for zeile in belege:
                for z in zeile["zellen"]:
                    geaendert += self.einstellen(request, f"{zeile['titel']} {z['mandant'].kurzname}", z["name"], z["wert"],
                                                 lambda n: Nummernkreis.naechste_setzen(z["mandant"], z["art"], n))
            for z in stamm:
                geaendert += self.einstellen(request, z["titel"], z["name"], z["wert"], lambda n: Stammnummer.setzen(z["art"], n))
            if geaendert:
                messages.success(request, f"{geaendert} laufende Nummer(n) eingestellt.")
            return redirect("admin:verkauf_nummernkreis_changelist")
        return TemplateResponse(request, "admin/verkauf/nummernkreis/laufende_nummern.html", {
            **self.admin_site.each_context(request), "title": "Laufende Nummern", "opts": self.model._meta, "mandanten": mandanten,
            "belege": belege, "stamm": stamm, "jahr": timezone.localdate().year})


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
    """Versandarten („vers“): Nummer und Standardkosten; Kunden und Lieferanten wählen sie als Standard-Versandart."""

    list_display = ["nummer", "name", "dienstleister", "versandkosten", "verpackung", "versicherung", "frachtfrei_ab", "nur_inland", "aktiv"]
    list_display_links = ["nummer", "name"]
    list_editable = ["versandkosten", "verpackung", "versicherung"]
    search_fields = ["name", "nummer"]

@admin.register(Wiedervorlage)
class WiedervorlageAdmin(admin.ModelAdmin):
    list_display = ["faellig", "kunde", "text", "erledigt", "mandant"]
    list_filter = ["mandant", "erledigt"]


@admin.register(Briefprodukt)
class BriefproduktAdmin(admin.ModelAdmin):
    """Porto der Deutschen Post (INTERNETMARKE); bei einer Portoerhöhung den Preis hier anpassen."""

    list_display = ["name", "preis", "max_gewicht_g", "masse", "produktcode", "aktiv"]
    list_editable = ["preis", "aktiv"]
