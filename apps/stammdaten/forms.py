"""Formulare für die Bearbeitung von Kunden und Lieferanten im Dialog (Registerkarten statt Admin-Fieldsets)."""
from django import forms
from django.forms import inlineformset_factory

from .models import (Ansprechpartner, Artikel, ArtikelMandant, Artikeltyp, Kunde, KundeMandant, Kundenanschrift, Lieferant, LieferantMandant)

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
        ("konditionen", "Konditionen & Hinweis", ["zahlungsbedingung", "mindestbestellwert", "versandkostenfrei_ab", "lieferzeit_tage",
                                                  "gesperrt", "hinweis"]),
        ("bank", "Steuer & Bank", PARTNER_BANK),
    ]

    class Meta:
        model = Lieferant
        fields = (["nummer", "kurzname"] + PARTNER_ADRESSE
                  + ["zahlungsbedingung", "mindestbestellwert", "versandkostenfrei_ab", "lieferzeit_tage", "gesperrt", "hinweis"]
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
    ("grund", "Stammdaten", ["nummer", "typ", "bezeichnung", "bezeichnung_erweitert", "kategorie", "hersteller", "herstellernummer",
                             "ean", "mengeneinheit", "steuersatz", "nicht_aktiv"]),
    ("preise", "Preise", ["vk1", "vk2", "vk3", "aufschlag_vk1", "aufschlag_vk2", "aufschlag_vk3"]),
    ("logistik", "Maße & Versand", ["laenge_cm", "breite_cm", "hoehe_cm", "gewicht_kg", "sperrgut", "zolltarifnummer",
                                    "garantie_monate", "eol_datum", "nachfolger", "esd_key"]),
    ("online", "Online & Texte", ["online", "kurzbeschreibung", "keywords", "meta_titel", "meta_description", "meta_keywords"]),
]


class ArtikelForm(forms.ModelForm):
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
