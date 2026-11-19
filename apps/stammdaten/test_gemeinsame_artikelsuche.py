"""Gemeinsame Artikelsuche (Wunsch des Nutzers 19.11.2026): Belegmaske, Lieferantenbestellung und Artikelliste nutzen dieselbe Syntax
(apps/stammdaten/suche.py); das Auswahlfenster zeigt einen Detailbereich mit Bild und Angaben."""
from decimal import Decimal as D

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.einkauf import services as einkauf
from apps.kern.models import Mandant
from apps.lager.models import Bewegungsart
from apps.lager.services import buche_manuell

from .models import Artikel, Hersteller, Kategorie, Lieferant, Preislistenartikel, Steuersatz


@override_settings(SECURE_SSL_REDIRECT=False)
class GemeinsameArtikelsucheTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        st = Steuersatz.objects.get(nummer=1)
        self.kat = Kategorie.objects.create(name="Netzteile", online=True)
        self.netz = Artikel.objects.create(nummer="20274", bezeichnung="Netzteil ATX 750W", steuersatz=st, vk1=D("39.90"),
                                           herstellernummer="PSU-750", ean="4250686414745", kategorie=self.kat, online=True,
                                           hersteller=Hersteller.objects.create(name="Xilence"))
        self.kabel = Artikel.objects.create(nummer="20988", bezeichnung="Patchkabel CAT5e", steuersatz=st, vk1=D("16.79"))
        buche_manuell(self.m, self.netz, Bewegungsart.WARENEINGANG, D("20"))
        buche_manuell(self.m, self.kabel, Bewegungsart.WARENEINGANG, D("2"))
        self.lf = Lieferant.objects.create(nummer="L1", name1="API GmbH", kurzname="API")
        self.pl = Preislistenartikel.objects.create(lieferant=self.lf, bestellnummer="500", bezeichnung="Netzteil 650W Bronze",
                                                    ek=D("40"), bestand=8, hersteller="be quiet", herstellernummer="BQ-650", ean="4260052187",
                                                    kategorie="PC-Netzteile")
        self.client.force_login(get_user_model().objects.create_superuser("chef", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()
        self.b = einkauf.bestellung_anlegen(self.m, self.lf)

    def einkauf(self, q, **extra):
        return self.client.get(reverse("einkauf:bestellartikel_suche", args=[self.b.pk]), {"q": q, **extra}).json()

    def nummern(self, d):
        return [t["nummer"] for t in d["treffer"]]

    def test_einkauf_suche_mit_syntax(self):
        self.assertEqual(self.nummern(self.einkauf("netz%")), ["20274", "BQ-650"])  # beginnt mit, eigener Artikel und Preisliste
        self.assertEqual(self.nummern(self.einkauf(">5")), ["20274", "BQ-650"])  # eigener Bestand 20, beim Lieferanten 8; Kabel nur 2
        self.assertEqual(self.nummern(self.einkauf(">10")), ["20274"])
        self.assertEqual(len(self.einkauf("*")["treffer"]), 3)
        d = self.einkauf("*", tabelle="1")
        eigen = next(t for t in d["treffer"] if t["nummer"] == "20274")
        self.assertEqual((eigen["bestand"], eigen["verfuegbar"]), ("20", "20"))
        pl = next(t for t in d["treffer"] if t["pl"])
        self.assertEqual((pl["lieferant_bestand"], pl["bestand"]), (8, None))
        self.assertFalse(d["mehr"])

    def test_verkauf_suche_bestand_bei_preisliste_ist_lieferantenbestand(self):
        d = self.client.get(reverse("verkauf:api_artikel"), {"q": ">5"}).json()
        self.assertEqual([t["nummer"] for t in d["treffer"]], ["20274", "BQ-650"])

    def test_artikelliste_mit_syntax(self):
        r = self.client.get(reverse("lager:uebersicht"), {"q": ">5"})
        self.assertEqual([z["artikel"].nummer for z in r.context["zeilen"]], ["20274"])
        r = self.client.get(reverse("lager:uebersicht"), {"q": "%kabel%"})
        self.assertEqual([z["artikel"].nummer for z in r.context["zeilen"]], ["20988"])

    def test_detailbereich(self):
        d = self.client.get(reverse("lager:artikel_details"), {"nummer": "20274"}).json()
        felder = dict(d["felder"])
        self.assertEqual((felder["Herstellernummer"], felder["Hersteller"], felder["EAN"], felder["Kategorie"], felder["Online"]),
                         ("PSU-750", "Xilence", "4250686414745", "Netzteile", "online"))
        self.assertEqual(d["bild"], "")
        d = self.client.get(reverse("lager:artikel_details"), {"pl": self.pl.pk}).json()
        self.assertEqual(dict(d["felder"])["Hersteller"], "be quiet")
        self.assertEqual(self.client.get(reverse("lager:artikel_details"), {"nummer": "gibtsnicht"}).status_code, 404)

    def test_masken_nutzen_das_auswahlfenster_mit_details(self):
        r = self.client.get(reverse("einkauf:bestellung", args=[self.b.pk]))
        self.assertContains(r, "details: wwokArtikelDetails")
        self.assertContains(r, "window.wwokArtikelDetails")
