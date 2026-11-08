"""Wareneingang: Preislistenartikel (P) über das vorausgefüllte Artikelformular als A-Artikel anlegen und übernehmen."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.kern.models import Mandant
from apps.stammdaten.models import Artikel, ArtikelLieferant, Kunde, Lieferant, Preislistenartikel, Stammnummer, Steuersatz
from apps.verkauf.models import Beleg, Belegart, Position

from . import services

NEU = "stammdaten:artikel_neu"


@override_settings(SECURE_SSL_REDIRECT=False)
class WareneingangArtikelFormularTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.lf = Lieferant.objects.create(nummer="70272", name1="API GmbH", kurzname="API")
        self.st = Steuersatz.objects.get(nummer=1)
        self.pl = Preislistenartikel.objects.create(
            lieferant=self.lf, bestellnummer="500", herstellernummer="PSAZ-650", ean="4012345678901", bezeichnung="Netzteil 650W",
            hersteller="Azza", ek=Decimal("40.0000"), bestand=12, gewicht_kg=Decimal("1.800"))
        Stammnummer.setzen(Stammnummer.Art.ARTIKEL, 7000)
        self.client.force_login(get_user_model().objects.create_superuser("chef", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()
        self.b = services.bestellung_anlegen(self.m, self.lf)
        self.bp = services.bestellposition(self.b, None, Decimal("4"), preisliste_artikel=self.pl)
        services.bestellen(self.b)
        self.we = services.wareneingang_anlegen(self.m, self.lf, self.b)

    def url(self, **bezug):
        from urllib.parse import urlencode
        return reverse(NEU) + "?" + urlencode({"kopie_von_pl": self.pl.pk, "we": self.we.pk, **bezug})

    def daten(self, **extra):
        d = {"nummer": "7000", "typ": "A", "bezeichnung": "Netzteil 650 W (geändert)", "mengeneinheit": "Stück",
             "steuersatz": self.st.pk, "herstellernummer": "PSAZ-650", "ean": "4012345678901", "gewicht_kg": "1.800",
             "kopie_von_pl": self.pl.pk, "we": self.we.pk, "we_bp": self.bp.pk, "we_menge": "", "md-lagerplatz": "R1-04"}
        d.update(extra)
        return d

    def test_wareneingang_zeigt_formular_knopf_je_p_position(self):
        seite = self.client.get(reverse("einkauf:wareneingang", args=[self.we.pk]))
        self.assertContains(seite, "Im Formular anlegen …")
        self.assertContains(seite, f'data-modal="{self.url(we_bp=self.bp.pk)}"'.replace("&", "&amp;"))
        self.assertContains(seite, "Als eigene Artikel (A) anlegen und übernehmen")  # der schnelle Weg bleibt

    def test_formular_ist_mit_lieferantendaten_vorausgefuellt_und_legt_noch_nichts_an(self):
        r = self.client.get(self.url(we_bp=self.bp.pk))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'value="Netzteil 650W"')
        self.assertContains(r, 'value="PSAZ-650"')
        self.assertContains(r, 'value="4012345678901"')
        self.assertContains(r, f'name="we" value="{self.we.pk}"')
        self.assertContains(r, f'name="we_bp" value="{self.bp.pk}"')
        self.assertEqual(Artikel.objects.count(), 0)
        self.assertEqual(self.we.positionen.count(), 0)

    def test_speichern_legt_a_artikel_mit_aenderungen_an_und_uebernimmt_ihn(self):
        kunde = Kunde.objects.create(nummer="1", name1="K")
        angebot = Beleg.objects.create(mandant=self.m, art=Belegart.ANGEBOT, kunde=kunde)
        Position.objects.create(beleg=angebot, preisliste_artikel=self.pl, typ="P", bezeichnung="x", einzelpreis=Decimal("50"))
        r = self.client.post(reverse(NEU), self.daten())
        self.assertEqual(r.status_code, 200, r.content[:500])
        self.assertEqual(r.json(), {"ok": True, "ziel": reverse("einkauf:wareneingang", args=[self.we.pk])})
        a = Artikel.objects.get()
        self.assertEqual((a.nummer, a.typ, a.bezeichnung), ("7000", "A", "Netzteil 650 W (geändert)"))  # manuelle Änderung
        self.bp.refresh_from_db()
        self.pl.refresh_from_db()
        self.assertEqual((self.bp.artikel, self.pl.artikel), (a, a))
        pos = self.we.positionen.get()
        self.assertEqual((pos.artikel, pos.menge, pos.bestellposition), (a, Decimal("4"), self.bp))
        self.assertTrue(ArtikelLieferant.objects.filter(artikel=a, lieferant=self.lf, bestellnummer="500").exists())
        vp = angebot.positionen.get()  # wie beim schnellen Weg: offene Verkaufspositionen werden zu A
        self.assertEqual((vp.typ, vp.artikel, vp.preisliste_artikel), ("A", a, None))
        seite = self.client.get(reverse("einkauf:wareneingang", args=[self.we.pk]))
        self.assertNotContains(seite, "Im Formular anlegen …")  # keine offene P-Position mehr

    def test_abbrechen_laesst_alles_offen(self):
        self.client.get(self.url(we_bp=self.bp.pk))  # Formular nur geöffnet, nicht gespeichert
        self.assertEqual(Artikel.objects.count(), 0)
        self.bp.refresh_from_db()
        self.assertIsNone(self.bp.artikel)
        self.assertContains(self.client.get(reverse("einkauf:wareneingang", args=[self.we.pk])), "Im Formular anlegen …")

    def test_gebuchter_wareneingang_legt_nichts_an_und_meldet_es_im_formular(self):
        self.client.get(self.url(we_bp=self.bp.pk))
        type(self.we).objects.filter(pk=self.we.pk).update(gebucht=True)
        r = self.client.post(reverse(NEU), self.daten())
        self.assertEqual(r.status_code, 422)
        self.assertContains(r, "nicht mehr offen (z. B. inzwischen gebucht) – es wurde nichts angelegt", status_code=422)
        self.assertContains(r, f'name="we" value="{self.we.pk}"', status_code=422)  # Bezug bleibt: speichert nie ohne ihn
        self.assertEqual(Artikel.objects.count(), 0)
        self.assertEqual(self.client.get(self.url(we_bp=self.bp.pk)).status_code, 404)  # Öffnen geht dann nicht mehr

    def test_inzwischen_gebucht_beim_uebernehmen_meldet_nichts_angelegt(self):
        bezug = services.we_bezug_holen(self.m, self.pl, self.we.pk, self.bp.pk)
        type(self.we).objects.filter(pk=self.we.pk).update(gebucht=True)
        a = Artikel.objects.create(nummer="X1", typ="A", bezeichnung="x", steuersatz=self.st)
        with self.assertRaisesMessage(ValueError, "inzwischen gebucht – es wurde nichts angelegt"):
            services.we_artikel_uebernehmen(bezug, a)

    def test_typ_ist_fest_a_auch_wenn_l_oder_g_gesendet_wird(self):
        self.assertContains(self.client.get(self.url(we_bp=self.bp.pk)), "disabled")
        for typ in ("L", "G"):
            with self.subTest(typ=typ):
                r = self.client.post(reverse(NEU), self.daten(typ=typ))
                self.assertEqual(r.status_code, 200, r.content[:400])
                a = Artikel.objects.get()
                self.assertEqual(a.typ, "A")
                self.assertEqual(self.we.positionen.get().artikel, a)
                self.we.positionen.all().delete()
                type(self.bp).objects.filter(pk=self.bp.pk).update(artikel=None)
                Preislistenartikel.objects.filter(pk=self.pl.pk).update(artikel=None)
                ArtikelLieferant.objects.all().delete()
                a.delete()

    def test_nicht_a_artikel_wird_nicht_uebernommen(self):
        bezug = services.we_bezug_holen(self.m, self.pl, self.we.pk, self.bp.pk)
        leistung = Artikel.objects.create(nummer="L1", typ="L", bezeichnung="x", steuersatz=self.st)
        Preislistenartikel.objects.filter(pk=self.pl.pk).update(artikel=leistung)
        with self.assertRaisesMessage(ValueError, "Typ A"):
            services.we_artikel_uebernehmen(bezug, leistung)

    def test_inzwischen_anderweitig_angelegt_gibt_keinen_zweiten_artikel(self):
        bezug = services.we_bezug_holen(self.m, self.pl, self.we.pk, "", "2")  # Scan-Weg, Formular offen
        services.artikel_aus_preisliste(self.pl)  # anderer Tab: schneller Weg legt A1 an
        self.pl.refresh_from_db()
        self.assertIsNone(services.we_bezug_holen(self.m, self.pl, self.we.pk, "", "2"))  # neu öffnen geht nicht mehr
        a2 = Artikel.objects.create(nummer="A2", typ="A", bezeichnung="x", steuersatz=self.st)
        with self.assertRaisesMessage(ValueError, "inzwischen einen eigenen Artikel"):
            services.we_artikel_uebernehmen(bezug, a2)
        self.assertEqual(self.we.positionen.count(), 0)

    def test_fremde_oder_falsche_position_wird_abgelehnt(self):
        anderer = Preislistenartikel.objects.create(lieferant=self.lf, bestellnummer="501", bezeichnung="Anderes", ek=Decimal("1"))
        r = self.client.get(reverse(NEU) + f"?kopie_von_pl={anderer.pk}&we={self.we.pk}&we_bp={self.bp.pk}")
        self.assertEqual(r.status_code, 404)
        fremd = Mandant.objects.create(kurzname="ZONE", firmenname="ZONE Handel")
        self.assertIsNone(services.we_bezug_holen(fremd, self.pl, self.we.pk, self.bp.pk))

    def test_fehlerhafte_eingabe_legt_nichts_an(self):
        r = self.client.post(reverse(NEU), self.daten(bezeichnung=""))
        self.assertEqual(r.status_code, 422)
        self.assertEqual(Artikel.objects.count(), 0)
        self.assertContains(r, f'name="we_bp" value="{self.bp.pk}"', status_code=422)  # Bezug bleibt im Formular

    def test_scan_rueckfrage_bietet_formular_mit_menge(self):
        r = self.client.get(reverse("einkauf:wareneingang", args=[self.we.pk]), {"p_code": "PSAZ-650", "menge": "3"})
        self.assertContains(r, "Im Formular anlegen …")
        self.assertContains(r, "we_menge=3")
        d = self.daten(we_bp="", we_menge="3")
        r = self.client.post(reverse(NEU), d)
        self.assertEqual(r.status_code, 200, r.content[:500])
        a = Artikel.objects.get()
        pos = self.we.positionen.get()
        self.assertEqual((pos.artikel, pos.menge), (a, Decimal("3")))

    def test_scan_fremder_lieferant_wird_abgelehnt(self):
        anderer_lf = Lieferant.objects.create(nummer="70300", name1="Kosatec", kurzname="Kosa")
        fremd = Preislistenartikel.objects.create(lieferant=anderer_lf, bestellnummer="K1", bezeichnung="x", ek=Decimal("1"))
        self.assertIsNone(services.we_bezug_holen(self.m, fremd, self.we.pk, "", "1"))
        self.assertIsNone(services.we_bezug_holen(self.m, self.pl, self.we.pk, "", "0"))
        self.assertIsNone(services.we_bezug_holen(self.m, self.pl, self.we.pk, "", "abc"))

    def test_dublettenhinweis_traegt_den_wareneingang_weiter(self):
        Artikel.objects.create(nummer="1", typ="A", bezeichnung="Alt", herstellernummer="PSAZ-650", steuersatz=self.st)
        r = self.client.get(self.url(we_bp=self.bp.pk))
        self.assertContains(r, "Artikel gibt es schon")
        self.assertContains(r, f"we={self.we.pk}")
        self.assertContains(r, f"we_bp={self.bp.pk}")

    def test_ohne_wareneingang_bleibt_es_beim_normalen_kopieren(self):
        d = self.daten()
        for k in ("we", "we_bp", "we_menge"):
            d.pop(k)
        r = self.client.post(reverse(NEU), d)
        a = Artikel.objects.get()
        self.assertEqual(r.json()["ziel"], reverse("lager:artikel", args=[a.pk]))
        self.assertEqual(self.we.positionen.count(), 0)
