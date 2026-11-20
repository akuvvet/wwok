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

    def test_herkunft_nur_intern_nicht_auf_dem_pdf(self):
        """Wunsch 20.11.2026: „Bestellvorschlag“ stand im Lieferantentext und damit auf dem PDF."""
        import io
        from pypdf import PdfReader
        from .pdf import bestellung_pdf
        self.kundenbestellung(self.maus, "3")
        services.bestellungen_aus_vorschlag(self.m, [(self.maus.pk, D("3"))])
        b = Bestellung.objects.get()
        self.assertEqual((b.bemerkung, b.notiz), ("", "Bestellvorschlag"))
        text = "\n".join(s.extract_text() or "" for s in PdfReader(io.BytesIO(bestellung_pdf(b))).pages)
        self.assertNotIn("Bestellvorschlag", text)

    def test_versand_tab_rechnet_live(self):
        """Wunsch 20.11.2026: Nebenkosten und Gesamt rechnen sich beim Tippen (gemeinsame Funktion templates/rechnen.html)."""
        b = services.bestellung_anlegen(self.m, self.lf2)
        services.bestellposition(b, self.maus, D("1000"), ek=D("1.5"))
        Bestellung.objects.filter(pk=b.pk).update(versandkosten=D("10.25"))
        r = self.client.get(reverse("einkauf:bestellung", args=[b.pk]))
        self.assertEqual(r.context["gesamt_netto"], D("1510.25"))
        self.assertContains(r, 'data-ergibt="versand + verpackung + versicherung"')
        self.assertContains(r, 'data-wert="ware" data-ergibt="summe(runde(menge * ek))"')
        self.assertContains(r, 'data-wert="menge"')
        self.assertContains(r, "1.510,25 €")  # ohne JavaScript schon richtig (nicht auf ganze Euro abgeschnitten)
        self.assertContains(r, "window.wwokRechnen")

    def test_migration_verschiebt_vermerke_in_die_notiz(self):
        import importlib
        from django.apps import apps as django_apps
        mig = importlib.import_module("apps.einkauf.migrations.0027_herkunft_in_notiz")
        b1 = services.bestellung_anlegen(self.m, self.lf2)
        b2 = services.bestellung_anlegen(self.m, self.lf2)
        Bestellung.objects.filter(pk=b1.pk).update(bemerkung="Aus Bestellung AB2026-00002 – nur fehlende Mengen\nBitte mit Lieferschein",
                                                   notiz="alt")
        Bestellung.objects.filter(pk=b2.pk).update(bemerkung="Bitte schnell liefern")
        mig.verschieben(django_apps, None)
        b1.refresh_from_db(), b2.refresh_from_db()
        self.assertEqual((b1.bemerkung, b1.notiz), ("Bitte mit Lieferschein", "Aus Bestellung AB2026-00002 – nur fehlende Mengen\nalt"))
        self.assertEqual((b2.bemerkung, b2.notiz), ("Bitte schnell liefern", ""))

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
