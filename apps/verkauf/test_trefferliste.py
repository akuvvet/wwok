"""Artikelsuche in der Belegmaske: Trefferliste als Tabelle Nummer | Bezeichnung | Lager | VK der Preisstufe des Kunden."""
from decimal import Decimal as D

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.kern.models import Mandant
from apps.lager.models import Bewegungsart, Lagerbewegung
from apps.stammdaten.models import Artikel, Kunde, Lieferant, Preislistenartikel, Steuersatz

from .models import Beleg, Belegart


class TrefferlisteTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        st = self.st = Steuersatz.objects.create(bezeichnung="19", prozent=D("19"))
        self.kunde = Kunde.objects.create(nummer="1000", name1="Muster GmbH", ort="Solingen", preisstufe=2)
        self.lagernd = Artikel.objects.create(nummer="M1", bezeichnung="Maus lagernd", steuersatz=st, vk1=D("10"), vk2=D("8"))
        self.leer = Artikel.objects.create(nummer="M2", bezeichnung="Maus ohne Bestand", steuersatz=st, vk1=D("10"), vk2=D("8"))
        Lagerbewegung.objects.create(mandant=self.m, artikel=self.lagernd, art=Bewegungsart.WARENEINGANG, menge=D("4"))
        lf = Lieferant.objects.create(nummer="L1", name1="Kosatec GmbH", kurzname="KOSATEC")
        Preislistenartikel.objects.create(lieferant=lf, bestellnummer="K-1", bezeichnung="Maus vom Lieferanten", ek=D("5"), bestand=50)
        self.client.force_login(get_user_model().objects.create_superuser("chef", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    def test_spaltenkopf_nennt_die_preisstufe_des_kunden(self):
        beleg = Beleg.objects.create(mandant=self.m, art=Belegart.ANGEBOT, kunde=self.kunde)
        r = self.client.get(reverse("verkauf:beleg", args=[beleg.pk]))
        self.assertContains(r, 'id="trefferliste" data-keine-ansicht role="listbox" aria-label="Artikel-Treffer" data-vk="VK2"')
        self.assertNotContains(r, "· beim Lieferanten")  # alter Fließtext der Trefferzeile
        self.assertContains(r, "['Lager', 'zahl']")

    def test_tabelle_mit_stern_zeigt_alle_eigenen_artikel_mit_ek(self):
        st = self.st
        Artikel.objects.bulk_create(
            Artikel(nummer=f"X{i:04d}", bezeichnung=f"Kabel {i}", steuersatz=st) for i in range(320))
        d = self.client.get(reverse("verkauf:api_artikel"), {"q": "*", "kunde": self.kunde.pk, "typ": "A", "tabelle": "1"}).json()
        eigene = [t for t in d["treffer"] if not t.get("lieferant")]
        self.assertEqual(len(eigene), 322)  # früher bei 300 abgeschnitten
        self.assertFalse(d["mehr"])
        self.assertTrue(all("ek" in t for t in d["treffer"]))  # letzte Spalte der Tabelle
        beleg = Beleg.objects.create(mandant=self.m, art=Belegart.ANGEBOT, kunde=self.kunde)
        self.assertContains(self.client.get(reverse("verkauf:beleg", args=[beleg.pk])), "{titel: 'EK netto', zahl: true")

    def test_api_liefert_die_werte_fuer_lager_und_preis(self):
        treffer = {t["nummer"]: t for t in self.client.get(
            reverse("verkauf:api_artikel"), {"q": "Maus", "kunde": self.kunde.pk, "typ": "A"}).json()["treffer"]}
        self.assertEqual((treffer["M1"]["bestand"], treffer["M1"]["preis"]), ("4", "8.00"))  # Lager > 0: Zeile wird hervorgehoben
        self.assertEqual(treffer["M2"]["bestand"], "0")
        fremd = next(t for t in treffer.values() if t.get("lieferant"))
        self.assertEqual((fremd["lieferant"], fremd["lieferant_bestand"], fremd["typ"]), ("KOSATEC", 50, "P"))
