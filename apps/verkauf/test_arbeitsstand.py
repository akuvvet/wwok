"""Positionsänderungen gelten erst nach dem Speichern; Verwerfen stellt den gespeicherten Stand wieder her."""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.kern.models import Mandant
from apps.lager.models import Seriennummer
from apps.stammdaten.models import Artikel, Kunde, KundeMandant, Steuersatz

from .models import Beleg, BelegArbeitsstand, Belegart, Position


@override_settings(SECURE_SSL_REDIRECT=False)
class ArbeitsstandTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.k = Kunde.objects.create(nummer="1", name1="K")
        self.st = Steuersatz.objects.create(bezeichnung="19", prozent=Decimal("19"))
        self.a = Artikel.objects.create(nummer="A1", bezeichnung="Gehäuse", steuersatz=self.st, vk1=Decimal("100"))
        self.client.force_login(get_user_model().objects.create_user("t", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()
        self.beleg = Beleg.objects.create(mandant=self.m, art=Belegart.ANGEBOT, kunde=self.k)
        self.p = Position.objects.create(beleg=self.beleg, artikel=self.a, bezeichnung="Gehäuse", einzelpreis=Decimal("100"))
        self.beleg.berechne()

    def url(self, name, *args):
        return reverse("verkauf:" + name, args=[self.beleg.pk, *args])

    def neu(self, **felder):
        return self.client.post(self.url("position_neu"), {"typ": "A", "artikel": "A1", "menge": "1", **felder})

    def verwerfen(self):
        self.client.post(self.url("beleg_verwerfen"))
        self.beleg.refresh_from_db()

    def stand(self):
        return BelegArbeitsstand.objects.filter(beleg=self.beleg).exists()

    def test_neue_position_laesst_sich_verwerfen(self):
        self.neu()
        self.assertTrue(self.stand())
        self.assertContains(self.client.get(self.url("beleg")), "Nicht gespeicherte Änderungen")
        self.verwerfen()
        self.assertFalse(self.stand())
        self.assertEqual(list(self.beleg.positionen.all()), [self.p])
        self.assertEqual(self.beleg.summe_netto, Decimal("100.00"))
        self.assertNotContains(self.client.get(self.url("beleg")), "Nicht gespeicherte Änderungen")

    def test_speichern_behaelt_die_aenderung(self):
        self.neu()
        self.client.post(self.url("beleg_speichern"))
        self.assertFalse(self.stand())
        self.assertEqual(self.beleg.positionen.count(), 2)

    def test_geloeschte_position_kommt_mit_seriennummer_zurueck(self):
        sn = Seriennummer.objects.create(mandant=self.m, artikel=self.a, nummer="SN1", position=self.p)
        self.client.post(self.url("position_loeschen", self.p.pk))
        self.assertFalse(self.beleg.positionen.exists())
        self.verwerfen()
        p = self.beleg.positionen.get()
        self.assertEqual(p.pk, self.p.pk)
        sn.refresh_from_db()
        self.assertEqual(sn.position_id, p.pk)

    def test_aenderung_und_gruppenwechsel_werden_zurueckgenommen(self):
        kopf = Position.objects.create(beleg=self.beleg, typ="G", bezeichnung="PC", einzelpreis=Decimal("0"))
        self.beleg.berechne()
        self.client.post(self.url("position_aendern", self.p.pk), {"menge": "5", "preis": "80", "rabatt": "0", "gruppe": str(kopf.pk)})
        self.p.refresh_from_db()
        self.assertEqual(self.p.gruppe_id, kopf.pk)
        self.verwerfen()
        self.p.refresh_from_db()
        kopf.refresh_from_db()
        self.assertIsNone(self.p.gruppe_id)
        self.assertEqual((self.p.menge, self.p.einzelpreis, kopf.einzelpreis), (Decimal("1"), Decimal("100"), Decimal("0")))
        self.assertEqual(self.beleg.summe_netto, Decimal("100.00"))

    def test_alte_unterposition_in_neuer_gruppe_ueberlebt_das_verwerfen(self):
        self.client.post(self.url("position_neu"), {"typ": "G", "bezeichnung": "PC", "menge": "1"})
        kopf = self.beleg.positionen.get(typ="G")
        self.client.post(self.url("position_aendern", self.p.pk), {"menge": "1", "preis": "100", "rabatt": "0", "gruppe": str(kopf.pk)})
        self.verwerfen()
        self.assertEqual(list(self.beleg.positionen.all()), [self.p])

    def test_fehlgeschlagene_aenderung_laesst_keinen_stand_zurueck(self):
        self.client.post(self.url("position_neu"), {"typ": "A", "artikel": "GIBTESNICHT", "menge": "1"})
        self.assertFalse(self.stand())
        self.client.post(self.url("position_aendern", 999999), {"menge": "1"})
        self.assertFalse(self.stand())

    def test_stand_bleibt_beim_zweiten_eingriff_der_erste(self):
        self.neu()
        self.neu()
        self.client.post(self.url("position_aendern", self.p.pk), {"menge": "9", "preis": "100", "rabatt": "0"})
        self.verwerfen()
        self.assertEqual(list(self.beleg.positionen.all()), [self.p])
        self.p.refresh_from_db()
        self.assertEqual(self.p.menge, Decimal("1"))

    def test_abschliessen_und_weiterfuehren_uebernehmen_die_aenderung(self):
        self.neu()
        self.assertEqual(self.client.post(self.url("beleg_abschliessen"), {"danach": ""}).status_code, 302)
        self.beleg.refresh_from_db()
        self.assertTrue(self.beleg.abgeschlossen)
        self.assertFalse(self.stand())
        self.assertEqual(self.beleg.positionen.count(), 2)

    def test_kopfdaten_speichern_uebernimmt_die_aenderung(self):
        self.neu()
        self.client.post(self.url("beleg_kopf"), {"versandkosten": "0"})
        self.assertFalse(self.stand())

    def test_alter_stand_wird_beim_oeffnen_verworfen(self):
        self.neu()
        BelegArbeitsstand.objects.filter(beleg=self.beleg).update(geaendert_am=timezone.now() - timedelta(hours=9))
        r = self.client.get(self.url("beleg"), follow=True)
        self.assertContains(r, "zu alt")
        self.assertEqual(self.beleg.positionen.count(), 1)

    def test_alter_stand_wird_in_der_kundenliste_verworfen_und_leerer_beleg_bleibt_erhalten(self):
        self.client.post(self.url("position_loeschen", self.p.pk))
        Beleg.objects.filter(pk=self.beleg.pk).update(angelegt_am=timezone.now() - timedelta(hours=1))
        self.client.get(reverse("verkauf:kunde", args=[self.k.pk]))
        self.assertTrue(Beleg.objects.filter(pk=self.beleg.pk).exists())
        BelegArbeitsstand.objects.filter(beleg=self.beleg).update(geaendert_am=timezone.now() - timedelta(hours=9))
        self.client.get(reverse("verkauf:kunde", args=[self.k.pk]))
        self.assertEqual(self.beleg.positionen.count(), 1)

    def test_fremder_mandant_kann_nicht_verwerfen(self):
        self.neu()
        anderer = Mandant.objects.create(kurzname="ZONE", firmenname="ZONE Handel")
        s = self.client.session
        s["mandant_id"] = anderer.pk
        s.save()
        self.assertEqual(self.client.post(self.url("beleg_verwerfen")).status_code, 404)
        self.assertTrue(self.stand())

    def test_verwerfen_ohne_offene_aenderung_sagt_das(self):
        r = self.client.post(self.url("beleg_verwerfen"), follow=True)
        self.assertContains(r, "keine offenen Änderungen")
        r = self.client.post(self.url("beleg_speichern"), follow=True)
        self.assertContains(r, "keine offenen Änderungen")

    def test_dialog_bekommt_bestaetigung_statt_weiterleitung(self):
        self.neu()
        r = self.client.post(self.url("beleg_speichern"), headers={"X-Wwok-Stand": "1"})
        self.assertEqual((r.status_code, r.json()), (200, {"ok": True}))

    def test_beleg_ohne_position_laesst_sich_nicht_speichern(self):
        self.client.post(self.url("position_loeschen", self.p.pk))
        r = self.client.post(self.url("beleg_speichern"), follow=True)
        self.assertContains(r, "ohne Position")
        self.assertTrue(self.stand())

    def test_offene_aenderungen_sperren_seriennummern_mail_und_bestellung(self):
        sn = Seriennummer.objects.create(mandant=self.m, artikel=self.a, nummer="SN1")
        self.neu()
        r = self.client.post(reverse("lager:position_sn", args=[self.p.pk]), {"seriennummern": "SN1"}, follow=True)
        self.assertContains(r, "nicht gespeicherte Änderungen")
        sn.refresh_from_db()
        self.assertIsNone(sn.position_id)
        r = self.client.post(reverse("verkauf:beleg_mail", args=[self.beleg.pk]), {"an": "a@example.org"}, follow=True)
        self.assertContains(r, "nicht gespeicherte Änderungen")
        r = self.client.post(reverse("einkauf:aus_beleg", args=[self.beleg.pk]), follow=True)
        self.assertContains(r, "nicht gespeicherte Änderungen")

    def test_neuer_beleg_verliert_nach_ablauf_seine_positionen_nicht(self):
        neu = Beleg.objects.create(mandant=self.m, art=Belegart.AUFTRAG, kunde=self.k)
        self.client.post(reverse("verkauf:position_neu", args=[neu.pk]), {"typ": "A", "artikel": "A1", "menge": "1"})
        BelegArbeitsstand.objects.filter(beleg=neu).update(geaendert_am=timezone.now() - timedelta(hours=9))
        self.client.get(reverse("verkauf:kunde", args=[self.k.pk]))
        self.assertEqual(neu.positionen.count(), 1)
        self.assertFalse(BelegArbeitsstand.objects.filter(beleg=neu).exists())

    def test_seriennummer_an_anderem_beleg_bleibt_beim_verwerfen_dort(self):
        sn = Seriennummer.objects.create(mandant=self.m, artikel=self.a, nummer="SN1", position=self.p)
        self.client.post(self.url("position_loeschen", self.p.pk))
        anderer = Beleg.objects.create(mandant=self.m, art=Belegart.ANGEBOT, kunde=self.k)
        fremd = Position.objects.create(beleg=anderer, artikel=self.a, bezeichnung="X", einzelpreis=Decimal("1"))
        Seriennummer.objects.filter(pk=sn.pk).update(position=fremd)
        self.verwerfen()
        sn.refresh_from_db()
        self.assertEqual(sn.position_id, fremd.pk)

    def test_folgeposition_behaelt_ihren_verweis(self):
        folge = Beleg.objects.create(mandant=self.m, art=Belegart.LIEFERSCHEIN, kunde=self.k)
        kopie = Position.objects.create(beleg=folge, artikel=self.a, bezeichnung="Gehäuse", einzelpreis=Decimal("100"), quellposition=self.p)
        self.client.post(self.url("position_loeschen", self.p.pk))
        kopie.refresh_from_db()
        self.assertIsNone(kopie.quellposition_id)
        self.verwerfen()
        kopie.refresh_from_db()
        self.assertEqual(kopie.quellposition_id, self.p.pk)

    def test_umstellung_von_p_auf_a_bleibt_beim_verwerfen(self):
        from apps.einkauf.services import artikel_aus_preisliste
        from apps.stammdaten.models import Lieferant, Preislistenartikel
        lieferant = Lieferant.objects.create(nummer="L1", name1="Liefer GmbH")
        eintrag = Preislistenartikel.objects.create(lieferant=lieferant, bestellnummer="B1", bezeichnung="Teil", ek=Decimal("5"))
        p = Position.objects.create(beleg=self.beleg, typ="P", preisliste_artikel=eintrag, bezeichnung="Teil", einzelpreis=Decimal("9"))
        self.client.post(self.url("position_aendern", self.p.pk), {"menge": "2", "preis": "100", "rabatt": "0"})
        artikel = artikel_aus_preisliste(eintrag)
        self.verwerfen()
        p.refresh_from_db()
        self.assertEqual((p.typ, p.artikel_id, p.preisliste_artikel_id), ("A", artikel.pk, None))

    def test_folgebeleg_und_kopie_uebernehmen_die_aenderung(self):
        self.neu()
        self.client.post(self.url("beleg_folgebeleg"))
        self.assertFalse(self.stand())
        self.neu()
        self.client.post(self.url("beleg_kopieren"), {"art": "AB", "urbeleg_abschliessen": "0"})
        self.assertFalse(self.stand())

    def test_fehlerhafter_stand_blockiert_die_listen_nicht(self):
        self.neu()
        BelegArbeitsstand.objects.filter(beleg=self.beleg).update(daten="kein json", geaendert_am=timezone.now() - timedelta(hours=9))
        with self.assertLogs("apps.verkauf.models", level="ERROR"):
            self.assertEqual(self.client.get(reverse("verkauf:kunde", args=[self.k.pk])).status_code, 200)

    def test_verlassen_schutz_nur_fuer_den_bearbeiter(self):
        self.neu()
        self.assertContains(self.client.get(self.url("beleg")), "Dieser Beleg hat nicht gespeicherte Änderungen")
        anderer = get_user_model().objects.create_user("b", password="x" * 14)
        self.client.force_login(anderer)
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()
        r = self.client.get(self.url("beleg"))
        self.assertContains(r, "Nicht gespeicherte Änderungen von t")
        self.assertNotContains(r, "Dieser Beleg hat nicht gespeicherte Änderungen")

    def test_kreditwarnung_steht_in_der_kopfkarte_unter_netto(self):
        KundeMandant.objects.create(kunde=self.k, mandant=self.m, kreditlimit=Decimal("10"))
        Beleg.objects.filter(pk=self.beleg.pk).update(art=Belegart.AUFTRAG)
        html = self.client.get(self.url("beleg")).content.decode()
        self.assertIn("kreditwarnung", html)
        self.assertLess(html.index("netto "), html.index('class="warnung kreditwarnung"'))
        self.assertLess(html.index('class="warnung kreditwarnung"'), html.index('class="aktionen"'))
