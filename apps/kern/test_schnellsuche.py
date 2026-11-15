"""Schnellbedienung (Strg+K): Suche über Kunden, Artikel, Belege, Lieferanten, Bestellungen und Werkstattaufträge."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.einkauf.models import Bestellung
from apps.kern.models import Mandant
from apps.stammdaten.models import Artikel, Kunde, Lieferant, Steuersatz
from apps.verkauf.models import Beleg, BelegUmwandlung, Belegart
from apps.werkstatt.models import Reparatur


@override_settings(SECURE_SSL_REDIRECT=False)
class SchnellsucheTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.m2 = Mandant.objects.create(kurzname="ZONE", firmenname="ZONE Handel")
        self.user = get_user_model().objects.create_user("anna", password="x" * 14)
        self.client.force_login(self.user)
        self.waehle(self.m)
        self.kunde = Kunde.objects.create(nummer="10001", name1="Müller Computer GmbH", ort="Hamburg")
        self.artikel = Artikel.objects.create(nummer="M1", bezeichnung="Funkmaus", ean="4012345678901",
                                              steuersatz=Steuersatz.objects.get(nummer=1), vk1=Decimal("20"))
        self.lieferant = Lieferant.objects.create(nummer="70272", name1="API Distribution GmbH", ort="Köln")
        self.beleg = Beleg.objects.create(mandant=self.m, art=Belegart.RECHNUNG, kunde=self.kunde, nummer="R-2026-0001")

    def waehle(self, mandant):
        sitzung = self.client.session
        sitzung["mandant_id"] = mandant.pk
        sitzung.save()

    def suche(self, q):
        antwort = self.client.get(reverse("schnellsuche"), {"q": q})
        self.assertEqual(antwort.status_code, 200)
        return antwort.json()["treffer"]

    def gruppen(self, treffer, gruppe):
        return [t for t in treffer if t["gruppe"] == gruppe]

    # --- Zugriff ---
    def test_ohne_anmeldung_kein_zugriff(self):
        self.client.logout()
        antwort = self.client.get(reverse("schnellsuche"), {"q": "Müller"})
        self.assertEqual(antwort.status_code, 302)
        self.assertIn("login", antwort["Location"])

    def test_ohne_gewaehlten_mandanten_keine_treffer(self):
        sitzung = self.client.session
        del sitzung["mandant_id"]
        sitzung.save()
        self.client.cookies.clear()
        antwort = self.client.get(reverse("schnellsuche"), {"q": "Müller"})
        self.assertEqual(antwort.status_code, 302)  # zwei aktive Mandanten, keiner gewählt: zurück zur Startseite

    # --- Treffer je Bereich ---
    def test_kunde_nach_name_nummer_und_ort(self):
        for q in ("müller", "10001", "hamburg", "computer müller"):
            treffer = self.gruppen(self.suche(q), "Kunde")
            self.assertEqual(len(treffer), 1, q)
        t = self.gruppen(self.suche("10001"), "Kunde")[0]
        self.assertEqual(t["url"], reverse("verkauf:kunde", args=[self.kunde.pk]))
        self.assertIn("Müller", t["titel"])
        self.assertEqual(t["zusatz"], "Hamburg")

    def test_mehrere_woerter_muessen_alle_vorkommen(self):
        self.assertEqual(self.gruppen(self.suche("müller berlin"), "Kunde"), [])

    def test_artikel_nach_nummer_bezeichnung_und_ean(self):
        for q in ("M1", "funkmaus", "4012345678901"):
            treffer = self.gruppen(self.suche(q), "Artikel")
            self.assertEqual(len(treffer), 1, q)
        self.assertEqual(treffer[0]["url"], reverse("lager:artikel", args=[self.artikel.pk]))

    def test_beleg_nach_nummer_und_alter_nummer(self):
        BelegUmwandlung.objects.create(beleg=self.beleg, von_art=Belegart.ANGEBOT, von_nummer="A-2026-0007",
                                       von_belegdatum="2026-01-02", nach_art=Belegart.RECHNUNG)
        for q in ("R-2026-0001", "A-2026-0007"):
            treffer = self.gruppen(self.suche(q), "Beleg")
            self.assertEqual(len(treffer), 1, q)
        self.assertEqual(treffer[0]["url"], reverse("verkauf:beleg", args=[self.beleg.pk]))
        self.assertIn("Müller", treffer[0]["zusatz"])

    def test_lieferant_nach_name_und_nummer(self):
        for q in ("distribution", "70272"):
            treffer = self.gruppen(self.suche(q), "Lieferant")
            self.assertEqual(len(treffer), 1, q)
        self.assertEqual(treffer[0]["url"], reverse("einkauf:lieferant", args=[self.lieferant.pk]))

    def test_bestellung_und_werkstattauftrag(self):
        b = Bestellung.objects.create(mandant=self.m, lieferant=self.lieferant, nummer="B-2026-0042")
        r = Reparatur.objects.create(mandant=self.m, nummer="W-2026-0003", kunde=self.kunde, geraet="Notebook", fehler="Display")
        t = self.gruppen(self.suche("B-2026-0042"), "Bestellung")
        self.assertEqual(t[0]["url"], reverse("einkauf:bestellung", args=[b.pk]))
        t = self.gruppen(self.suche("W-2026-0003"), "Werkstatt")
        self.assertEqual(t[0]["url"], reverse("werkstatt:detail", args=[r.pk]))

    # --- Fachregeln ---
    def test_mandantentrennung(self):
        fremd = Beleg.objects.create(mandant=self.m2, art=Belegart.RECHNUNG, kunde=self.kunde, nummer="R-2026-0099")
        Bestellung.objects.create(mandant=self.m2, lieferant=self.lieferant, nummer="B-2026-0099")
        Reparatur.objects.create(mandant=self.m2, nummer="W-2026-0099", kunde=self.kunde, geraet="PC", fehler="Lüfter")
        self.assertEqual(self.suche("2026-0099"), [])
        self.waehle(self.m2)
        treffer = self.suche("2026-0099")
        self.assertEqual({t["gruppe"] for t in treffer}, {"Beleg", "Bestellung", "Werkstatt"})
        self.assertEqual(self.gruppen(treffer, "Beleg")[0]["url"], reverse("verkauf:beleg", args=[fremd.pk]))

    def test_anonymisierter_kunde_erscheint_nicht(self):
        Kunde.objects.create(nummer="10002", name1="Müller Anonym", anonymisiert=True)
        treffer = self.gruppen(self.suche("müller"), "Kunde")
        self.assertEqual([t["titel"].startswith("10001") for t in treffer], [True])

    def test_unter_zwei_zeichen_leer(self):
        self.assertEqual(self.suche("M"), [])
        self.assertEqual(self.suche(""), [])
        self.assertEqual(self.suche("  x "), [])

    def test_begrenzung_je_gruppe(self):
        for i in range(9):
            Kunde.objects.create(nummer=f"2000{i}", name1=f"Serienkunde {i}")
            Artikel.objects.create(nummer=f"S{i}", bezeichnung=f"Serienartikel {i}", steuersatz=Steuersatz.objects.get(nummer=1))
        treffer = self.suche("serien")
        self.assertEqual(len(self.gruppen(treffer, "Kunde")), 6)
        self.assertEqual(len(self.gruppen(treffer, "Artikel")), 6)

    def test_exakter_nummerntreffer_zuerst(self):
        Kunde.objects.create(nummer="A-Z5", name1="Anderer")
        Kunde.objects.create(nummer="Z5", name1="Exakter")
        self.assertEqual(self.gruppen(self.suche("Z5"), "Kunde")[0]["titel"], "Z5 – Exakter")

    def test_sonderzeichen_stoeren_nicht(self):
        self.assertEqual(self.suche("%"), [])
        self.assertEqual(self.suche("a_"), [])
        self.assertEqual(self.suche("'\"; --"), [])

    def test_keine_preise_oder_bankdaten_in_der_antwort(self):
        Kunde.objects.filter(pk=self.kunde.pk).update(iban="DE02120300000000202051")
        inhalt = self.client.get(reverse("schnellsuche"), {"q": "m"}).content.decode() + str(self.suche("müller")) + str(self.suche("M1"))
        self.assertNotIn("DE02120300000000202051", inhalt)
        self.assertNotIn("20.00", inhalt)

    def test_normaler_mitarbeiter_sieht_die_bereiche(self):
        """Das Rechtesystem kennt nur Admin/Mitarbeiter für Änderungen; Lesen steht allen offen, also auch hier alle Gruppen."""
        self.assertFalse(self.user.is_staff)
        self.assertTrue(self.suche("Müller"))

    # --- Einbindung ---
    def test_palette_ist_auf_seiten_eingebunden(self):
        html = self.client.get(reverse("dashboard"), follow=True).content.decode()
        self.assertEqual(html.count('id="palette"'), 1)
        self.assertIn(reverse("schnellsuche"), html)
        self.assertIn("window.wwokPalette", html)

    def test_palette_nicht_auf_der_anmeldeseite(self):
        self.client.logout()
        self.assertNotContains(self.client.get(reverse("login")), 'id="palette"')
