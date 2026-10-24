import io
from decimal import Decimal

from django.core.management import call_command
from django.test import TestCase

from apps.kern.models import Mandant
from apps.stammdaten.models import Artikel, ArtikelLieferant, Hersteller, Kunde, Lieferant, Preislistenartikel, Steuersatz
from apps.verkauf.models import Beleg, Belegart, Position

from . import assistent, preislisten, services
from .models import Preislistenprofil

API = (
    "sku;msku;ean;title;manufacturer;lwg1;price;stock\n"
    "100008;UA0126;;Logilink USB-HUB 13-Port;Logilink;PC;12,50;5\n"
    "100012;65051;;Delock DVI Adapter;Delock;PC;7,90;0\n"
    "100099;65051;;Delock DVI Adapter (zweite SKU);Delock;PC;8,10;1\n"
    "100100;;;Artikel ohne MSKU;Fabrikat;PC;1,00;1\n").encode()


class PreislistenartikelTest(TestCase):
    def setUp(self):
        self.lf = Lieferant.objects.create(nummer="70272", name1="API GmbH", kurzname="API")
        self.p = Preislistenprofil.objects.create(
            lieferant=self.lf, name="Api", sp_bestellnummer="sku", sp_herstellernummer="msku", sp_bezeichnung="title",
            sp_hersteller="manufacturer", sp_ek="price", sp_bestand="stock")

    def test_import_nur_in_preislistentabelle_mit_hersteller_als_text(self):
        prot = preislisten.importieren(self.p, API)
        self.assertEqual((prot.neu, prot.fehler), (4, 0))
        self.assertEqual((Artikel.objects.count(), Hersteller.objects.count()), (0, 0))  # nichts in den eigenen Stammdaten
        e = Preislistenartikel.objects.get(bestellnummer="100008")
        self.assertEqual((e.nummer, e.bezeichnung, e.hersteller, e.ek, e.lieferant.kurz), ("UA0126", "Logilink USB-HUB 13-Port", "Logilink", Decimal("12.5000"), "API"))
        self.assertEqual(Preislistenartikel.objects.filter(herstellernummer="65051").count(), 2)  # zwei SKUs, gleiche MSKU
        self.assertEqual(Preislistenartikel.objects.get(bestellnummer="100100").nummer, "100100")  # ohne MSKU: Bestellnummer

    def test_spalten_vorschlag_fuer_englische_ueberschriften(self):
        namen = ["sku", "msku", "ean", "title", "manufacturer", "lwg1", "price", "stock"]
        v = assistent.spalten_raten(namen)
        self.assertEqual((v["bestellnummer"], v["herstellernummer"], v["bezeichnung"], v["hersteller"], v["ek"], v["bestand"]),
                         ("sku", "msku", "title", "manufacturer", "price", "stock"))
        self.assertEqual(assistent.spalten_raten(["msku", "title", "price"])["bestellnummer"], "msku")  # ohne eigene SKU-Spalte

    def test_aus_preislistenartikel_wird_beim_wareneingang_ein_eigener_artikel(self):
        Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        Steuersatz.objects.get(nummer=1)
        preislisten.importieren(self.p, API)
        erster = Preislistenartikel.objects.get(bestellnummer="100012")
        a = services.artikel_aus_preisliste(erster)
        self.assertEqual((a.nummer, a.typ, a.bezeichnung, a.herstellernummer, a.hersteller.name), ("65051", "A", "Delock DVI Adapter", "65051", "Delock"))
        erster.refresh_from_db()
        self.assertEqual(erster.artikel, a)
        self.assertTrue(ArtikelLieferant.objects.filter(artikel=a, bestellnummer="100012", ek=Decimal("7.90")).exists())
        zweiter = Preislistenartikel.objects.get(bestellnummer="100099")  # gleiche MSKU → derselbe eigene Artikel
        self.assertEqual(services.artikel_aus_preisliste(zweiter), a)
        self.assertEqual(Artikel.objects.count(), 1)
        self.assertEqual(Hersteller.objects.filter(name="Delock").count(), 1)
        self.assertEqual(services.artikel_aus_preisliste(erster), a)  # nicht doppelt anlegen
        ohne = services.artikel_aus_preisliste(Preislistenartikel.objects.get(bestellnummer="100100"))
        self.assertEqual(ohne.nummer, "P70272-100100")  # ohne MSKU: Lieferantennummer-Bestellnummer

    def test_zuruecksetzen_alter_p_artikel(self):
        st = Steuersatz.objects.get(nummer=1)
        alt = Artikel.objects.create(nummer="P70272-1", typ="P", bezeichnung="alt", steuersatz=st)
        benutzt = Artikel.objects.create(nummer="P70272-2", typ="P", bezeichnung="benutzt", steuersatz=st)
        ArtikelLieferant.objects.create(artikel=alt, lieferant=self.lf, bestellnummer="1", ek=Decimal("1"))
        m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        kunde = Kunde.objects.create(nummer="1", name1="K")
        beleg = Beleg.objects.create(mandant=m, art=Belegart.ANGEBOT, kunde=kunde)
        Position.objects.create(beleg=beleg, artikel=benutzt, bezeichnung="x", einzelpreis=Decimal("1"))
        ausgabe = io.StringIO()
        call_command("p_artikel_zuruecksetzen", stdout=ausgabe)
        self.assertIn("davon 1 unbenutzt", ausgabe.getvalue())
        call_command("p_artikel_zuruecksetzen", "--loeschen", stdout=ausgabe)
        self.assertEqual(list(Artikel.objects.values_list("nummer", flat=True)), ["P70272-2"])
        self.assertEqual(ArtikelLieferant.objects.count(), 0)
