"""Formulare für die Bearbeitung von Kunden und Lieferanten im Dialog (Registerkarten statt Admin-Fieldsets)."""
from django import forms
from django.forms import inlineformset_factory
from django.utils import formats, timezone

from .models import (Ansprechpartner, Artikel, ArtikelMandant, Artikeltyp, EkHerkunft, Kunde, KundeMandant, Kundenanschrift, Lieferant,
                     LieferantMandant, _ek_quelle)

from .preise import stufenpreis

DATUM = forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"})


def datum_felder(form):
    for feld in form.fields.values():
        if isinstance(feld, forms.DateField):
            feld.widget = forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"})
        if isinstance(feld.widget, forms.Textarea):
            feld.widget.attrs["rows"] = 3


PARTNER_ADRESSE = ["name1", "name2", "name3", "strasse", "plz", "ort", "land", "telefon", "email", "email_weitere", "webseite",
                   "gesellschaftsform", "inhaber_gf"]
PARTNER_BANK = ["ust_id", "ust_id_geprueft_am", "kontoinhaber", "bank", "iban", "bic"]


KUNDE_TABS = [
    ("grund", "Grunddaten", ["nummer"] + PARTNER_ADRESSE),
    ("einstufung", "Einstufung & Hinweis", ["kundenart", "preisstufe", "zahlungsbedingung", "gesperrt", "hinweis"]),
    ("bank", "Steuer & Bank", PARTNER_BANK + ["sepa_mandatsreferenz", "sepa_mandat_datum", "leitweg_id"]),
]


class KundeForm(forms.ModelForm):
    TABS = KUNDE_TABS  # Registerkarten: (Kennung, Titel, Felder)

    class Meta:
        model = Kunde
        fields = [f for _, _, felder in KUNDE_TABS for f in felder]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        datum_felder(self)
        self.fields["land"].help_text = "Ländercode, z. B. DE"
        self.fields["email_weitere"].widget.attrs["rows"] = 2


class LieferantForm(forms.ModelForm):
    TABS = [
        ("grund", "Grunddaten", ["nummer", "kurzname"] + PARTNER_ADRESSE),
        ("konditionen", "Konditionen & Hinweis", ["zahlungsbedingung", "mindestbestellwert", "versandkosten", "versandkostenfrei_ab",
                                                  "lieferzeit_tage", "gesperrt", "hinweis"]),
        ("bank", "Steuer & Bank", PARTNER_BANK),
    ]

    class Meta:
        model = Lieferant
        fields = (["nummer", "kurzname"] + PARTNER_ADRESSE
                  + ["zahlungsbedingung", "mindestbestellwert", "versandkosten", "versandkostenfrei_ab", "lieferzeit_tage", "gesperrt", "hinweis"]
                  + PARTNER_BANK)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        datum_felder(self)
        self.fields["land"].help_text = "Ländercode, z. B. DE"
        self.fields["email_weitere"].widget.attrs["rows"] = 2


class KundeMandantForm(forms.ModelForm):
    class Meta:
        model = KundeMandant
        fields = ["kreditlimit", "newsletter"]


class LieferantMandantForm(forms.ModelForm):
    class Meta:
        model = LieferantMandant
        fields = ["unsere_kundennummer"]


AP_FELDER = ["anrede", "vorname", "nachname", "abteilung", "email", "telefon", "durchwahl", "mobil", "hauptkontakt"]

AnsprechpartnerKundeSet = inlineformset_factory(
    Kunde, Ansprechpartner, fk_name="kunde", fields=AP_FELDER + ["shop_zugang"], extra=3, can_delete=True)
AnsprechpartnerLieferantSet = inlineformset_factory(
    Lieferant, Ansprechpartner, fk_name="lieferant", fields=AP_FELDER, extra=3, can_delete=True)
AnschriftSet = inlineformset_factory(
    Kunde, Kundenanschrift, fields=["art", "standard", "name1", "name2", "strasse", "plz", "ort", "land"], extra=3, can_delete=True)

ARTIKEL_TABS = [
    ("grund", "Stammdaten", ["nummer", "typ", "bezeichnung", "bezeichnung_erweitert", "kategorie", "leistungskategorie", "hersteller", "herstellernummer",
                             "ean", "mengeneinheit", "steuersatz", "nicht_aktiv"]),
    ("preise", "Preise", ["kosten", "ek_manuell", "vk1_fuer_alle", "vk1", "vk2", "vk3", "aufschlag_vk1", "aufschlag_vk2", "aufschlag_vk3"]),
    ("logistik", "Maße & Versand", ["laenge_cm", "breite_cm", "hoehe_cm", "gewicht_kg", "sperrgut", "zolltarifnummer",
                                    "garantie_monate", "eol_datum", "nachfolger", "esd_key"]),
    ("online", "Online & Texte", ["online", "kurzbeschreibung", "keywords", "meta_titel", "meta_description", "meta_keywords"]),
]


class VkVorschlagForm(forms.ModelForm):
    """Leere VK zeigen den Vorschlag aus EK und Aufschlag. Bleibt er unverändert, wird nichts Festes gespeichert und der
    VK folgt weiter dem EK; erst ein selbst eingetippter Preis ist fest."""

    vk1_fuer_alle = forms.BooleanField(label="VK1 für alle VKs übernehmen", required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # EK als Basis für die Live-Umrechnung VK ↔ Aufschlag in der Maske (wie stufenpreis: ohne Leistungskosten)
        ek, quelle = _ek_quelle(self.instance, mit_kosten=False) if self.instance.pk else (None, "")
        for stufe in (1, 2, 3):
            if f"vk{stufe}" in self.fields:
                self.fields[f"vk{stufe}"].widget.attrs.update({"data-ek": "" if ek is None else str(ek), "data-ek-quelle": quelle})
        if self.instance.pk and self.instance.typ != Artikeltyp.L:
            for stufe in (1, 2, 3):
                feld, preis = f"vk{stufe}", stufenpreis(self.instance, stufe)
                if feld in self.fields and getattr(self.instance, feld) is None and preis is not None:
                    self.initial[feld] = preis
                    self.fields[feld].show_hidden_initial = True  # beim Speichern zählt, was angezeigt war, nicht der aktuelle Vorschlag
                    self.fields[feld].widget.attrs["data-vorschlag"] = str(preis)
                    self.fields[feld].help_text = "Vorschlag aus EK und Aufschlag, folgt dem EK. Ein eigener Preis bleibt fest."

    def clean(self):
        daten = super().clean()
        if daten.get("vk1_fuer_alle"):
            if daten.get("vk1") is None:
                self.add_error("vk1_fuer_alle", "Bitte zuerst VK1 eintragen.")
            else:
                daten["vk2"] = daten["vk3"] = daten["vk1"]
            return daten
        for feld in ("vk1", "vk2", "vk3"):
            if self.instance.pk and feld in self.fields and getattr(self.instance, feld) is None:
                try:
                    if daten.get(feld) == self.fields[feld].to_python(self.data.get(self.add_initial_prefix(feld))):
                        daten[feld] = None
                except forms.ValidationError:
                    pass
        return daten


class ArtikelForm(VkVorschlagForm):
    TABS = ARTIKEL_TABS

    class Meta:
        model = Artikel
        fields = [f for _, _, felder in ARTIKEL_TABS for f in felder]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        datum_felder(self)
        # P-Artikel sind eine eigene Tabelle (Preislisten); hier nur A, L und G
        self.fields["typ"].choices = [(w, n) for w, n in Artikeltyp.choices if w != Artikeltyp.P]
        if self.instance.pk:  # Typwechsel würde Bestand und Belege verfälschen
            self.fields["typ"].disabled = True
            self.fields["nachfolger"].queryset = Artikel.objects.exclude(pk=self.instance.pk)
        self.fields["nachfolger"].queryset = self.fields["nachfolger"].queryset.order_by("nummer")
        self.ek_feld_einrichten()

    def ek_feld_einrichten(self):
        """EK-Preis netto mit Komma; nach dem ersten Wareneingang gilt der Ø-EK und das Feld ist nur Anzeige."""
        feld = self.fields["ek_manuell"]
        feld.localize, feld.widget = True, forms.TextInput(attrs={"inputmode": "decimal", "autocomplete": "off"})
        feld.widget.is_localized = True
        feld.error_messages["invalid"] = "Bitte einen Betrag wie 12,50 eingeben."
        a = self.instance
        if not a.pk:
            return
        from apps.lager.models import Bewegungsart, Lagerbewegung
        if Lagerbewegung.objects.filter(artikel=a, art=Bewegungsart.WARENEINGANG).exists():
            feld.disabled = True
        ek, quelle = _ek_quelle(a, mit_kosten=False)
        if ek is None:
            feld.help_text = "EK fehlt – bitte EK-Preis eintragen."
        else:
            text = f"Aktueller EK: {formats.number_format(ek, 2, use_l10n=True)} € ({quelle})"
            datum = a.ek_datum(quelle)
            if datum:
                text += f" vom {formats.date_format(timezone.localtime(datum), 'd.m.Y')}"
            if feld.disabled:
                text += " – nach dem Wareneingang gilt der Ø-EK, das Feld ist gesperrt."
            elif quelle != "manuell":
                text += " – dieser EK hat Vorrang vor dem manuellen EK."
            feld.help_text = text

    def clean_ek_manuell(self):
        ek = self.cleaned_data.get("ek_manuell")
        if ek is not None and ek < 0:
            raise forms.ValidationError("Der EK-Preis darf nicht negativ sein.")
        return ek

    def save(self, commit=True):
        # Herkunft „manuell“ nur, wenn der manuelle EK auch gilt; ein Ø-EK (Import, Wareneingang) behält seine Herkunft
        if "ek_manuell" in self.changed_data and self.instance.typ == Artikeltyp.A and not self.instance.durchschnitts_ek:
            self.instance.ek_herkunft_setzen(EkHerkunft.MANUELL)
        return super().save(commit)

    def clean(self):
        daten = super().clean()
        if daten.get("esd_key"):  # ESD-Keys nur für Typ A der Kategorie Software (wie in der ESD-Verwaltung)
            from apps.esd.views import software_kategorien
            ids = software_kategorien()
            kat = daten.get("kategorie")
            if daten.get("typ", self.instance.typ) != Artikeltyp.A or (ids is not None and (kat is None or kat.pk not in ids)):
                self.add_error("esd_key", "ESD-Keys gibt es nur für Lagerartikel (A) der Kategorie Software.")
        return daten


class ArtikelMandantForm(forms.ModelForm):
    class Meta:
        model = ArtikelMandant
        fields = ["lagerplatz", "mindestbestand", "maximalbestand"]

    def clean(self):
        daten = super().clean()
        mi, ma = daten.get("mindestbestand"), daten.get("maximalbestand")
        if mi is not None and ma is not None and ma < mi:
            self.add_error("maximalbestand", "Der Maximalbestand darf nicht unter dem Mindestbestand liegen.")
        return daten
