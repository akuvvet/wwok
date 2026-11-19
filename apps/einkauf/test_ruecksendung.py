"""WE-Rücksendung (Wunsch des Nutzers 18.11.2026): gebuchte Ware aus einem Wareneingang geht zur Gutschrift oder zum
Austausch an den Lieferanten zurück. Bestand wird abgebucht, die Bestellung bleibt unverändert."""
from datetime import date
from decimal import Decimal as D

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.kern.models import Mandant
from apps.lager.models import Bewegungsart, Lagerbewegung, Seriennummer, bestand
from apps.lager.services import buche_manuell
from apps.stammdaten.models import Artikel, ArtikelLieferant, Lieferant, Steuersatz

from . import services
from .models import BestellStatus, Ruecksendung, RuecksendungStatus, Wareneingang


class RuecksendungTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.lf = Lieferant.objects.create(nummer="L1", name1="Techdata GmbH", kurzname="TD")
        st = Steuersatz.objects.get(nummer=1)
        self.maus = Artikel.objects.create(nummer="M1", bezeichnung="Maus", steuersatz=st, vk1=D("15"))
        self.nt = Artikel.objects.create(nummer="NT1", bezeichnung="Netzteil", steuersatz=st, vk1=D("60"))
        ArtikelLieferant.objects.create(artikel=self.maus, lieferant=self.lf, bestellnummer="TD-1", ek=D("9"))
        self.user = get_user_model().objects.create_superuser("chef", password="x" * 14)

    def anmelden(self, mandant=None):
        self.client.force_login(self.user)
        s = self.client.session
        s["mandant_id"] = (mandant or self.m).pk
        s.save()

    def ueberlieferung(self):
        """Beispiel des Nutzers: 5 bestellt, 10 geliefert und gebucht."""
        b = services.bestellung_anlegen(self.m, self.lf)
        services.bestellposition(b, self.maus, D("5"))
        b.status = BestellStatus.BESTELLT
        b.save()
        we = services.wareneingang_anlegen(self.m, self.lf, b)
        services.wareneingang_aus_bestellung(we)
        p = we.positionen.get()
        p.menge, p.ek = D("10"), D("9")
        p.save()
        services.wareneingang_buchen(we, bestaetigt=True)
        return b, we, p

    def mit_seriennummern(self):
        we = services.wareneingang_anlegen(self.m, self.lf)
        p = services.wareneingang_erfassen(we, self.nt, D("3"), D("40"), "S1\nS2\nS3")
        services.wareneingang_buchen(we, bestaetigt=True)
        return we, p

    # ---- Anlegen ----------------------------------------------------------------------------
    def test_beispiel_5_von_10_zurueck_zur_gutschrift(self):
        b, we, p = self.ueberlieferung()
        self.assertEqual(bestand(self.m, self.maus), D("10"))
        r = services.ruecksendung_anlegen(we, [(p, D("5"), [])], "gutschrift", "10 statt 5 geliefert")
        self.assertTrue(r.nummer.startswith("WR"))
        self.assertEqual((r.status, r.lieferant, r.summe_netto), (RuecksendungStatus.ANGELEGT, self.lf, D("45.00")))
        self.assertEqual(bestand(self.m, self.maus), D("5"))
        bew = Lagerbewegung.objects.get(art=Bewegungsart.RUECKSENDUNG)
        self.assertEqual((bew.menge, bew.bemerkung), (D("-5"), f"{r.nummer} an Techdata GmbH"))
        b.refresh_from_db()
        self.assertEqual((b.status, b.positionen.get().geliefert), (BestellStatus.GELIEFERT, D("10")))  # Bestellung unverändert
        we.refresh_from_db()
        self.assertEqual(we.positionen.get().menge, D("10"))  # Wareneingang (und Rechnungsabgleich) unverändert

    def test_hoechstens_was_noch_zurueckgehen_kann(self):
        _, we, p = self.ueberlieferung()
        services.ruecksendung_anlegen(we, [(p, D("8"), [])], "gutschrift", "zu viel")
        with self.assertRaisesMessage(ValueError, "Höchstens 2 können noch zurückgehen"):
            services.ruecksendung_anlegen(we, [(p, D("3"), [])], "gutschrift", "zu viel")
        self.assertEqual(services.ruecksendbar(p), D("2"))

    def test_bestand_faellt_nie_unter_null(self):
        _, we, p = self.ueberlieferung()
        buche_manuell(self.m, self.maus, Bewegungsart.EIGENVERBRAUCH, D("-8"))
        with self.assertRaisesMessage(ValueError, "Lagerbestand reicht nicht"):
            services.ruecksendung_anlegen(we, [(p, D("5"), [])], "gutschrift", "zu viel")
        self.assertFalse(Ruecksendung.objects.exists())  # nichts halb angelegt

    def test_pflichtangaben(self):
        _, we, p = self.ueberlieferung()
        with self.assertRaisesMessage(ValueError, "Grund"):
            services.ruecksendung_anlegen(we, [(p, D("1"), [])], "gutschrift", "  ")
        with self.assertRaisesMessage(ValueError, "Gutschrift oder zum Austausch"):
            services.ruecksendung_anlegen(we, [(p, D("1"), [])], "", "x")
        with self.assertRaisesMessage(ValueError, "mindestens einer Position"):
            services.ruecksendung_anlegen(we, [(p, None, [])], "gutschrift", "x")
        with self.assertRaisesMessage(ValueError, "größer als 0"):
            services.ruecksendung_anlegen(we, [(p, D("-1"), [])], "gutschrift", "x")

    def test_nur_aus_gebuchtem_wareneingang(self):
        we = services.wareneingang_anlegen(self.m, self.lf)
        p = services.wareneingang_erfassen(we, self.maus, D("2"), D("9"))
        with self.assertRaisesMessage(ValueError, "gebuchten Wareneingang"):
            services.ruecksendung_anlegen(we, [(p, D("1"), [])], "gutschrift", "x")

    def test_seriennummern_genau_waehlen_und_status(self):
        we, p = self.mit_seriennummern()
        self.assertEqual(services.seriennummern_im_lager(p), ["S1", "S2", "S3"])
        with self.assertRaisesMessage(ValueError, "genau 2 Seriennummer(n)"):
            services.ruecksendung_anlegen(we, [(p, D("2"), ["S1"])], "austausch", "falsche Ware")
        r = services.ruecksendung_anlegen(we, [(p, D("2"), ["S1", "S3"])], "austausch", "falsche Ware")
        self.assertEqual(sorted(Seriennummer.objects.filter(status=Seriennummer.ZURUECK).values_list("nummer", flat=True)), ["S1", "S3"])
        self.assertEqual(services.seriennummern_im_lager(p), ["S2"])
        self.assertEqual(r.positionen.get().seriennummern, "S1\nS3")
        with self.assertRaisesMessage(ValueError, "S1 ist nicht (mehr) im Lager"):
            services.ruecksendung_anlegen(we, [(p, D("1"), ["S1"])], "austausch", "x")

    # ---- Status ------------------------------------------------------------------------------
    def test_versendet_und_gutschrift(self):
        _, we, p = self.ueberlieferung()
        r = services.ruecksendung_anlegen(we, [(p, D("5"), [])], "gutschrift", "zu viel")
        r = services.ruecksendung_versendet(r)
        self.assertEqual(r.status, RuecksendungStatus.VERSENDET)
        with self.assertRaisesMessage(ValueError, "Netto"):
            services.ruecksendung_gutschrift(r, "G-1", date.today(), None, D("8.45"), D("52.95"))
        r = services.ruecksendung_gutschrift(r, "G-1", date.today(), D("44.5"), D("8.46"), D("52.96"))
        self.assertEqual((r.status, r.gutschrift_betrag), (RuecksendungStatus.GUTSCHRIFT, D("44.50")))
        self.assertIn("Gutschrift G-1 erhalten: 44.50 € netto, 52.96 € brutto (erwartet 45.00 € netto)", r.historie)
        with self.assertRaisesMessage(ValueError, "schon erledigt"):
            services.ruecksendung_versendet(r)

    def test_austausch_legt_ersatz_wareneingang_an(self):
        we, p = self.mit_seriennummern()
        r = services.ruecksendung_anlegen(we, [(p, D("1"), ["S2"])], "austausch", "falsches Modell")
        ersatz = services.ruecksendung_ersatz(r)
        r.refresh_from_db()
        self.assertEqual((r.status, r.ersatz_wareneingang), (RuecksendungStatus.ERSATZ, ersatz))
        self.assertFalse(ersatz.gebucht)
        self.assertEqual(ersatz.lieferschein, f"Ersatz zu {r.nummer}")
        pos = ersatz.positionen.get()
        self.assertEqual((pos.artikel, pos.menge, pos.ek, pos.seriennummern), (self.nt, D("1"), D("40.0000"), ""))
        self.assertIsNone(ersatz.bestellung)
        self.assertEqual(bestand(self.m, self.nt), D("2"))  # erst mit dem Buchen des Ersatzes wieder 3
        pos.seriennummern = "S9"
        pos.save()
        services.wareneingang_buchen(ersatz, bestaetigt=True)
        self.assertEqual(bestand(self.m, self.nt), D("3"))

    def test_abgelehnt_bucht_zurueck_ins_lager(self):
        we, p = self.mit_seriennummern()
        r = services.ruecksendung_anlegen(we, [(p, D("2"), ["S1", "S2"])], "gutschrift", "zu viel")
        self.assertEqual(bestand(self.m, self.nt), D("1"))
        with self.assertRaisesMessage(ValueError, "warum"):
            services.ruecksendung_abgelehnt(r, "")
        services.ruecksendung_abgelehnt(r, "Lieferant nimmt nicht zurück")
        self.assertEqual(bestand(self.m, self.nt), D("3"))
        self.assertFalse(Seriennummer.objects.exclude(status=Seriennummer.IM_LAGER).exists())
        self.assertEqual(services.ruecksendbar(p), D("3"))  # abgelehnte zählen nicht

    # ---- Seiten ------------------------------------------------------------------------------
    def test_ablauf_ueber_die_seiten(self):
        _, we, p = self.ueberlieferung()
        self.anmelden()
        self.assertContains(self.client.get(reverse("einkauf:wareneingang", args=[we.pk])), reverse("einkauf:ruecksendung_neu", args=[we.pk]))
        seite = self.client.get(reverse("einkauf:ruecksendung_neu", args=[we.pk]))
        self.assertContains(seite, f'name="menge_{p.pk}"')
        r = self.client.post(reverse("einkauf:ruecksendung_neu", args=[we.pk]),
                             {f"menge_{p.pk}": "5", "art": "gutschrift", "grund": "10 statt 5"}, follow=True)
        rs = Ruecksendung.objects.get()
        self.assertEqual(r.redirect_chain[-1][0], reverse("einkauf:ruecksendung", args=[rs.pk]))
        self.assertContains(r, "45,00")
        self.client.post(reverse("einkauf:ruecksendung", args=[rs.pk]), {"aktion": "versendet"})
        self.client.post(reverse("einkauf:ruecksendung", args=[rs.pk]), {
            "aktion": "gutschrift", "nummer": "G-77", "datum": date.today().isoformat(), "netto": "45,00", "steuer": "8,55", "brutto": "53,55"})
        rs.refresh_from_db()
        self.assertEqual((rs.status, rs.gutschrift_betrag), (RuecksendungStatus.GUTSCHRIFT, D("45.00")))
        liste = self.client.get(reverse("einkauf:ruecksendungen"), {"filter": "alle"})
        self.assertContains(liste, rs.nummer)
        self.assertNotContains(self.client.get(reverse("einkauf:ruecksendungen")), rs.nummer)  # nur offene
        konto = self.client.get(reverse("einkauf:lieferant", args=[self.lf.pk]), {"art": "WR"})
        self.assertContains(konto, rs.nummer)

    def test_fehler_zeigt_formular_mit_eingaben_wieder(self):
        _, we, p = self.ueberlieferung()
        self.anmelden()
        r = self.client.post(reverse("einkauf:ruecksendung_neu", args=[we.pk]), {f"menge_{p.pk}": "11", "art": "gutschrift", "grund": "x"})
        self.assertContains(r, "Höchstens 10 können noch zurückgehen")
        self.assertContains(r, 'value="11"')
        self.assertFalse(Ruecksendung.objects.exists())

    def test_fremder_mandant_sieht_nichts(self):
        _, we, p = self.ueberlieferung()
        rs = services.ruecksendung_anlegen(we, [(p, D("1"), [])], "gutschrift", "x")
        zone = Mandant.objects.create(kurzname="ZONE", firmenname="ZONE Handel")
        self.anmelden(zone)
        self.assertEqual(self.client.get(reverse("einkauf:ruecksendung", args=[rs.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("einkauf:ruecksendung_neu", args=[we.pk])).status_code, 404)

    def test_ersatz_wareneingang_nicht_zur_rechnung_anbieten(self):
        we, p = self.mit_seriennummern()
        r = services.ruecksendung_anlegen(we, [(p, D("1"), ["S1"])], "austausch", "falsch")
        ersatz = services.ruecksendung_ersatz(r)
        services.wareneingang_buchen(ersatz, bestaetigt=True)
        from .models import Lieferantenrechnung
        from django.utils import timezone
        lr = Lieferantenrechnung.objects.create(mandant=self.m, lieferant=self.lf, rechnungsnummer="R1", datum=timezone.localdate(),
                                                faellig=timezone.localdate(), netto=D("120"), brutto=D("142.80"), steuer=D("22.80"))
        self.anmelden()
        kandidaten = list(self.client.get(reverse("einkauf:rechnung", args=[lr.pk])).context["kandidaten"])
        self.assertIn(we, kandidaten)
        self.assertNotIn(Wareneingang.objects.get(pk=ersatz.pk), kandidaten)

    # ---- Menü unter „Lieferant“ ----------------------------------------------------------------
    def test_kein_untermenue_in_der_seitenleiste_knoepfe_auf_der_lieferantenseite(self):
        """Wunsch des Nutzers 19.11.2026: kein Untermenü „Neuer Beleg für …“ unter „Lieferant“ in der Seitenleiste – weder auf der
        Lieferantenseite noch in Bestellung, Wareneingang oder Rücksendung. Neue Belege über die Knöpfe unter der Adresse."""
        b, we, _ = self.ueberlieferung()
        b2 = services.bestellung_anlegen(self.m, self.lf)
        services.bestellposition(b2, self.maus, D("1"))
        b2.status = BestellStatus.BESTELLT
        b2.save()
        self.anmelden()
        r = self.client.get(reverse("einkauf:lieferant", args=[self.lf.pk]))
        self.assertContains(r, f'<option value="{b2.pk}">zu {b2.nummer}</option>', count=1)
        self.assertContains(r, 'action="%s"' % reverse("einkauf:bestellung_neu"), count=1)
        self.assertContains(r, 'action="%s"' % reverse("einkauf:wareneingang_neu"), count=1)
        for url in (reverse("einkauf:lieferant", args=[self.lf.pk]), reverse("einkauf:bestellung", args=[b.pk]),
                    reverse("einkauf:wareneingang", args=[we.pk]), reverse("einkauf:ruecksendung_neu", args=[we.pk]),
                    reverse("einkauf:bestellungen")):
            r = self.client.get(url)
            self.assertEqual(r.status_code, 200, url)
            self.assertNotContains(r, "Neuer Beleg für TD")
            self.assertNotContains(r, 'class="untermenue"><small')
        # WE-Rücksendung nicht mehr in der Seitenleiste, sondern als „+ WE-Rücksendung“ beim Lieferanten (Wunsch des Nutzers 19.11.2026)
        self.assertNotContains(self.client.get(reverse("einkauf:bestellungen")), "WE-Rücksendung</span>")
        self.assertContains(self.client.get(reverse("einkauf:lieferant", args=[self.lf.pk])), "+ WE-Rücksendung</button>")

    def test_gesperrter_lieferant_ohne_knoepfe(self):
        self.lf.gesperrt = True
        self.lf.save()
        self.anmelden()
        r = self.client.get(reverse("einkauf:lieferant", args=[self.lf.pk]))
        self.assertContains(r, "Lieferant ist gesperrt.")
        self.assertNotContains(r, 'action="%s"' % reverse("einkauf:bestellung_neu"))
        self.assertNotContains(r, 'action="%s"' % reverse("einkauf:wareneingang_neu"))


class RuecksendungReviewTest(RuecksendungTest):
    """Befunde aus dem Review: doppelte Seriennummern, gelöschter Ersatz, Rechnungszuordnung per POST, Grenzen."""

    def test_doppelte_seriennummer_wird_abgewiesen(self):
        we, p = self.mit_seriennummern()
        with self.assertRaisesMessage(ValueError, "doppelt"):
            services.ruecksendung_anlegen(we, [(p, D("2"), ["S1", "S1"])], "gutschrift", "x")
        self.assertEqual(bestand(self.m, self.nt), D("3"))

    def test_seriennummer_inzwischen_verkauft_rollt_zurueck(self):
        we, p = self.mit_seriennummern()
        echte = services.seriennummern_im_lager

        def veraltet(pos):  # Stand vor dem Verkauf: S1 galt noch als im Lager
            Seriennummer.objects.filter(nummer="S1").update(status=Seriennummer.VERKAUFT)
            return ["S1", "S2", "S3"]
        services.seriennummern_im_lager = veraltet
        try:
            with self.assertRaisesMessage(ValueError, "inzwischen nicht mehr im Lager"):
                services.ruecksendung_anlegen(we, [(p, D("1"), ["S1"])], "gutschrift", "x")
        finally:
            services.seriennummern_im_lager = echte
        self.assertEqual(bestand(self.m, self.nt), D("3"))
        self.assertFalse(Ruecksendung.objects.exists())

    def test_geloeschter_ersatz_oeffnet_die_ruecksendung_wieder(self):
        we, p = self.mit_seriennummern()
        r = services.ruecksendung_anlegen(we, [(p, D("1"), ["S1"])], "austausch", "falsch")
        ersatz = services.ruecksendung_ersatz(r)
        services.wareneingang_loeschen(ersatz)
        r.refresh_from_db()
        self.assertEqual((r.status, r.ersatz_wareneingang, r.erledigt_am), (RuecksendungStatus.VERSENDET, None, None))
        self.assertIn("gelöscht – Rücksendung wieder offen", r.historie)
        services.ruecksendung_gutschrift(r, "G-1", date.today(), D("40"), D("7.60"), D("47.60"))  # jetzt geht es weiter

    def test_ersatz_per_post_nicht_an_rechnung(self):
        we, p = self.mit_seriennummern()
        r = services.ruecksendung_anlegen(we, [(p, D("1"), ["S1"])], "austausch", "falsch")
        ersatz = services.ruecksendung_ersatz(r)
        services.wareneingang_buchen(ersatz, bestaetigt=True)
        from django.utils import timezone

        from .models import Lieferantenrechnung
        lr = Lieferantenrechnung.objects.create(mandant=self.m, lieferant=self.lf, rechnungsnummer="R1", datum=timezone.localdate(),
                                                faellig=timezone.localdate(), netto=D("120"), brutto=D("142.80"), steuer=D("22.80"))
        self.anmelden()
        self.client.post(reverse("einkauf:rechnung", args=[lr.pk]), {"wareneingang": [we.pk, ersatz.pk]})
        self.assertEqual(list(lr.wareneingaenge.values_list("pk", flat=True)), [we.pk])

    def test_gutschrift_zu_gross(self):
        _, we, p = self.ueberlieferung()
        r = services.ruecksendung_anlegen(we, [(p, D("1"), [])], "gutschrift", "x")
        with self.assertRaisesMessage(ValueError, "zu groß"):
            services.ruecksendung_gutschrift(r, "G-1", date.today(), D("10000000000"), D("0"), D("10000000000"))

    def test_abgelehnt_mit_wieder_eingelagerter_nummer_bricht_ab(self):
        we, p = self.mit_seriennummern()
        r = services.ruecksendung_anlegen(we, [(p, D("1"), ["S1"])], "gutschrift", "x")
        Seriennummer.objects.filter(nummer="S1").update(status=Seriennummer.IM_LAGER)
        with self.assertRaisesMessage(ValueError, "wieder im Lager oder verkauft"):
            services.ruecksendung_abgelehnt(r, "nimmt nicht zurück")
        self.assertEqual(bestand(self.m, self.nt), D("2"))
