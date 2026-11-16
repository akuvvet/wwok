"""Spalte R der Bestellung: Ohne Haken reserviert eine Position keinen Lagerbestand (sie wartet auf den Wareneingang).
Beispiel des Nutzers: 5 Windows am Lager, Kunde A bestellt 10, Kunde B bestellt 3. Erst wenn A den Haken herausnimmt,
lässt sich B heute beliefern."""
from decimal import Decimal as D

from django.urls import reverse

from apps.kern.models import AenderungsProtokoll, Mandant
from apps.lager.models import Bewegungsart, bestand, reserviert, verfuegbar
from apps.lager.services import buche_beleg, buche_manuell, fehlbestand
from apps.stammdaten.models import Artikel, Steuersatz

from . import uebernahme
from .models import Beleg, Belegart, Position
from .test_vorlaeufig import VorlaeufigBasis


class ReservierenTest(VorlaeufigBasis):
    def setUp(self):
        super().setUp()
        self.win = Artikel.objects.create(nummer="W11", bezeichnung="Windows 11", steuersatz=Steuersatz.objects.get(nummer=1), vk1=D("100"))
        buche_manuell(self.m, self.win, Bewegungsart.WARENEINGANG, D("5"))
        self.a = self.beleg()
        self.pa = self.pos(self.a, self.win, menge="10", preis="100")
        self.a.berechne()
        self.b = self.beleg()
        self.pb = self.pos(self.b, self.win, menge="3", preis="100")
        self.b.berechne()

    def haken(self, beleg, position, an: bool, **kw):
        return self.client.post(reverse("verkauf:position_reservieren", args=[beleg.pk, position.pk]),
                                {"reservieren": "1"} if an else {}, **kw)

    def lieferschein_b(self):
        return self.b.folgebeleg_erzeugen(art=Belegart.LIEFERSCHEIN, vorlaeufig=True)

    def test_standard_alle_positionen_reservieren(self):
        self.assertTrue(self.pa.reservieren)
        self.assertEqual(reserviert(self.m, self.win), D("13"))
        self.assertTrue(fehlbestand(self.lieferschein_b()))  # die 5 Stück gehören Kunde A

    def test_ohne_haken_ist_der_bestand_fuer_andere_frei(self):
        r = self.haken(self.a, self.pa, False, follow=True)
        self.assertContains(r, "Reservierung aufgehoben")
        self.assertFalse(Position.objects.get(pk=self.pa.pk).reservieren)
        self.assertEqual((reserviert(self.m, self.win), verfuegbar(self.m, self.win)), (D("3"), D("2")))
        ls = self.lieferschein_b()
        self.assertEqual(fehlbestand(ls), [])
        buche_beleg(ls)
        self.assertEqual(bestand(self.m, self.win), D("2"))  # nie unter 0

    def test_haken_wieder_setzen_reserviert_erneut(self):
        self.haken(self.a, self.pa, False)
        r = self.haken(self.a, self.pa, True, follow=True)
        self.assertContains(r, "wird wieder reserviert")
        self.assertEqual(reserviert(self.m, self.win), D("13"))

    def test_maske_zeigt_spalte_r_haken_und_hinweis(self):
        url = reverse("verkauf:beleg", args=[self.a.pk])
        r = self.client.get(url)
        self.assertContains(r, ">R</th>")
        self.assertContains(r, 'name="reservieren" value="1" checked')
        self.assertNotContains(r, "nicht reserviert")
        self.haken(self.a, self.pa, False)
        r = self.client.get(url)
        self.assertContains(r, 'name="reservieren" value="1"  onchange')
        self.assertContains(r, "nicht reserviert – wartet auf Wareneingang")
        self.assertContains(r, "nur 2 verfügbar")  # 5 am Lager, 3 für Kunde B reserviert
        self.assertTrue(r.context["positionen"][0].fehlt)

    def test_eigene_reservierung_zaehlt_nur_mit_haken(self):
        """Mit Haken blockiert die eigene Reservierung die eigene Position nicht: A sieht 2 frei (5 minus 3 von B) – mit wie ohne Haken."""
        url = reverse("verkauf:beleg", args=[self.a.pk])
        self.assertEqual(self.client.get(url).context["positionen"][0].knapp, D("2"))
        self.haken(self.a, self.pa, False)
        self.assertEqual(self.client.get(url).context["positionen"][0].knapp, D("2"))
        self.assertEqual(self.a.uebernahme_engpaesse()[0]["verfuegbar"], D("2"))

    def test_bestellung_b_ist_nach_freigabe_nicht_mehr_knapp(self):
        url = reverse("verkauf:beleg", args=[self.b.pk])
        self.assertTrue(self.client.get(url).context["positionen"][0].fehlt)
        self.haken(self.a, self.pa, False)
        self.assertFalse(self.client.get(url).context["positionen"][0].fehlt)
        self.assertEqual(self.b.uebernahme_engpaesse(), [])

    def test_spalte_r_nur_in_der_bestellung_und_nur_lagerartikel(self):
        re = self.beleg(Belegart.RECHNUNG)
        p = self.pos(re, self.win, menge="1", preis="100")
        r = self.haken(re, p, False, follow=True)
        self.assertContains(r, "nur in einer Bestellung")
        self.assertTrue(Position.objects.get(pk=p.pk).reservieren)
        self.assertNotContains(self.client.get(reverse("verkauf:beleg", args=[re.pk])), ">R</th>")
        frei = Position.objects.create(beleg=self.a, bezeichnung="Arbeitszeit", typ="L", menge=D("1"), einzelpreis=D("50"))
        self.assertEqual(self.haken(self.a, frei, False).status_code, 404)

    def test_fremder_mandant_kann_den_haken_nicht_aendern(self):
        s = self.client.session
        s["mandant_id"] = Mandant.objects.create(kurzname="ZONE", firmenname="ZONE Handel").pk
        s.save()
        self.assertEqual(self.haken(self.a, self.pa, False).status_code, 404)
        self.assertTrue(Position.objects.get(pk=self.pa.pk).reservieren)

    def test_aenderung_steht_im_protokoll(self):
        vorher = AenderungsProtokoll.objects.filter(datensatz_id=str(self.pa.pk), aktion="aendern").count()
        self.haken(self.a, self.pa, False)
        self.assertEqual(AenderungsProtokoll.objects.filter(datensatz_id=str(self.pa.pk), aktion="aendern").count(), vorher + 1)

    def test_proforma_uebernimmt_den_haken(self):
        self.haken(self.a, self.pa, False)
        proforma = self.a.kopieren_als(Belegart.PROFORMA, vorlaeufig=True)
        self.assertFalse(proforma.positionen.get().reservieren)
        self.assertEqual(reserviert(self.m, self.win), D("3"))

    def ziel(self, art):
        return Beleg.objects.create(mandant=self.m, art=art, kunde=self.kunde, vorgaenger=self.a)

    def test_teilmenge_in_die_proforma_behaelt_den_haken(self):
        self.haken(self.a, self.pa, False)
        proforma = self.ziel(Belegart.PROFORMA)
        uebernahme.menge_verschieben(self.a, proforma, {self.pa.pk: D("4")})
        self.assertFalse(proforma.positionen.get().reservieren)
        self.assertEqual(reserviert(self.m, self.win), D("3"))  # nur Kunde B

    def test_rueckbuchung_in_die_bestellung_behaelt_den_haken(self):
        self.haken(self.a, self.pa, False)
        ls = self.ziel(Belegart.LIEFERSCHEIN)
        uebernahme.menge_verschieben(self.a, ls, {self.pa.pk: D("10")})  # ganze Menge: die Bestellung hat keine Position mehr
        uebernahme.zurueckbuchen(ls.positionen.get(), D("10"))
        self.assertEqual([(p.menge, p.reservieren) for p in self.a.positionen.all()], [(D("10.000"), False)])
        self.assertEqual(reserviert(self.m, self.win), D("3"))

    def test_kopie_als_neue_bestellung_startet_mit_haken(self):
        self.haken(self.a, self.pa, False)
        kopie = self.a.kopieren_als(Belegart.AUFTRAG)
        self.assertTrue(kopie.positionen.get().reservieren)
