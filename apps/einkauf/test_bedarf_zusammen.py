"""Wunsch des Nutzers 20.11.2026: Bestellvorschlag (Menü) und Tab „Bedarf“ zusammengeführt – eine Tabelle, Lieferant je Zeile
im Bestellvorschlag wählbar, Übersicht je Lieferant, Hinweis, wo die Ware schon bestellt ist."""
from decimal import Decimal as D

from django.urls import reverse

from apps.stammdaten.models import ArtikelLieferant, Lieferant

from . import services
from .models import BestellStatus, Bestellung
from .test_tabs import Basis


class BedarfZusammenTest(Basis):
    def test_vorschlag_lieferant_je_zeile_vorgewaehlt_der_guenstigste(self):
        self.kundenbestellung(self.maus, "3")
        r = self.client.get(reverse("einkauf:vorschlag"))
        self.assertTemplateUsed(r, "einkauf/_bedarf_tabelle.html")
        self.assertContains(r, f'name="lieferant_{self.maus.pk}"')
        self.assertContains(r, f'value="{self.lf2.pk}" data-ek="8.0000" data-kurz="WAVE" selected>WAVE · 8,00 €')
        self.assertContains(r, f'value="{self.lf.pk}" data-ek="9.0000" data-kurz="TD">TD · 9,00 €')
        self.assertContains(r, 'id="vorschlag-uebersicht"')

    def test_vorschlag_gewaehlter_lieferant_wird_bestellt(self):
        self.kundenbestellung(self.maus, "3")
        self.kundenbestellung(self.kabel, "2")
        r = self.client.post(reverse("einkauf:vorschlag"), {
            "auswahl": [self.maus.pk, self.kabel.pk], f"menge_{self.maus.pk}": "3", f"lieferant_{self.maus.pk}": self.lf.pk,
            f"menge_{self.kabel.pk}": "2", f"lieferant_{self.kabel.pk}": self.lf2.pk}, follow=True)
        self.assertContains(r, "2 Bestellung(en) angelegt")
        td = Bestellung.objects.get(lieferant=self.lf)
        self.assertEqual([(p.artikel, p.menge, p.ek) for p in td.positionen.all()], [(self.maus, D("3"), D("9.00"))])
        self.assertEqual(Bestellung.objects.get(lieferant=self.lf2).positionen.get().artikel, self.kabel)

    def test_vorschlag_lieferant_ohne_preis_abgelehnt(self):
        self.kundenbestellung(self.kabel, "2")  # TD führt das Kabel nicht
        r = self.client.post(reverse("einkauf:vorschlag"), {
            "auswahl": [self.kabel.pk], f"menge_{self.kabel.pk}": "2", f"lieferant_{self.kabel.pk}": self.lf.pk}, follow=True)
        self.assertContains(r, "Der gewählte Lieferant führt den Artikel nicht mit Preis.")
        self.assertFalse(Bestellung.objects.exists())

    def test_ohne_lieferantenwahl_wie_bisher_der_guenstigste(self):
        self.kundenbestellung(self.maus, "3")
        services.bestellungen_aus_vorschlag(self.m, [(self.maus.pk, D("3"))])
        self.assertEqual(Bestellung.objects.get().lieferant, self.lf2)

    def test_schon_bestellt_wird_angezeigt(self):
        self.kundenbestellung(self.maus, "5")
        entwurf = services.bestellung_anlegen(self.m, self.lf2)
        services.bestellposition(entwurf, self.maus, D("2"))
        bestellt = self.bestellt((self.maus, "1"))
        zeile = services.bedarf_zeilen(self.m)[0]
        self.assertEqual(zeile["menge"], D("2"))  # 5 − 2 − 1
        self.assertEqual([(u["bestellung"], u["menge"]) for u in zeile["unterwegs"]], [(entwurf, D("2")), (bestellt, D("1"))])
        for url in (reverse("einkauf:vorschlag"), reverse("einkauf:bestellung", args=[entwurf.pk])):
            r = self.client.get(url)
            self.assertContains(r, "schon im Entwurf")
            self.assertContains(r, reverse("einkauf:bestellung", args=[entwurf.pk]))
            self.assertContains(r, "bestellt <a")

    def test_gesperrter_lieferant_nicht_waehlbar(self):
        gesp = Lieferant.objects.create(nummer="L9", name1="Gesperrt AG", kurzname="GESP", gesperrt=True)
        ArtikelLieferant.objects.create(artikel=self.maus, lieferant=gesp, bestellnummer="G", ek=D("1"))
        self.kundenbestellung(self.maus, "1")
        self.assertNotContains(self.client.get(reverse("einkauf:vorschlag")), "GESP")
        r = self.client.post(reverse("einkauf:vorschlag"), {
            "auswahl": [self.maus.pk], f"menge_{self.maus.pk}": "1", f"lieferant_{self.maus.pk}": gesp.pk}, follow=True)
        self.assertContains(r, "führt den Artikel nicht mit Preis")
        self.assertFalse(Bestellung.objects.filter(status=BestellStatus.ENTWURF).exists())
