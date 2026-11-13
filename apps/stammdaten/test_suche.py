"""Suchsyntax der Artikelsuche: * = alle, >Zahl = Lagerbestand größer, % wie SQL-LIKE (Belegmaske und Tabellensuche)."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.kern.models import Mandant
from apps.lager.models import Bewegungsart
from apps.lager.services import buche_manuell

from .models import Artikel, Kunde, Lieferant, Preislistenartikel, Steuersatz
from .suche import artikel_filtern, like_regex, zerlegen


class ZerlegenTest(TestCase):
    def test_syntax(self):
        self.assertEqual(zerlegen("* >5"), ([], [], Decimal("5")))
        self.assertEqual(zerlegen("netz%  > 2,5 gold"), (["gold"], ["netz%"], Decimal("2.5")))
        self.assertEqual(like_regex("%750W%"), "^.*750W.*$")
        self.assertEqual(like_regex("a.b%"), r"^a\.b.*$")  # Punkt ist kein Platzhalter


@override_settings(SECURE_SSL_REDIRECT=False)
class SucheTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.m2 = Mandant.objects.create(kurzname="ZONE", firmenname="ZONE Handel")
        st = Steuersatz.objects.create(bezeichnung="19z", prozent=Decimal("19"))
        self.a = Artikel.objects.create(nummer="N1", bezeichnung="Netzteil 750W Gold", steuersatz=st, vk1=Decimal("80"))
        self.b = Artikel.objects.create(nummer="N2", bezeichnung="Gehäuse ohne Netzteil", steuersatz=st, vk1=Decimal("40"))
        self.c = Artikel.objects.create(nummer="N3", bezeichnung="Netzteil 550W", steuersatz=st, vk1=Decimal("50"))
        buche_manuell(self.m, self.a, Bewegungsart.WARENEINGANG, Decimal("10"))
        buche_manuell(self.m, self.b, Bewegungsart.WARENEINGANG, Decimal("3"))
        buche_manuell(self.m2, self.c, Bewegungsart.WARENEINGANG, Decimal("99"))  # anderer Mandant zählt nicht
        lf = Lieferant.objects.create(nummer="L1", name1="Lieferant", kurzname="LF")
        Preislistenartikel.objects.create(lieferant=lf, bestellnummer="X1", bezeichnung="Netzteil 650W", ek=Decimal("30"))
        self.kunde = Kunde.objects.create(nummer="K1", name1="Kunde")
        self.client.force_login(get_user_model().objects.create_superuser("t", "t@example.test", "x" * 14))
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    def nummern(self, q):
        return sorted(artikel_filtern(Artikel.objects.all(), q, self.m).values_list("nummer", flat=True))

    def test_bestand_groesser(self):
        self.assertEqual(self.nummern(">5"), ["N1"])
        self.assertEqual(self.nummern(">2"), ["N1", "N2"])
        self.assertEqual(self.nummern("netzteil >0"), ["N1", "N2"])

    def test_like(self):
        self.assertEqual(self.nummern("Netz%"), ["N1", "N3"])  # beginnt mit
        self.assertEqual(self.nummern("%750W%"), ["N1"])  # enthält
        self.assertEqual(self.nummern("%netzteil"), ["N2"])  # endet mit, Groß/klein egal
        self.assertEqual(self.nummern("netzteil"), ["N1", "N2", "N3"])  # ohne % wie bisher: enthält

    def api(self, q, **extra):
        r = self.client.get(reverse("verkauf:api_artikel"), {"q": q, "kunde": self.kunde.pk, "typ": "A", **extra})
        return [t["nummer"] for t in r.json()["treffer"]]

    def test_belegsuche(self):
        self.assertEqual(self.api(">5"), ["N1"])  # Preislistenartikel haben keinen eigenen Bestand
        self.assertEqual(self.api("Netz%"), ["N1", "N3", "X1"])
        self.assertEqual(len(self.api("*", tabelle="1")), 4)
        treffer = self.client.get(reverse("verkauf:api_artikel"), {"q": "N1", "kunde": self.kunde.pk, "typ": "A"}).json()["treffer"][0]
        self.assertEqual((treffer["bestand"], treffer["preis"]), ("10", "80.00"))

    def test_tabellensuche_nachfolger(self):
        r = self.client.get(reverse("stammdaten:artikel_tabelle"), {"q": "* >2"}).json()
        self.assertEqual([z["nummer"] for z in r["treffer"]], ["N1", "N2"])
        r = self.client.get(reverse("stammdaten:artikel_tabelle"), {"q": "%550W"}).json()
        self.assertEqual([z["nummer"] for z in r["treffer"]], ["N3"])
