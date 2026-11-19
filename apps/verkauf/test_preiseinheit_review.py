"""Befunde aus dem Review zur Preiseinheit (19.11.2026): EK + Aufschlag je Preiseinheit, Shop-Preis je Stück, Abo-Position,
Preis-Radar, Schutz vor langsamen %-Mustern."""
import time
from decimal import Decimal as D

from django.test import TestCase

from apps.kern.models import Mandant
from apps.stammdaten.models import Artikel, ArtikelLieferant, Kunde, Lieferant, Steuersatz
from apps.stammdaten.partnersuche import partner_filter
from apps.stammdaten.preise import stufenpreis
from apps.stammdaten.suche import like_regex

from .models import Abo, AboPosition


class PreiseinheitReviewTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.st = Steuersatz.objects.get(nummer=1)
        # Schrauben: EK 0,10 € je Stück, 30 % Aufschlag, Preis je 100 Stück, kein fester VK
        self.schraube = Artikel.objects.create(nummer="S1", bezeichnung="Schraube", steuersatz=self.st, preiseinheit=100,
                                               ek_manuell=D("0.10"), aufschlag_vk1=D("30"))

    def test_vk_aus_ek_und_aufschlag_gilt_je_preiseinheit(self):
        self.assertEqual(stufenpreis(self.schraube, 1), D("13.00"))  # 100 × 0,10 × 1,3
        self.schraube.vk1 = D("12.50")  # ein fester VK wird je Preiseinheit eingetragen und bleibt so
        self.assertEqual(stufenpreis(self.schraube, 1), D("12.50"))
        einzeln = Artikel.objects.create(nummer="S2", bezeichnung="Mutter", steuersatz=self.st, ek_manuell=D("0.10"), aufschlag_vk1=D("30"))
        self.assertEqual(stufenpreis(einzeln, 1), D("0.13"))  # Preiseinheit 1 unverändert

    def test_beleg_betrag_mit_aufschlags_vk(self):
        from .models import Beleg, Belegart, Position
        b = Beleg.objects.create(mandant=self.m, art=Belegart.ANGEBOT, kunde=Kunde.objects.create(nummer="1", name1="K"))
        p = Position.objects.create(beleg=b, artikel=self.schraube, bezeichnung="Schraube", menge=D("100"),
                                    einzelpreis=stufenpreis(self.schraube, 1), preiseinheit=self.schraube.preiseinheit)
        self.assertEqual(p.netto_gesamt, D("13.00"))  # 100 Stück = 13 €, nicht 0,13 €

    def test_shop_preis_und_uvp_je_stueck(self):
        from apps.shop.models import Shop
        from apps.shop.services import produktpreis, verkaufspreis
        shop = Shop.objects.create(mandant=self.m, name="Shop", url="https://shop.test", preisstufe=1)
        self.schraube.vk1, self.schraube.uvp = D("13.00"), D("20.00")
        netto, brutto = verkaufspreis(shop, self.schraube)
        self.assertEqual((netto, brutto), (D("0.13"), D("0.15")))
        preis = produktpreis(self.schraube, netto, brutto)[0]
        self.assertEqual(preis["listPrice"]["net"], 0.2)

    def test_abo_position_uebernimmt_preiseinheit_und_nie_null(self):
        from django.utils import timezone
        abo = Abo.objects.create(mandant=self.m, kunde=Kunde.objects.create(nummer="2", name1="K2"), bezeichnung="Wartung",
                                 naechster_termin=timezone.localdate())
        p = AboPosition.objects.create(abo=abo, artikel=self.schraube, bezeichnung="Schraube", menge=D("200"), einzelpreis=D("13"))
        self.assertEqual(p.preiseinheit, 100)
        frei = AboPosition.objects.create(abo=abo, bezeichnung="Service", menge=D("1"), einzelpreis=D("5"), preiseinheit=0)
        self.assertEqual(frei.preiseinheit, 1)
        self.assertEqual(abo.netto, D("31"))  # 200 × 13 ÷ 100 + 1 × 5

    def test_radar_vorschlag_je_preiseinheit(self):
        from apps.stammdaten.radar import eintrag
        lf = Lieferant.objects.create(nummer="L1", name1="Lieferant")
        ArtikelLieferant.objects.create(artikel=self.schraube, lieferant=lf, bestellnummer="X", ek=D("0.10"))
        self.schraube.vk1 = D("13.00")
        self.schraube.save()
        e = eintrag(self.schraube, self.m)
        if e is not None and e.vorschlag is not None:
            self.assertGreater(e.vorschlag, D("1"))  # Vorschlag je 100 Stück, nicht je Stück

    def test_prozent_muster_bleibt_schnell(self):
        start = time.monotonic()
        list(Kunde.objects.filter(partner_filter("a%a%a%a%a%a%a%a%a%a%a%a%a%a%x", ["name1", "ort"])))
        self.assertLess(time.monotonic() - start, 2)
        self.assertLessEqual(like_regex("a%b%c%d%e%f%g").count(".*"), 4)
        self.assertEqual(like_regex("Ma%%%er"), "^Ma.*er$")
