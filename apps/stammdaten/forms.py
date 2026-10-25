"""Formulare für die Bearbeitung von Kunden und Lieferanten im Dialog (Registerkarten statt Admin-Fieldsets)."""
from django import forms
from django.forms import inlineformset_factory

from .models import (Ansprechpartner, Kunde, KundeMandant, Kundenanschrift, Lieferant, LieferantMandant)

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
    ("bank", "Steuer & Bank", PARTNER_BANK + ["sepa_mandatsreferenz", "sepa_mandat_datum"]),
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
