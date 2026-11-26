"""Prüfbericht 14.11.2026, Abschnitt C (Verkauf): Spalte B der Gutschrift, Sperre bei Positionsänderungen, Gruppenartikel im Abo,
Gutschriftentwurf, Seriennummern beim Löschen einer Position."""
from datetime import date
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.db.models.query import QuerySet
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.kern.models import Mandant
from apps.lager.models import Bewegungsart, Seriennummer
from apps.lager.services import buche_manuell, seriennummern_einlagern, seriennummern_zuordnen
from apps.stammdaten.models import Artikel, GruppenKomponente, Kunde, Steuersatz

from .models import Abo, AboPosition, Beleg, Belegart, Nummernkreis, Position, Ruecknahme


@override_settings(SECURE_SSL_REDIRECT=False)
class PruefberichtCTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.kunde = Kunde.objects.create(nummer="1", name1="Kunde GmbH")
        self.st = Steuersatz.objects.create(bezeichnung="19", prozent=Decimal("19"))
        self.a = Artikel.objects.create(nummer="A1", bezeichnung="Notebook", steuersatz=self.st, vk1=Decimal("10"))
        self.l = Artikel.objects.create(nummer="L1", bezeichnung="Arbeitszeit", typ="L", steuersatz=self.st)
        buche_manuell(self.m, self.a, Bewegungsart.WARENEINGANG, Decimal("20"))
        self.user = get_user_model().objects.create_user("t", password="x" * 14)
        self.client.force_login(self.user)
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    def rechnung(self, artikel=None, menge="2"):
        artikel = artikel or self.a
        re = Beleg.objects.create(mandant=self.m, art=Belegart.RECHNUNG, kunde=self.kunde)
        Position.objects.create(beleg=re, artikel=artikel, typ=artikel.typ, bezeichnung=artikel.bezeichnung,
                                menge=Decimal(menge), einzelpreis=Decimal("10"))
        re.abschliessen()
        re.refresh_from_db()
        return re

    # Befund 4: Spalte B der Gutschrift
    def test_spalte_b_abgeschlossene_gutschrift_bleibt_unveraendert(self):
        gs = self.rechnung().gutschrift_erzeugen(Ruecknahme.REPARATUR)
        gs.abschliessen()
        p = gs.positionen.get()
        Position.objects.filter(pk=p.pk).update(ruecknahme="")  # Altzustand: die Sammel-update()-Zeile greift sonst auf alle
        url = reverse("verkauf:position_ruecknahme", args=[gs.pk, p.pk])
        self.client.post(url)
        p.refresh_from_db()
        self.assertEqual(p.ruecknahme, "")  # früher: update() setzte R auch am abgeschlossenen Beleg
        Position.objects.filter(pk=p.pk).update(ruecknahme=Ruecknahme.LAGER)
        self.client.post(url)
        p.refresh_from_db()
        self.assertEqual(p.ruecknahme, Ruecknahme.LAGER)  # auch der Klick R -> B -> - bleibt gesperrt

    def test_spalte_b_offene_gutschrift_wechselt_weiter(self):
        gs = self.rechnung().gutschrift_erzeugen(Ruecknahme.REPARATUR)
        p = gs.positionen.get()
        Position.objects.filter(pk=p.pk).update(ruecknahme="")
        self.client.post(reverse("verkauf:position_ruecknahme", args=[gs.pk, p.pk]))
        p.refresh_from_db()
        self.assertEqual(p.ruecknahme, Ruecknahme.REPARATUR)

    # Befund 5: Belegzeile sperren
    def test_positions_views_sperren_die_belegzeile(self):
        angebot = Beleg.objects.create(mandant=self.m, art=Belegart.ANGEBOT, kunde=self.kunde)
        orig = QuerySet.select_for_update
        gesperrt = []

        def spion(qs, *args, **kwargs):
            gesperrt.append(qs.model)
            return orig(qs, *args, **kwargs)
        with mock.patch.object(QuerySet, "select_for_update", spion):
            self.client.post(reverse("verkauf:position_neu", args=[angebot.pk]),
                             {"typ": "L", "bezeichnung": "Beratung", "preis": "5", "menge": "1"})
            self.assertEqual(gesperrt.count(Beleg), 1)
            p = angebot.positionen.get()
            self.client.post(reverse("verkauf:position_aendern", args=[angebot.pk, p.pk]), {"menge": "2", "preis": "5", "rabatt": "0"})
            self.assertEqual(gesperrt.count(Beleg), 2)
            self.client.post(reverse("verkauf:position_loeschen", args=[angebot.pk, p.pk]))
            self.assertEqual(gesperrt.count(Beleg), 3)
        self.assertFalse(angebot.positionen.exists())

    def test_position_neu_fehler_rollt_zurueck_und_abgeschlossen_bleibt_gesperrt(self):
        angebot = Beleg.objects.create(mandant=self.m, art=Belegart.ANGEBOT, kunde=self.kunde)
        self.client.post(reverse("verkauf:position_neu", args=[angebot.pk]), {"typ": "L", "bezeichnung": "x", "preis": "5", "menge": "0"})
        self.assertFalse(angebot.positionen.exists())
        re = self.rechnung()
        self.client.post(reverse("verkauf:position_neu", args=[re.pk]), {"typ": "L", "bezeichnung": "x", "preis": "5", "menge": "1"})
        self.assertEqual(re.positionen.count(), 1)

    # Befund 6: Gruppenartikel im Abo
    def test_abo_gruppenartikel_mit_komponenten(self):
        g = Artikel.objects.create(nummer="G1", bezeichnung="Paket", typ="G", steuersatz=self.st)
        GruppenKomponente.objects.create(gruppe=g, komponente=self.a, menge=Decimal("2"))
        abo = Abo.objects.create(mandant=self.m, kunde=self.kunde, bezeichnung="Paket", monate=1, naechster_termin=date(2026, 11, 1))
        AboPosition.objects.create(abo=abo, artikel=g, bezeichnung="Paket", menge=Decimal("3"), einzelpreis=Decimal("100"),
                                   steuer_prozent=Decimal("19"))
        ab = abo.rechnung_erzeugen(date(2026, 11, 1))
        kopf = ab.positionen.get(gruppe__isnull=True)
        self.assertEqual((kopf.typ, kopf.preis_fest, kopf.einzelpreis, kopf.menge), ("G", True, Decimal("100"), Decimal("3")))
        teil = kopf.unterpositionen.get()
        self.assertEqual((teil.artikel, teil.menge), (self.a, Decimal("6")))  # Komponentenmenge x Gruppenmenge
        self.assertEqual(ab.summe_netto, Decimal("300.00"))  # versteuert wird der Kopf, die Unterposition kostet nichts extra

    def test_abo_normaler_artikel_bleibt_einzelposition(self):
        abo = Abo.objects.create(mandant=self.m, kunde=self.kunde, bezeichnung="Mäuse", monate=1, naechster_termin=date(2026, 11, 1))
        AboPosition.objects.create(abo=abo, artikel=self.a, bezeichnung="Notebook", menge=Decimal("1"), einzelpreis=Decimal("10"))
        ab = abo.rechnung_erzeugen(date(2026, 11, 1))
        self.assertEqual([(p.typ, p.gruppe_id) for p in ab.positionen.all()], [("A", None)])

    # Befund 8: Seriennummern beim Löschen einer Position
    def seriennummer_an(self, position, nummer):
        seriennummern_einlagern(self.m, position.artikel, nummer)
        seriennummern_zuordnen(position, nummer)
        return Seriennummer.objects.get(nummer=nummer)

    def test_position_loeschen_gibt_seriennummer_frei(self):
        re = Beleg.objects.create(mandant=self.m, art=Belegart.RECHNUNG, kunde=self.kunde)
        p = Position.objects.create(beleg=re, artikel=self.a, typ="A", bezeichnung="Notebook", menge=Decimal("1"), einzelpreis=Decimal("10"))
        sn = self.seriennummer_an(p, "SN-1")
        self.assertEqual(sn.status, Seriennummer.VERKAUFT)
        self.client.post(reverse("verkauf:position_loeschen", args=[re.pk, p.pk]))
        sn.refresh_from_db()
        self.assertEqual((sn.status, sn.position_id), (Seriennummer.IM_LAGER, None))
        neu = Position.objects.create(beleg=re, artikel=self.a, typ="A", bezeichnung="Notebook", menge=Decimal("1"), einzelpreis=Decimal("10"))
        seriennummern_zuordnen(neu, "SN-1")  # wieder zuordenbar, früher: „nicht im Lager“
        sn.refresh_from_db()
        self.assertEqual(sn.position_id, neu.pk)

    def test_gruppe_loeschen_gibt_seriennummern_der_unterposition_frei(self):
        re = Beleg.objects.create(mandant=self.m, art=Belegart.RECHNUNG, kunde=self.kunde)
        kopf = Position.objects.create(beleg=re, typ="G", bezeichnung="Paket", menge=Decimal("1"), einzelpreis=Decimal("50"), preis_fest=True)
        teil = Position.objects.create(beleg=re, gruppe=kopf, artikel=self.a, typ="A", bezeichnung="Notebook", menge=Decimal("1"),
                                       einzelpreis=Decimal("10"))
        sn = self.seriennummer_an(teil, "SN-G")
        self.client.post(reverse("verkauf:position_loeschen", args=[re.pk, kopf.pk]))
        sn.refresh_from_db()
        self.assertEqual((sn.status, sn.position_id), (Seriennummer.IM_LAGER, None))
        self.assertFalse(re.positionen.exists())

    def test_andere_seriennummer_bleibt_beim_loeschen_unberuehrt(self):
        re = Beleg.objects.create(mandant=self.m, art=Belegart.RECHNUNG, kunde=self.kunde)
        p1 = Position.objects.create(beleg=re, artikel=self.a, typ="A", bezeichnung="Notebook", menge=Decimal("1"), einzelpreis=Decimal("10"))
        p2 = Position.objects.create(beleg=re, artikel=self.a, typ="A", bezeichnung="Notebook", menge=Decimal("1"), einzelpreis=Decimal("10"))
        sn1, sn2 = self.seriennummer_an(p1, "SN-A"), self.seriennummer_an(p2, "SN-B")
        self.client.post(reverse("verkauf:position_loeschen", args=[re.pk, p1.pk]))
        sn1.refresh_from_db()
        sn2.refresh_from_db()
        self.assertEqual(sn1.status, Seriennummer.IM_LAGER)
        self.assertEqual((sn2.status, sn2.position_id), (Seriennummer.VERKAUFT, p2.pk))

    # Befund 1: Nummern gelöschter Bestellungen werden im Normalbetrieb nicht neu vergeben
    def test_nummer_geloeschter_bestellung_wird_nicht_neu_vergeben(self):
        ab = Beleg.objects.create(mandant=self.m, art=Belegart.AUFTRAG, kunde=self.kunde)
        Position.objects.create(beleg=ab, artikel=self.a, typ="A", bezeichnung="Notebook", menge=Decimal("2"), einzelpreis=Decimal("10"))
        ab.abschliessen()
        alt = ab.nummer
        ls = ab.kopieren_als(Belegart.LIEFERSCHEIN)
        ab.refresh_from_db()
        if ab.loeschhindernis() is None:
            ab.delete()
        else:
            self.skipTest("Bestellung nach dem Übernehmen nicht löschbar")
        neu = Beleg.objects.create(mandant=self.m, art=Belegart.AUFTRAG, kunde=self.kunde)
        Position.objects.create(beleg=neu, artikel=self.a, typ="A", bezeichnung="Notebook", menge=Decimal("1"), einzelpreis=Decimal("10"))
        neu.abschliessen()
        self.assertNotEqual(neu.nummer, alt)
        self.assertTrue(ls.pk)
        self.assertEqual(Nummernkreis.objects.get(mandant=self.m, art="AB").letzte_nummer, 2)
