import io
from decimal import Decimal

from django.core.management import call_command
from django.test import TestCase

from apps.kern.models import Mandant
from apps.stammdaten.models import Artikel, ArtikelLieferant, Hersteller, Kunde, Lieferant, Steuersatz
from apps.verkauf.models import Beleg, Belegart, Position

from . import assistent, preislisten
from .models import Preislistenprofil

API = (
    "sku;msku;ean;title;manufacturer;lwg1;price;stock\n"
    "100008;UA0126;;Logilink USB-HUB 13-Port;Logilink;PC;12,50;5\n"
    "100012;65051;;Delock DVI Adapter;Delock;PC;7,90;0\n"
    "100099;65051;;Delock DVI Adapter (zweite SKU);Delock;PC;8,10;1\n"
    "100100;;;Artikel ohne MSKU;Fabrikat;PC;1,00;1\n").encode()


class PArtikelTest(TestCase):
    def setUp(self):
        self.lf = Lieferant.objects.create(nummer="70272", name1="API GmbH")
        Hersteller.objects.create(kennzeichen="LOGI", name="Logilink")
        self.p = Preislistenprofil.objects.create(
            lieferant=self.lf, name="Api", sp_bestellnummer="sku", sp_herstellernummer="msku", sp_bezeichnung="title",
            sp_hersteller="manufacturer", sp_ek="price", sp_bestand="stock")

    def test_artikelnummer_ist_die_msku_und_hersteller_wird_angelegt(self):
        prot = preislisten.importieren(self.p, API)
        self.assertEqual((prot.neu, prot.fehler), (4, 0))
        a = Artikel.objects.get(nummer="UA0126")
        self.assertEqual((a.typ, a.bezeichnung, a.herstellernummer, a.hersteller.name), ("P", "Logilink USB-HUB 13-Port", "UA0126", "Logilink"))
        self.assertEqual(ArtikelLieferant.objects.get(bestellnummer="100008").ek, Decimal("12.5000"))
        delock = Artikel.objects.get(nummer="65051")
        self.assertEqual(delock.hersteller.name, "Delock")  # Hersteller automatisch angelegt
        self.assertEqual(Hersteller.objects.filter(name="Delock").count(), 1)
        # zweite SKU mit gleicher MSKU gehört zum selben Artikel (Abgleich über die Herstellernummer)
        self.assertEqual(ArtikelLieferant.objects.get(bestellnummer="100099").artikel, delock)
        self.assertEqual(Artikel.objects.filter(typ="P").count(), 3)
        ohne = Artikel.objects.get(bezeichnung="Artikel ohne MSKU")
        self.assertEqual(ohne.nummer, "P70272-100100")  # ohne MSKU: Lieferantennummer-Bestellnummer

    def test_spalten_vorschlag_fuer_englische_ueberschriften(self):
        namen = ["sku", "msku", "ean", "title", "manufacturer", "lwg1", "price", "stock"]
        v = assistent.spalten_raten(namen)
        self.assertEqual((v["bestellnummer"], v["herstellernummer"], v["bezeichnung"], v["hersteller"], v["ek"], v["bestand"]),
                         ("sku", "msku", "title", "manufacturer", "price", "stock"))
        nur_msku = assistent.spalten_raten(["msku", "title", "price"])
        self.assertEqual(nur_msku["bestellnummer"], "msku")  # ohne eigene SKU-Spalte gilt die MSKU

    def test_zuruecksetzen_loescht_nur_unbenutzte_p_artikel(self):
        preislisten.importieren(self.p, API)
        benutzt = Artikel.objects.get(nummer="UA0126")
        m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        kunde = Kunde.objects.create(nummer="1", name1="K")
        beleg = Beleg.objects.create(mandant=m, art=Belegart.ANGEBOT, kunde=kunde)
        Position.objects.create(beleg=beleg, artikel=benutzt, bezeichnung="x", einzelpreis=Decimal("1"))
        ausgabe = io.StringIO()
        call_command("p_artikel_zuruecksetzen", stdout=ausgabe)
        self.assertIn("davon 2 unbenutzt", ausgabe.getvalue())
        self.assertEqual(Artikel.objects.filter(typ="P").count(), 3)  # Trockenlauf löscht nichts
        call_command("p_artikel_zuruecksetzen", "--loeschen", stdout=ausgabe)
        self.assertEqual(list(Artikel.objects.filter(typ="P").values_list("nummer", flat=True)), ["UA0126"])
        self.assertEqual(ArtikelLieferant.objects.count(), 1)  # Lieferantenpreise der gelöschten Artikel sind weg
        self.p.refresh_from_db()
        prot = preislisten.importieren(self.p, API)  # neu einlesen: gelöschte Artikel entstehen wieder
        self.assertEqual(prot.neu, 3)
        self.assertEqual(Artikel.objects.filter(typ="P").count(), 3)
