"""Wunsch des Nutzers 18.11.2026: Reicht der Lagerbestand für Kundenbestellungen nicht, kommt die Fehlmenge in den
Bestellvorschlag – auch ohne Mindestbestand. Beispiel: 4 bestellt, 2 am Lager → 2 vorschlagen."""
from decimal import Decimal as D

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.kern.models import Mandant
from apps.lager.models import Bewegungsart
from apps.lager.services import buche_manuell
from apps.stammdaten.models import Artikel, ArtikelLieferant, ArtikelMandant, Kunde, Lieferant, Steuersatz
from apps.verkauf.models import Beleg, Belegart, Position

from . import services
from .models import BestellStatus


class VorschlagKundenbestellungTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.lf = Lieferant.objects.create(nummer="L1", name1="Techdata GmbH", kurzname="TD")
        self.maus = Artikel.objects.create(nummer="M1", bezeichnung="Maus", steuersatz=Steuersatz.objects.get(nummer=1), vk1=D("15"))
        ArtikelLieferant.objects.create(artikel=self.maus, lieferant=self.lf, bestellnummer="TD-1", ek=D("9"))
        self.kunde = Kunde.objects.create(nummer="1000", name1="Muster GmbH")
        buche_manuell(self.m, self.maus, Bewegungsart.WARENEINGANG, D("2"))

    def kundenbestellung(self, menge, mandant=None, reservieren=True):
        b = Beleg.objects.create(mandant=mandant or self.m, art=Belegart.AUFTRAG, kunde=self.kunde)
        Position.objects.create(beleg=b, artikel=self.maus, bezeichnung="Maus", menge=D(menge), einzelpreis=D("15"), reservieren=reservieren)
        return b

    def zeile(self):
        v = [z for z in services.bestellvorschlag(self.m) if z["artikel"] == self.maus]
        return v[0] if v else None

    def test_beispiel_des_nutzers_4_bestellt_2_am_lager(self):
        ab = self.kundenbestellung("4")
        z = self.zeile()
        self.assertEqual((z["menge"], z["ziel"], z["lager"], z["kundenbedarf"], z["haben"]), (D("2"), D("0"), D("2"), D("4"), D("-2")))
        self.assertEqual(z["grund"], "Kundenbestellung")
        self.assertEqual(z["kundenbelege"], [ab])
        self.assertEqual(z["lieferant"], self.lf)

    def test_genug_am_lager_kein_vorschlag(self):
        self.kundenbestellung("2")
        self.assertIsNone(self.zeile())

    def test_mehrere_kundenbestellungen_summieren(self):
        a, b = self.kundenbestellung("3"), self.kundenbestellung("2")
        z = self.zeile()
        self.assertEqual(z["menge"], D("3"))
        self.assertEqual(z["kundenbelege"], [a, b])

    def test_position_ohne_haken_r_ist_trotzdem_bedarf(self):
        self.kundenbestellung("5", reservieren=False)
        self.assertEqual(self.zeile()["menge"], D("3"))

    def test_schon_beim_lieferanten_bestellt_wird_abgezogen(self):
        self.kundenbestellung("4")
        b = services.bestellung_anlegen(self.m, self.lf)
        services.bestellposition(b, self.maus, D("1"))
        self.assertEqual(self.zeile()["menge"], D("1"))
        services.bestellposition(b, self.maus, D("1"))
        self.assertIsNone(self.zeile())  # Entwurf zählt mit: zweiter Klick legt nichts doppelt an

    def test_aus_vorschlag_bestellen_deckt_den_bedarf(self):
        self.kundenbestellung("4")
        liste = services.bestellungen_aus_vorschlag(self.m, [(self.maus.pk, self.zeile()["menge"])])
        self.assertEqual((liste[0].status, liste[0].positionen.get().menge), (BestellStatus.ENTWURF, D("2")))
        self.assertIsNone(self.zeile())

    def test_mindestbestand_und_kundenbedarf_addieren_sich(self):
        ArtikelMandant.objects.create(artikel=self.maus, mandant=self.m, mindestbestand=5)
        self.kundenbestellung("4")
        z = self.zeile()
        self.assertEqual(z["menge"], D("7"))  # 5 Mindestbestand − (2 Lager − 4 Kunden)
        self.assertEqual(z["grund"], "Mindestbestand + Kundenbestellung")

    def test_mindestbestand_ohne_fehlmenge_nennt_nur_den_mindestbestand(self):
        ArtikelMandant.objects.create(artikel=self.maus, mandant=self.m, mindestbestand=5)
        self.kundenbestellung("1")
        self.assertEqual((self.zeile()["menge"], self.zeile()["grund"]), (D("4"), "Mindestbestand"))

    def test_abgeschlossene_kundenbestellung_ohne_offene_menge_zaehlt_nicht(self):
        ab = self.kundenbestellung("4")
        ab.positionen.update(menge=D("1"))
        self.assertIsNone(self.zeile())

    def test_anderer_mandant_zaehlt_nicht(self):
        zone = Mandant.objects.create(kurzname="ZONE", firmenname="ZONE Handel")
        self.kundenbestellung("9", mandant=zone)
        self.assertIsNone(self.zeile())

    def test_inaktiver_artikel_fehlt(self):
        self.kundenbestellung("4")
        Artikel.objects.filter(pk=self.maus.pk).update(nicht_aktiv=True)
        self.assertIsNone(self.zeile())

    def test_seite_zeigt_lager_kunden_und_beleg(self):
        ab = self.kundenbestellung("4")
        self.client.force_login(get_user_model().objects.create_superuser("chef", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()
        r = self.client.get(reverse("einkauf:vorschlag"))
        self.assertContains(r, 'name="menge_%d" value="2"' % self.maus.pk)
        self.assertContains(r, "Kundenbestellung")
        self.assertContains(r, reverse("verkauf:beleg", args=[ab.pk]))
