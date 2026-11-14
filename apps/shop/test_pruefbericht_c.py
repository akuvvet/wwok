"""Prüfbericht C, Shop: Gruppenartikel aus einer Shop-Bestellung werden in ihre Komponenten aufgelöst und abgebucht."""
from decimal import Decimal as D

from django.test import TestCase

from apps.kern.models import Mandant
from apps.lager.models import Bewegungsart, bestand
from apps.lager.services import buche_manuell
from apps.stammdaten.models import Artikel, GruppenKomponente, Steuersatz

from . import services
from .models import Shop
from .tests import bestellung


class ShopGruppenartikelTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        st = Steuersatz.objects.create(bezeichnung="19", prozent=D("19"))
        self.shop = Shop.objects.create(mandant=self.m, name="Webshop", url="https://shop.example.test", client_id="abc")
        self.cpu = Artikel.objects.create(nummer="CPU", bezeichnung="Prozessor", steuersatz=st, vk1=D("100"))
        self.ram = Artikel.objects.create(nummer="RAM", bezeichnung="Speicher", steuersatz=st, vk1=D("40"))
        self.pc = Artikel.objects.create(nummer="PC1", typ="G", bezeichnung="Bundle", steuersatz=st, vk1=D("180"), online=True)
        GruppenKomponente.objects.create(gruppe=self.pc, komponente=self.cpu, menge=D("1"))
        GruppenKomponente.objects.create(gruppe=self.pc, komponente=self.ram, menge=D("2"))
        buche_manuell(self.m, self.cpu, Bewegungsart.WARENEINGANG, D("5"))
        buche_manuell(self.m, self.ram, Bewegungsart.WARENEINGANG, D("10"))

    def importieren(self, produkt="PC1", menge=2):
        b = bestellung(produkt=produkt, menge=menge)
        b["lineItems"] = [z for z in b["lineItems"] if z["type"] == "product"]
        b["lineItems"][0]["unitPrice"] = 238.0
        b["amountTotal"] = 238.0 * menge + 4.80
        return services.bestellung_importieren(self.shop, b).beleg

    def test_komponenten_werden_unterpositionen_und_preis_bleibt_am_kopf(self):
        beleg = self.importieren()
        kopf = beleg.positionen.get(gruppe__isnull=True)
        self.assertEqual((kopf.typ, kopf.artikel, kopf.menge, kopf.preis_fest), ("G", self.pc, D("2"), True))
        unter = {p.artikel.nummer: p.menge for p in beleg.positionen.filter(gruppe=kopf)}
        self.assertEqual(unter, {"CPU": D("2"), "RAM": D("4")})
        self.assertEqual(kopf.einzelpreis, D("200.00"))  # 238 brutto / 1,19, nicht durch Komponenten verändert
        beleg.refresh_from_db()
        self.assertEqual(beleg.summe_netto, D("400.00") + beleg.versandkosten)  # Unterpositionen zählen nicht extra

    def test_komponenten_buchen_beim_abschluss_vom_lager_ab(self):
        beleg = self.importieren()
        from apps.verkauf.models import Belegart
        beleg.art = Belegart.LIEFERSCHEIN
        beleg.save()
        beleg.abschliessen()
        self.assertEqual((bestand(self.m, self.cpu), bestand(self.m, self.ram)), (D("3"), D("6")))

    def test_normaler_artikel_bleibt_eine_position(self):
        beleg = self.importieren(produkt="CPU")
        self.assertEqual(beleg.positionen.count(), 1)
        self.assertFalse(beleg.positionen.get().preis_fest)
