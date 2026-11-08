"""Belegübernahme bei zu wenig Bestand: komplett reservieren (A) oder Teillieferung mit Rückstand (B). Beispiel: 10 bestellt, 5 auf Lager."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.kern.models import Mandant
from apps.lager.models import Bewegungsart, bestand, reserviert
from apps.lager.services import buche_manuell
from apps.stammdaten.models import Artikel, Kunde, Steuersatz
from apps.verkauf.models import Beleg, BelegUebernahme, Belegart, Position

D = Decimal


@override_settings(SECURE_SSL_REDIRECT=False)
class UebernahmeBestandTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.m2 = Mandant.objects.create(kurzname="ZONE", firmenname="ZONE Handel")
        self.kunde = Kunde.objects.create(nummer="1", name1="Kunde")
        st = Steuersatz.objects.create(bezeichnung="19", prozent=D("19"))
        self.a = Artikel.objects.create(nummer="A1", bezeichnung="Maus", steuersatz=st, vk1=D("10"))
        self.b = Artikel.objects.create(nummer="A2", bezeichnung="Tastatur", steuersatz=st, vk1=D("20"))
        buche_manuell(self.m, self.a, Bewegungsart.WARENEINGANG, D("5"))
        buche_manuell(self.m2, self.a, Bewegungsart.WARENEINGANG, D("100"))  # anderer Mandant darf nichts ändern
        self.client.force_login(get_user_model().objects.create_user("t", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()
        self.ab = Beleg.objects.create(mandant=self.m, art=Belegart.AUFTRAG, kunde=self.kunde)
        self.pos = Position.objects.create(beleg=self.ab, artikel=self.a, bezeichnung="Maus", menge=D("10"), einzelpreis=D("10"))

    def uebernehmen(self, **daten):
        return self.client.post(reverse("verkauf:beleg_folgebeleg", args=[self.ab.pk]), {"von_art": "AB", **daten})

    def test_ohne_engpass_keine_rueckfrage(self):
        self.pos.menge = D("5")
        self.pos.save()
        self.assertEqual(self.ab.uebernahme_engpaesse(), [])
        r = self.uebernehmen()
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.ab.folgebelege.get().positionen.get().menge, D("5"))
        self.assertEqual(reserviert(self.m, self.a), D("0"))
        self.ab.refresh_from_db()
        self.assertTrue(self.ab.abgeschlossen)  # alles übernommen: die Bestellung ist erledigt (leer, mit Verlauf)
        self.assertFalse(self.ab.positionen.exists())

    def test_engpass_fragt_nach_und_aendert_nichts(self):
        r = self.uebernehmen()  # Abbruch = keine Entscheidung: nur die Rückfrage, nichts passiert
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Komplett reservieren")
        self.assertContains(r, "Teillieferung")
        e = r.context["engpaesse"][0]
        self.assertEqual((e["bestellt"], e["bestand"], e["verfuegbar"], e["reserviert"]), (D("10"), D("5"), D("5"), D("0")))
        self.assertFalse(self.ab.folgebelege.exists())
        self.assertEqual(reserviert(self.m, self.a), D("10"))

    def test_option_a_komplett_reservieren(self):
        r = self.uebernehmen(**{f"wahl_{self.pos.pk}": "A"})
        self.assertEqual(r.status_code, 302)  # einzige Position bleibt reserviert: zurück zur Bestellung, nichts übernommen
        self.assertFalse(self.ab.folgebelege.exists())
        self.assertEqual(reserviert(self.m, self.a), D("10"))
        self.assertEqual(self.pos.offen_menge, D("10"))

    def test_option_a_mit_zweiter_position_uebernimmt_nur_die_andere(self):
        Position.objects.create(beleg=self.ab, artikel=self.b, bezeichnung="Tastatur", menge=D("1"), einzelpreis=D("20"))
        buche_manuell(self.m, self.b, Bewegungsart.WARENEINGANG, D("3"))
        self.uebernehmen(**{f"wahl_{self.pos.pk}": "A"})
        ls = self.ab.folgebelege.get()
        self.assertEqual([p.artikel for p in ls.positionen.all()], [self.b])
        self.assertEqual(reserviert(self.m, self.a), D("10"))

    def test_option_b_teillieferung_mit_rueckstand(self):
        r = self.uebernehmen(**{f"wahl_{self.pos.pk}": "B", f"menge_{self.pos.pk}": "5"})
        self.assertEqual(r.status_code, 302)
        ls = self.ab.folgebelege.get()
        self.assertEqual(ls.positionen.get().menge, D("5"))
        self.pos.refresh_from_db()
        self.assertEqual((self.pos.menge, self.pos.offen_menge), (D("5"), D("5")))  # die Bestellung behält nur den Rest
        self.assertEqual(reserviert(self.m, self.a), D("5"))  # Rückstand bleibt reserviert
        seite = self.client.get(reverse("verkauf:beleg", args=[self.ab.pk]))
        self.assertContains(seite, f"5 übernommen in Lieferschein {ls.nummer}, 5 offen")
        self.assertContains(seite, "Verlauf der Übernahmen")
        ls.abschliessen()
        self.assertEqual(bestand(self.m, self.a), D("0"))

    def test_option_b_menge_ueber_verfuegbar_wird_abgelehnt(self):
        r = self.uebernehmen(**{f"wahl_{self.pos.pk}": "B", f"menge_{self.pos.pk}": "6"})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(self.ab.folgebelege.exists())

    def test_wareneingang_reserviert_rueckstand_und_zweite_teillieferung(self):
        self.uebernehmen(**{f"wahl_{self.pos.pk}": "B", f"menge_{self.pos.pk}": "5"})
        self.ab.folgebelege.get().abschliessen()
        buche_manuell(self.m, self.a, Bewegungsart.WARENEINGANG, D("3"))
        self.assertEqual(bestand(self.m, self.a) - reserviert(self.m, self.a), D("-2"))  # Rückstand 5 reserviert, 3 da
        engpass = self.ab.uebernahme_engpaesse()[0]
        self.assertEqual((engpass["bestellt"], engpass["verfuegbar"]), (D("5"), D("3")))
        buche_manuell(self.m, self.a, Bewegungsart.WARENEINGANG, D("2"))
        self.assertEqual(self.ab.uebernahme_engpaesse(), [])  # jetzt reicht der Bestand für den Rest
        self.uebernehmen()
        zweiter = self.ab.folgebelege.order_by("-pk").first()
        self.assertEqual(zweiter.positionen.get().menge, D("5"))
        self.assertEqual(reserviert(self.m, self.a), D("0"))
        with self.assertRaises(ValueError):  # nichts mehr offen
            self.ab.folgebeleg_erzeugen()

    def test_zweite_teillieferung_wieder_teilweise(self):
        self.uebernehmen(**{f"wahl_{self.pos.pk}": "B", f"menge_{self.pos.pk}": "5"})
        self.ab.folgebelege.get().abschliessen()
        buche_manuell(self.m, self.a, Bewegungsart.WARENEINGANG, D("2"))
        r = self.uebernehmen(**{f"wahl_{self.pos.pk}": "B", f"menge_{self.pos.pk}": "2"})
        self.assertEqual(r.status_code, 302)
        self.pos.refresh_from_db()
        self.assertEqual((self.pos.menge, self.pos.offen_menge), (D("3"), D("3")))  # 10 bestellt, 5 + 2 übernommen
        self.assertEqual(sum(e.menge for e in BelegUebernahme.objects.filter(quelle=self.ab)), D("7"))
        self.assertEqual(reserviert(self.m, self.a), D("3"))

    def test_rechnung_per_kopieren_fragt_ebenfalls(self):
        url = reverse("verkauf:beleg_kopieren", args=[self.ab.pk])
        r = self.client.post(url, {"art": Belegart.RECHNUNG})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Komplett reservieren")
        self.assertFalse(self.ab.folgebelege.exists())
        r = self.client.post(url, {"art": Belegart.RECHNUNG, f"wahl_{self.pos.pk}": "B", f"menge_{self.pos.pk}": "4"})
        self.assertEqual(r.status_code, 302)
        re = self.ab.folgebelege.get()
        self.assertEqual((re.art, re.positionen.get().menge), (Belegart.RECHNUNG, D("4")))

    def test_mehrere_positionen_fragen_je_betroffener_position(self):
        p2 = Position.objects.create(beleg=self.ab, artikel=self.b, bezeichnung="Tastatur", menge=D("4"), einzelpreis=D("20"))
        buche_manuell(self.m, self.b, Bewegungsart.WARENEINGANG, D("1"))
        self.assertEqual({e["position"].pk for e in self.ab.uebernahme_engpaesse()}, {self.pos.pk, p2.pk})
        r = self.uebernehmen(**{f"wahl_{self.pos.pk}": "A"})  # zweite Entscheidung fehlt -> weiter Rückfrage
        self.assertEqual(r.status_code, 200)
        self.assertFalse(self.ab.folgebelege.exists())

    def test_bestand_strikt_pro_mandant(self):
        # Mandant ZONE hat 100 Stück – für OKAY zählt nur der eigene Bestand
        self.assertEqual(self.ab.uebernahme_engpaesse()[0]["verfuegbar"], D("5"))
        andere = Beleg.objects.create(mandant=self.m2, art=Belegart.AUFTRAG, kunde=self.kunde)
        Position.objects.create(beleg=andere, artikel=self.a, bezeichnung="Maus", menge=D("50"), einzelpreis=D("10"))
        self.assertEqual(reserviert(self.m, self.a), D("10"))
        self.assertEqual(reserviert(self.m2, self.a), D("50"))
        self.assertEqual(self.ab.uebernahme_engpaesse()[0]["verfuegbar"], D("5"))
        self.assertEqual(andere.uebernahme_engpaesse(), [])

    def test_fremde_reservierung_verringert_verfuegbar(self):
        andere = Beleg.objects.create(mandant=self.m, art=Belegart.AUFTRAG, kunde=self.kunde)
        Position.objects.create(beleg=andere, artikel=self.a, bezeichnung="Maus", menge=D("2"), einzelpreis=D("10"))
        e = self.ab.uebernahme_engpaesse()[0]
        self.assertEqual((e["verfuegbar"], e["reserviert"]), (D("3"), D("2")))
