"""Suche in „Alle Belege“ findet auch Artikel: Bezeichnung und Artikelnummer.

Vorher durchsuchte die Liste nur Belegnummer (samt der alten Nummern umgewandelter Belege),
Kundenname, Kundennummer und Referenz. Wer wissen wollte, in welchen Belegen ein bestimmter
Artikel steckt, musste die Belege einzeln öffnen.

Gesucht wird an drei Stellen, und die drei sind nicht dasselbe:
  * `positionen__bezeichnung`       – der Text, der IM BELEG steht; er wird beim Einfügen aus dem
                                      Artikel kopiert und kann danach von Hand geändert worden sein,
  * `positionen__artikel__nummer`   – die Artikelnummer,
  * `positionen__artikel__bezeichnung` – der heutige Name im Stamm; nach einer Umbenennung findet
                                      man den alten Beleg sonst unter dem heutigen Namen nicht mehr.
"""
from decimal import Decimal as D

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.kern.models import Mandant
from apps.stammdaten.models import Artikel, Kunde, Lieferant, Preislistenartikel, Steuersatz

from .models import Beleg, Belegart, Position


class BelegsucheArtikelTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.st = Steuersatz.objects.create(bezeichnung="19", prozent=D("19"))
        self.kunde = Kunde.objects.create(nummer="1000", name1="Muster GmbH", ort="Solingen")
        self.anderer = Kunde.objects.create(nummer="1001", name1="Zweite GmbH", ort="Solingen")
        self.client.force_login(get_user_model().objects.create_superuser("chef", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    def _beleg(self, kunde=None, **kopf):
        kopf.setdefault("art", Belegart.AUFTRAG)
        return Beleg.objects.create(mandant=self.m, kunde=kunde or self.kunde, **kopf)

    def _position(self, beleg, bezeichnung, artikel=None, pos=1):
        return Position.objects.create(beleg=beleg, pos=pos, bezeichnung=bezeichnung, artikel=artikel,
                                       menge=D("1"), einzelpreis=D("10"), steuer_prozent=D("19"))

    def _nummern(self, q):
        """Die Belege, die die Liste zu dieser Suche zeigt – ohne Zeitraumfilter."""
        r = self.client.get(reverse("verkauf:belege"), {"q": q, "status": "", "von": "", "bis": ""})
        self.assertEqual(r.status_code, 200)
        return sorted(b.pk for b in r.context["seite"].object_list)

    def test_findet_den_beleg_ueber_die_artikelnummer(self):
        artikel = Artikel.objects.create(nummer="TAS-4711", bezeichnung="Tastatur", steuersatz=self.st)
        treffer = self._beleg()
        self._position(treffer, "Tastatur", artikel)
        daneben = self._beleg()
        self._position(daneben, "Maus")

        self.assertEqual(self._nummern("TAS-4711"), [treffer.pk])
        self.assertNotIn(daneben.pk, self._nummern("TAS-4711"))

    def test_findet_den_beleg_ueber_die_bezeichnung_im_beleg(self):
        treffer = self._beleg()
        # Positionstext von Hand geändert: im Stamm heisst der Artikel anders
        self._position(treffer, "Sondertastatur mit Nummernblock",
                       Artikel.objects.create(nummer="A1", bezeichnung="Tastatur", steuersatz=self.st))
        self.assertEqual(self._nummern("Nummernblock"), [treffer.pk])

    def test_findet_den_beleg_auch_nach_umbenennung_des_artikels(self):
        # Der Beleg trägt noch den alten Text, der Stammartikel heisst inzwischen anders.
        artikel = Artikel.objects.create(nummer="A2", bezeichnung="Funkmaus Pro", steuersatz=self.st)
        treffer = self._beleg()
        self._position(treffer, "Maus alt", artikel)
        self.assertEqual(self._nummern("Funkmaus"), [treffer.pk])

    def test_position_ohne_eigenen_artikel_wird_ueber_den_text_gefunden(self):
        # Typ P: Preislistenartikel des Lieferanten, kein eigener Artikelsatz – `artikel` ist leer.
        lf = Lieferant.objects.create(nummer="L1", name1="Kosatec GmbH", kurzname="KOSATEC")
        pla = Preislistenartikel.objects.create(lieferant=lf, bestellnummer="K-1", bezeichnung="Dockingstation", ek=D("5"))
        treffer = self._beleg(art=Belegart.ANGEBOT)
        Position.objects.create(beleg=treffer, pos=1, typ="P", preisliste_artikel=pla, bezeichnung="Dockingstation",
                                menge=D("1"), einzelpreis=D("9"), steuer_prozent=D("19"))
        self.assertEqual(self._nummern("Dockingstation"), [treffer.pk])

    def test_beleg_steht_nur_einmal_in_der_liste(self):
        """Die Suche verbindet die Positionen dazu – ohne distinct stuende der Beleg je Treffer einmal da."""
        artikel = Artikel.objects.create(nummer="K-100", bezeichnung="Kabel", steuersatz=self.st)
        beleg = self._beleg()
        for i in range(1, 4):
            self._position(beleg, f"Kabel {i} Meter", artikel, pos=i)

        r = self.client.get(reverse("verkauf:belege"), {"q": "Kabel", "status": "", "von": "", "bis": ""})
        gefunden = [b.pk for b in r.context["seite"].object_list]
        self.assertEqual(gefunden, [beleg.pk])
        self.assertEqual(r.context["anzahl"], 1, "die Zählung darf den Beleg nicht mehrfach zählen")

    def test_jedes_wort_muss_passen(self):
        """Mehrere Wörter grenzen weiter ein – auch wenn sie aus verschiedenen Feldern kommen."""
        artikel = Artikel.objects.create(nummer="S-9", bezeichnung="Schrauben", steuersatz=self.st)
        meins = self._beleg(referenz="PO-2026")
        self._position(meins, "Schrauben", artikel)
        fremd = self._beleg(kunde=self.anderer, referenz="PO-2026")
        self._position(fremd, "Muttern")

        self.assertEqual(self._nummern("Schrauben PO-2026"), [meins.pk])
        self.assertEqual(sorted(self._nummern("PO-2026")), sorted([meins.pk, fremd.pk]))

    def test_die_bisherigen_felder_suchen_weiter(self):
        """Gegenprobe: Kunde, Referenz und Belegnummer dürfen durch die Erweiterung nicht verlorengehen."""
        beleg = self._beleg(referenz="REF-77", nummer="AB2026-00001")
        self._position(beleg, "irgendwas")
        for suche in ("Muster", "1000", "REF-77", "AB2026-00001"):
            with self.subTest(suche=suche):
                self.assertIn(beleg.pk, self._nummern(suche))
