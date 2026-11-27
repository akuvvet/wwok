"""Geteilte Liste „Alle Belege“: unten die Positionen der angeklickten Zeile.

Die Markierung steht als `pos=<Beleg-Nr.>` in der Adresse, nicht in der Sitzung – Neuladen,
Zurück und ein geteilter Link zeigen dieselbe Zeile. Gewählt wird nur aus der angezeigten
Seite; damit gehört der Beleg immer dem angemeldeten Mandanten und passt zu den Filtern.

Zwei Stellen, an denen ein Parameter mehr sonst Schaden anrichtet, sind hier festgehalten:
`pos` darf die Vorauswahl „nicht abgeschlossen, letzte 30 Tage“ nicht aufheben, und es darf
nicht in die Blätterlinks geraten – eine Seite weiter zeigt andere Belege.
"""
import re
from decimal import Decimal as D

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.kern.models import Mandant
from apps.stammdaten.models import Artikel, Kunde, Steuersatz

from .models import Beleg, Belegart, BelegUebernahme, Position


class BelegePositionenTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.fremd = Mandant.objects.create(kurzname="ZONE", firmenname="ZONE Handel")
        self.st = Steuersatz.objects.create(bezeichnung="19", prozent=D("19"))
        self.kunde = Kunde.objects.create(nummer="1000", name1="Muster GmbH", ort="Solingen")
        self.client.force_login(get_user_model().objects.create_superuser("chef", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()
        self.artikel = Artikel.objects.create(nummer="TAS-4711", bezeichnung="Tastatur", steuersatz=self.st)

    def _beleg(self, mandant=None, **kopf):
        kopf.setdefault("art", Belegart.AUFTRAG)
        return Beleg.objects.create(mandant=mandant or self.m, kunde=self.kunde, **kopf)

    def _position(self, beleg, bezeichnung, artikel=None, pos=1, menge="1"):
        return Position.objects.create(beleg=beleg, pos=pos, bezeichnung=bezeichnung, artikel=artikel,
                                       menge=D(menge), einzelpreis=D("10"), steuer_prozent=D("19"))

    def _liste(self, **filter):
        """Die Liste mit leeren Zeitraum- und Statusfiltern, damit die Vorauswahl nicht dazwischenkommt."""
        r = self.client.get(reverse("verkauf:belege"), {"status": "", "von": "", "bis": "", **filter})
        self.assertEqual(r.status_code, 200)
        return r

    def test_ohne_markierung_steht_unten_nichts(self):
        beleg = self._beleg(nummer="AB2026-00001")
        self._position(beleg, "Tastatur", self.artikel)
        html = self._liste().content.decode()

        self.assertIn("data-positionen", html)  # der Bereich ist da, damit das Skript ihn füllen kann
        self.assertIn("hinweis-klick", html)  # und erklärt sich
        self.assertNotIn("Positionen zu", html)
        self.assertNotIn('aria-selected="true"', html)

    def test_markierte_zeile_zeigt_ihre_positionen(self):
        beleg = self._beleg(nummer="AB2026-00001")
        self._position(beleg, "Tastatur mit Nummernblock", self.artikel, menge="3")
        self._position(beleg, "Versandkosten", pos=2)
        anderer = self._beleg(nummer="AB2026-00002")
        self._position(anderer, "Monitor 27 Zoll")

        r = self._liste(pos=beleg.pk)
        html = r.content.decode()

        self.assertEqual(r.context["gewaehlt"], beleg)
        self.assertEqual([p.bezeichnung for p in r.context["positionen"]],
                         ["Tastatur mit Nummernblock", "Versandkosten"])
        self.assertIn("Positionen zu", html)
        self.assertIn("Tastatur mit Nummernblock", html)
        self.assertIn("TAS-4711", html)  # Artikelnummer der Position
        self.assertIn("Versandkosten", html)
        self.assertNotIn("Monitor 27 Zoll", html)  # nur die Positionen der markierten Zeile
        self.assertNotIn("{#", html)  # ein mehrzeiliges {# … #} ist kein Kommentar und stünde sichtbar auf der Seite
        self.assertIn(f'data-beleg="{beleg.pk}" tabindex="0" aria-selected="true"', html)
        # Pos. und Artikel-Nr. haben feste Breiten, lange Artikelnummern stehen voll im Tooltip
        self.assertIn('table-layout:fixed', html)
        self.assertIn('<col style="width:56px"><col style="width:170px">', html)
        self.assertIn('title="TAS-4711"', html)

    def test_jede_zeile_ist_anklickbar_und_nur_eine_im_tabulatorweg(self):
        for i in (1, 2, 3):
            self._position(self._beleg(nummer=f"AB2026-0000{i}"), "Tastatur", self.artikel)
        html = self._liste().content.decode()

        # Nur die Zeilen zählen: tabindex steht auch an Knöpfen der Seitenleiste
        zeilen = re.findall(r'data-beleg="\d+" tabindex="(0|-1)"', html)

        self.assertEqual(len(zeilen), 3)
        self.assertEqual(zeilen.count("0"), 1, "ohne Markierung ist nur die erste Zeile im Tabulatorweg")
        self.assertEqual(zeilen.count("-1"), 2)

    def test_beleg_eines_anderen_mandanten_wird_nicht_gezeigt(self):
        meiner = self._beleg(nummer="AB2026-00001")
        self._position(meiner, "Tastatur", self.artikel)
        fremder = self._beleg(mandant=self.fremd, nummer="ZB2026-00001")
        self._position(fremder, "Geheime Sonderanfertigung")

        r = self._liste(pos=fremder.pk)

        self.assertIsNone(r.context["gewaehlt"])
        self.assertEqual(r.context["positionen"], [])
        self.assertNotIn("Geheime Sonderanfertigung", r.content.decode())

    def test_beleg_ausserhalb_der_filter_wird_nicht_gezeigt(self):
        """Wer filtert, markiert damit ab: die Positionen einer nicht mehr gezeigten Zeile stehen nicht unten."""
        offen = self._beleg(nummer="AB2026-00001", abgeschlossen=False)
        self._position(offen, "Tastatur", self.artikel)

        r = self.client.get(reverse("verkauf:belege"), {"status": "abgeschlossen", "von": "", "bis": "", "pos": offen.pk})

        self.assertIsNone(r.context["gewaehlt"])
        self.assertNotIn("Positionen zu", r.content.decode())

    def test_unsinnige_markierung_stoert_die_liste_nicht(self):
        beleg = self._beleg(nummer="AB2026-00001")
        self._position(beleg, "Tastatur", self.artikel)
        for wert in ("abc", "", "0", "999999", "-1", "1 OR 1=1"):
            with self.subTest(pos=wert):
                r = self._liste(pos=wert)
                self.assertIsNone(r.context["gewaehlt"])
                self.assertIn("AB2026-00001", r.content.decode())  # die Liste selbst steht weiter

    def test_markierung_hebt_die_vorauswahl_der_letzten_30_tage_nicht_auf(self):
        """Ohne Filter zeigt die Liste nicht abgeschlossene Belege der letzten 30 Tage. `pos` ist kein Filter."""
        beleg = self._beleg(nummer="AB2026-00001")
        self._position(beleg, "Tastatur", self.artikel)

        ohne = self.client.get(reverse("verkauf:belege"))
        mit = self.client.get(reverse("verkauf:belege"), {"pos": beleg.pk})

        self.assertTrue(ohne.context["standard_30"])
        self.assertTrue(mit.context["standard_30"], "pos darf die Vorauswahl nicht aufheben")
        self.assertEqual(mit.context["gewaehlt"], beleg)

    def test_blaettern_nimmt_die_markierung_nicht_mit(self):
        beleg = self._beleg(nummer="AB2026-00001")
        self._position(beleg, "Tastatur", self.artikel)

        r = self._liste(pos=beleg.pk, q="Tastatur")

        self.assertNotIn("pos", r.context["parameter"])  # steckt in den Blätter- und Schließen-Links
        self.assertIn("q=Tastatur", r.context["parameter"])  # die Filter dagegen bleiben

    def test_beleg_ohne_positionen_sagt_es(self):
        """Eine leergeräumte Bestellung steht weiter in der Liste: ihre Positionen sind in den Lieferschein gewandert."""
        quelle = self._beleg(nummer="AB2026-00001")
        ziel = self._beleg(nummer="LS2026-00001", art=Belegart.LIEFERSCHEIN, vorgaenger=quelle)
        position = self._position(ziel, "Tastatur", self.artikel)
        BelegUebernahme.objects.create(mandant=self.m, quelle=quelle, quelle_nummer=quelle.nummer, ziel=ziel,
                                       ziel_art=Belegart.LIEFERSCHEIN, zielposition=position, menge=D("1"),
                                       bezeichnung="Tastatur")

        r = self._liste(pos=quelle.pk)

        self.assertEqual(r.context["gewaehlt"], quelle)
        self.assertEqual(r.context["positionen"], [])
        self.assertIn("keine Positionen", r.content.decode())
