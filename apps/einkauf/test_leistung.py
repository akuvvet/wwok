"""Leistungen (Typ L): keine Ware – kein Wareneingang, keine Bestellung, kein Lager, keine Maße."""
from decimal import Decimal as D

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.kern.models import Mandant
from apps.lager.models import Lagerbewegung
from apps.stammdaten.models import Artikel, Kunde, Lieferant, Steuersatz
from apps.verkauf.models import Beleg, Belegart, Position

from . import services


@override_settings(SECURE_SSL_REDIRECT=False)
class LeistungTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.zone = Mandant.objects.create(kurzname="ZONE", firmenname="ZONE Handel")
        self.lf = Lieferant.objects.create(nummer="L1", name1="API GmbH", kurzname="API")
        self.st = Steuersatz.objects.create(bezeichnung="19", prozent=D("19"))
        self.l = Artikel.objects.create(nummer="40098", typ="L", bezeichnung="Network Installation", steuersatz=self.st, vk1=D("35"),
                                        gewicht_kg=D("2"), laenge_cm=D("10"), sperrgut=True, online=True)
        self.user = get_user_model().objects.create_superuser("t", password="x" * 14)
        self.client.force_login(self.user)
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    def test_keine_masse_kein_gewicht(self):
        self.l.refresh_from_db()
        self.assertEqual((self.l.gewicht_kg, self.l.laenge_cm, self.l.sperrgut, self.l.online), (None, None, False, False))

    def test_kein_wareneingang_und_keine_bestellung(self):
        we = services.wareneingang_anlegen(self.m, self.lf)
        with self.assertRaisesMessage(ValueError, "ist eine Leistung"):
            services.wareneingang_erfassen(we, self.l)
        r = self.client.post(reverse("einkauf:wareneingang_scan", args=[we.pk]), {"code": "40098", "menge": "1"}, follow=True)
        self.assertContains(r, "ist eine Leistung")
        self.assertFalse(we.positionen.exists())
        b = services.bestellung_anlegen(self.m, self.lf)
        with self.assertRaisesMessage(ValueError, "ist eine Leistung"):
            services.bestellposition(b, self.l, D("1"))
        self.assertEqual(self.client.get(reverse("einkauf:bestellartikel_suche", args=[b.pk]), {"q": "network"}).json()["treffer"], [])

    def test_altbestand_in_wareneingang_wird_nicht_gebucht(self):
        we = services.wareneingang_anlegen(self.m, self.lf)
        we.positionen.create(artikel=self.l, menge=D("1"), ek=D("1"))
        with self.assertRaisesMessage(ValueError, "ist eine Leistung"):
            services.wareneingang_buchen(we)
        self.assertFalse(Lagerbewegung.objects.exists())

    def test_kein_lager(self):
        for name, daten in (("lager:buchen", {"art": "WE", "menge": "5"}), ("lager:inventur", {"gezaehlt": "5"}),
                            ("lager:umbuchung", {"nach": self.zone.pk, "menge": "1"}), ("lager:stamm", {"lagerplatz": "A1"})):
            r = self.client.post(reverse(name, args=[self.l.pk]), daten, follow=True)
            self.assertContains(r, "ist eine Leistung", msg_prefix=name)
        self.assertFalse(Lagerbewegung.objects.exists())
        self.assertFalse(self.l.mandantendaten.exists())

    def test_artikelseite_zeigt_preise_statt_lager(self):
        r = self.client.get(reverse("lager:artikel", args=[self.l.pk]))
        self.assertContains(r, "ohne Lager, Bestellung und Wareneingang")
        self.assertContains(r, "35,00 €")
        self.assertNotContains(r, "<h2>Buchen</h2>")
        self.assertNotContains(r, "Bestandshistorie")

    def test_leistung_ist_in_belegen_berechenbar(self):
        kunde = Kunde.objects.create(nummer="1", name1="Muster GmbH")
        b = Beleg.objects.create(mandant=self.m, art=Belegart.RECHNUNG, kunde=kunde)
        Position.objects.create(beleg=b, typ="L", artikel=self.l, bezeichnung=self.l.bezeichnung, einzelpreis=D("35"))
        b.abschliessen()
        b.refresh_from_db()
        self.assertEqual(b.summe_netto, D("35.00"))
        self.assertFalse(Lagerbewegung.objects.exists())
