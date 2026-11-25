"""DATEV-Export: Erlöskonto je Kostenstelle (Vorgabe des Steuerberaters 25.11.2026: OKAY 8400, Apetito 8401)."""
from decimal import Decimal as D

from .datev import beleg_zeilen
from .test_proforma import ProformaBasis

from apps.stammdaten.models import Artikel
from apps.verkauf.models import Beleg, Belegart, Position


class DatevKostenstelleTest(ProformaBasis):
    def rechnung(self, rabatt="0", versand="0"):
        apetito, _ = Artikel.objects.get_or_create(nummer="L-AP", defaults={
            "bezeichnung": "Tour Apetito", "steuersatz": self.a.steuersatz, "vk1": D("100"), "kostenstelle": "Apetito", "typ": "L"})
        b = Beleg.objects.create(mandant=self.m, art=Belegart.RECHNUNG, kunde=self.kunde, rabatt_prozent=D(rabatt), versandkosten=D(versand))
        Position.objects.create(beleg=b, artikel=self.a, bezeichnung="Notebook", menge=1, einzelpreis=D("100"), steuer_prozent=D("19"))
        Position.objects.create(beleg=b, artikel=apetito, bezeichnung="Tour", menge=3, einzelpreis=D("33.33"), steuer_prozent=D("19"))
        Position.objects.create(beleg=b, bezeichnung="Buch", menge=1, einzelpreis=D("10"), steuer_prozent=D("7"))
        b.abschliessen()
        b.refresh_from_db()
        return b

    def test_ohne_zuordnung_wie_bisher(self):
        b = self.rechnung()
        self.assertEqual({k for k, _ in beleg_zeilen(b)}, {"8400", "8300"})

    def test_apetito_auf_8401_und_summe_bleibt_gleich(self):
        self.m.kostenstellen_konten = "apetito = 8401"
        self.m.save()
        for rabatt, versand in (("0", "0"), ("5", "4.90")):
            with self.subTest(rabatt=rabatt, versand=versand):
                b = self.rechnung(rabatt, versand)
                zeilen = dict(beleg_zeilen(b))
                self.assertEqual(set(zeilen), {"8400", "8401", "8300"})
                self.assertEqual(sum(zeilen.values()), b.summe_brutto)
        b = self.rechnung()
        zeilen = dict(beleg_zeilen(b))
        self.assertEqual(zeilen["8401"], D("118.99"))  # 99,99 netto × 1,19 anteilig
        self.assertEqual(zeilen["8400"], D("119.00"))
