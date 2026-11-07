from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.kern.models import Mandant
from apps.stammdaten.models import Artikel, Kunde, Lieferant, Preislistenartikel, Stammnummer, Steuersatz
from apps.verkauf.models import Beleg, Belegart, Position

from . import services


@override_settings(SECURE_SSL_REDIRECT=False)
class PAnlageNurNachRueckfrageTest(TestCase):
    """Aus einem Preislistenartikel (P) wird nie still ein eigener Artikel (A); Nummer kommt aus dem Nummernkreis."""

    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.lf = Lieferant.objects.create(nummer="70272", name1="API GmbH", kurzname="API")
        self.st = Steuersatz.objects.get(nummer=1)
        self.pl = Preislistenartikel.objects.create(
            lieferant=self.lf, bestellnummer="500", herstellernummer="PSAZ-650", bezeichnung="Netzteil 650W",
            hersteller="Azza", ek=Decimal("40.0000"), bestand=12)
        Stammnummer.setzen(Stammnummer.Art.ARTIKEL, 7000)
        self.client.force_login(get_user_model().objects.create_user("t", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    def bestellung_mit_p(self):
        b = services.bestellung_anlegen(self.m, self.lf)
        services.bestellposition(b, None, Decimal("4"), preisliste_artikel=self.pl)
        services.bestellen(b)
        return b

    def test_wareneingang_ohne_bestaetigung_legt_nichts_an(self):
        b = self.bestellung_mit_p()
        we = services.wareneingang_anlegen(self.m, self.lf, b)
        services.wareneingang_aus_bestellung(we)
        self.assertEqual((Artikel.objects.count(), we.positionen.count()), (0, 0))
        self.assertIsNone(b.positionen.get().artikel)  # bleibt in der Bestellung offen
        seite = self.client.get(reverse("einkauf:wareneingang", args=[we.pk]))
        self.assertContains(seite, "Als eigene Artikel (A) anlegen und übernehmen")
        self.assertContains(seite, "1 Artikel werden mit Nummern aus dem Nummernkreis angelegt")

    def test_mit_bestaetigung_nummernkreis_und_verkaufsposition_wird_umgestellt(self):
        kunde = Kunde.objects.create(nummer="1", name1="K")
        beleg = Beleg.objects.create(mandant=self.m, art=Belegart.ANGEBOT, kunde=kunde)
        Position.objects.create(beleg=beleg, preisliste_artikel=self.pl, typ="P", bezeichnung="x", einzelpreis=Decimal("50"))
        b = self.bestellung_mit_p()
        we = services.wareneingang_anlegen(self.m, self.lf, b)
        r = self.client.post(reverse("einkauf:wareneingang_p_anlegen", args=[we.pk]))
        self.assertEqual(r.status_code, 302)
        a = Artikel.objects.get()
        self.assertEqual((a.nummer, a.typ, a.herstellernummer), ("7000", "A", "PSAZ-650"))
        self.assertEqual(we.positionen.get().menge, Decimal("4"))
        pos = beleg.positionen.get()
        self.assertEqual((pos.typ, pos.artikel, pos.preisliste_artikel), ("A", a, None))

    def test_doch_geliefert_fuer_p_nur_mit_rueckfrage(self):
        b = self.bestellung_mit_p()
        we = services.wareneingang_anlegen(self.m, self.lf, b)
        bp = b.positionen.get()
        self.client.post(reverse("einkauf:wareneingang_aufnehmen", args=[we.pk, bp.pk]))  # ohne p_anlegen
        self.assertEqual(Artikel.objects.count(), 0)
        self.client.post(reverse("einkauf:wareneingang_aufnehmen", args=[we.pk, bp.pk]), {"p_anlegen": "1"})
        self.assertEqual(Artikel.objects.count(), 1)

    def test_scan_ohne_bestaetigung_fragt_nur(self):
        we = services.wareneingang_anlegen(self.m, self.lf)
        r = self.client.post(reverse("einkauf:wareneingang_scan", args=[we.pk]), {"code": "PSAZ-650", "menge": "3"})
        self.assertEqual(r.status_code, 302)
        self.assertIn("p_code=PSAZ-650", r["Location"])
        self.assertEqual((Artikel.objects.count(), we.positionen.count()), (0, 0))
        seite = self.client.get(r["Location"])
        self.assertContains(seite, "ohne eigenen Artikel. Als Artikel (A) anlegen und erfassen?")

    def test_scan_mit_bestaetigung_legt_an_und_erfasst(self):
        we = services.wareneingang_anlegen(self.m, self.lf)
        self.client.post(reverse("einkauf:wareneingang_scan", args=[we.pk]), {"code": "PSAZ-650", "menge": "3", "p_anlegen": "1"})
        a = Artikel.objects.get()
        self.assertEqual(a.nummer, "7000")
        self.assertEqual((we.positionen.get().artikel, we.positionen.get().menge), (a, Decimal("3")))

    def test_scan_unbekannter_code_ist_fehler(self):
        we = services.wareneingang_anlegen(self.m, self.lf)
        r = self.client.post(reverse("einkauf:wareneingang_scan", args=[we.pk]), {"code": "gibtsnicht"}, follow=True)
        self.assertContains(r, "keinem Artikel zugeordnet")
        self.assertEqual(Artikel.objects.count(), 0)

    def test_bestellung_code_eingabe_ergibt_p_position(self):
        b = services.bestellung_anlegen(self.m, self.lf)
        self.client.post(reverse("einkauf:bestellung_position", args=[b.pk]), {"artikel": "PSAZ-650", "menge": "2"})
        pos = b.positionen.get()
        self.assertEqual((pos.artikel, pos.preisliste_artikel, pos.menge), (None, self.pl, Decimal("2")))
        self.assertEqual(Artikel.objects.count(), 0)

    def test_vorhandener_artikel_mit_gleicher_herstellernummer_wird_verknuepft(self):
        from apps.stammdaten.models import Hersteller
        azza = Hersteller.objects.create(name="AZZA")
        vorhanden = Artikel.objects.create(nummer="X1", bezeichnung="Netzteil", herstellernummer="psaz-650", hersteller=azza,
                                           steuersatz=self.st)
        a = services.artikel_aus_preisliste(self.pl)
        self.assertEqual(a, vorhanden)
        self.assertEqual(Artikel.objects.count(), 1)
        self.pl.refresh_from_db()
        self.assertEqual(self.pl.artikel, vorhanden)

    def test_gleiche_herstellernummer_anderer_hersteller_ergibt_neuen_artikel(self):
        from apps.stammdaten.models import Hersteller
        fremd = Hersteller.objects.create(name="Fremd")
        vorhanden = Artikel.objects.create(nummer="X1", bezeichnung="Anderes", herstellernummer="PSAZ-650", hersteller=fremd,
                                           steuersatz=self.st)
        a = services.artikel_aus_preisliste(self.pl)
        self.assertNotEqual(a, vorhanden)
        self.assertEqual((a.nummer, Artikel.objects.count()), ("7000", 2))

    def test_gleiche_herstellernummer_gleiche_ean_wird_verknuepft(self):
        self.pl.ean = "4006381333931"
        self.pl.save()
        vorhanden = Artikel.objects.create(nummer="X1", bezeichnung="Netzteil", herstellernummer="PSAZ-650",
                                           ean="4006381333931", steuersatz=self.st)
        self.assertEqual(services.artikel_aus_preisliste(self.pl), vorhanden)

    def test_zweiter_aufruf_mit_altem_objekt_legt_keinen_zweiten_artikel_an(self):
        veraltet = Preislistenartikel.objects.get(pk=self.pl.pk)  # Doppelklick: Objekt noch ohne Artikel
        erster = services.artikel_aus_preisliste(self.pl)
        zweiter = services.artikel_aus_preisliste(veraltet)
        self.assertEqual(erster, zweiter)
        self.assertEqual(Artikel.objects.count(), 1)

    def test_p_anlegen_holt_entfernte_a_position_nicht_zurueck(self):
        a = Artikel.objects.create(nummer="A1", bezeichnung="Kabel", steuersatz=self.st)
        b = services.bestellung_anlegen(self.m, self.lf)
        services.bestellposition(b, a, Decimal("2"))
        services.bestellposition(b, None, Decimal("4"), preisliste_artikel=self.pl)
        services.bestellen(b)
        we = services.wareneingang_anlegen(self.m, self.lf, b)
        services.wareneingang_aus_bestellung(we)
        we.positionen.filter(artikel=a).delete()  # als „nicht geliefert“ entfernt
        self.client.post(reverse("einkauf:wareneingang_p_anlegen", args=[we.pk]))
        self.assertFalse(we.positionen.filter(artikel=a).exists())
        self.assertEqual(we.positionen.count(), 1)
        self.assertEqual(we.positionen.get().menge, Decimal("4"))
