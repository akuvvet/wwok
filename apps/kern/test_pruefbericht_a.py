"""Prüfbericht 14.11.2026: A5 (Rest nicht mehr lieferbar), A9 (Sperre wiederkehrende Bestellung), A10 (Werkstatt-Positionen),
A11 (ESD-Zuordnung begrenzt), A12 (Inventur gegen den angezeigten Bestand)."""
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.einkauf import services as einkauf
from apps.einkauf.models import BestellStatus
from apps.esd import services as esd
from apps.kern.models import Mandant
from apps.lager.models import Bewegungsart, bestand
from apps.lager.services import buche_manuell
from apps.stammdaten.models import Artikel, Kunde, Lieferant, Steuersatz
from apps.verkauf.models import Abo, AboPosition, Beleg, Belegart, Position
from apps.werkstatt import services as werkstatt


@override_settings(SECURE_SSL_REDIRECT=False)
class PruefberichtATest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.kunde = Kunde.objects.create(nummer="1", name1="Kunde GmbH")
        self.lf = Lieferant.objects.create(nummer="L1", name1="Kosatec Computer GmbH", kurzname="KOSATEC", ort="Braunschweig")
        st = Steuersatz.objects.create(bezeichnung="19", prozent=Decimal("19"))
        self.maus = Artikel.objects.create(nummer="M1", bezeichnung="Maus", steuersatz=st, vk1=Decimal("10"))
        self.kabel = Artikel.objects.create(nummer="K1", bezeichnung="Kabel", steuersatz=st, vk1=Decimal("3"))
        self.win = Artikel.objects.create(nummer="WIN11", bezeichnung="Windows 11", steuersatz=st, vk1=Decimal("120"), esd_key=True)
        buche_manuell(self.m, self.maus, Bewegungsart.WARENEINGANG, Decimal("10"))
        buche_manuell(self.m, self.win, Bewegungsart.WARENEINGANG, Decimal("10"))
        self.client.force_login(get_user_model().objects.create_superuser("chef", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    # ---- A5 ----
    def teilgeliefert(self):
        b = einkauf.bestellung_anlegen(self.m, self.lf)
        p1 = einkauf.bestellposition(b, self.maus, Decimal("5"), ek=Decimal("4"))
        p2 = einkauf.bestellposition(b, self.kabel, Decimal("3"), ek=Decimal("1"))
        einkauf.bestellen(b)
        p1.geliefert = Decimal("2")
        p1.save()
        b.status_aktualisieren()
        self.assertEqual(b.status, BestellStatus.TEIL)
        return b, p1, p2

    def test_a5_rest_nicht_mehr_lieferbar(self):
        b, p1, p2 = self.teilgeliefert()
        self.assertEqual(einkauf.offen_bestellt(self.m, self.maus), Decimal("3"))
        self.assertContains(self.client.get(reverse("einkauf:bestellung", args=[b.pk])), "Rest nicht mehr lieferbar")
        self.client.post(reverse("einkauf:bestellung_rest_schliessen", args=[b.pk]))
        b.refresh_from_db(), p1.refresh_from_db(), p2.refresh_from_db()
        self.assertEqual((b.status, p1.menge, p2.menge), (BestellStatus.GELIEFERT, Decimal("2"), Decimal("0")))
        self.assertIn("Rest nicht mehr lieferbar", b.bemerkung)
        self.assertIn("3 × M1", b.bemerkung)
        self.assertIn("3 × K1", b.bemerkung)
        self.assertEqual(einkauf.offen_bestellt(self.m, self.maus), Decimal("0"))  # Bestellvorschlag rechnet den Rest nicht mehr ein
        self.assertNotContains(self.client.get(reverse("einkauf:bestellung", args=[b.pk])), "bestellung_rest_schliessen")
        with self.assertRaises(ValueError):
            einkauf.rest_schliessen(b)  # nur einmal, nur bei „teilweise geliefert“

    def test_a5_nicht_bei_offenem_wareneingang_oder_ohne_lieferung(self):
        b, _, _ = self.teilgeliefert()
        einkauf.wareneingang_anlegen(self.m, self.lf, b)
        with self.assertRaises(ValueError):
            einkauf.rest_schliessen(b)
        neu = einkauf.bestellung_anlegen(self.m, self.lf)
        einkauf.bestellposition(neu, self.maus, Decimal("1"), ek=Decimal("4"))
        einkauf.bestellen(neu)
        with self.assertRaises(ValueError):
            einkauf.rest_schliessen(neu)  # noch nichts geliefert: stornieren statt abschließen

    # ---- A9 ----
    def test_a9_wiederkehrende_bestellung_nicht_doppelt(self):
        abo = Abo.objects.create(mandant=self.m, kunde=self.kunde, bezeichnung="Wartung", monate=3,
                                 naechster_termin=date(2026, 10, 1), ende=date(2027, 4, 1))
        AboPosition.objects.create(abo=abo, bezeichnung="Wartungspauschale", menge=1, einzelpreis=Decimal("100"))
        zweiter_klick = Abo.objects.get(pk=abo.pk)  # hat noch den alten Termin geladen
        self.assertIsNotNone(abo.rechnung_erzeugen())
        with self.assertRaises(ValueError):
            zweiter_klick.rechnung_erzeugen()
        self.assertEqual(Beleg.objects.filter(abo=abo).count(), 1)

    # ---- A10 ----
    def test_a10_werkstatt_positionen_gesperrt_und_nur_positive_menge(self):
        r = werkstatt.annehmen(self.m, self.kunde, "Notebook", "startet nicht")
        with self.assertRaises(ValueError):
            werkstatt.position_hinzufuegen(r, "T", "Maus", Decimal("-1"), Decimal("10"), self.maus)
        with self.assertRaises(ValueError):
            werkstatt.position_hinzufuegen(r, "T", "Maus", Decimal("0"), Decimal("10"), self.maus)
        werkstatt.position_hinzufuegen(r, "T", "Maus", Decimal("1"), Decimal("10"), self.maus)
        werkstatt.rechnung_erzeugen(r)
        with self.assertRaises(ValueError):
            werkstatt.position_hinzufuegen(r, "T", "Maus", Decimal("1"), Decimal("10"), self.maus)
        self.client.post(reverse("werkstatt:position", args=[r.pk]), {"art": "T", "artikel": "M1", "menge": "1", "preis": "10"})
        self.assertEqual(r.positionen.count(), 1)
        g = werkstatt.annehmen(self.m, self.kunde, "PC", "defekt", garantie=True)
        werkstatt.position_hinzufuegen(g, "T", "Maus", Decimal("1"), Decimal("0"), self.maus)
        werkstatt.garantieteile_buchen(g)
        with self.assertRaises(ValueError):
            werkstatt.position_hinzufuegen(g, "T", "Maus", Decimal("1"), Decimal("0"), self.maus)
        self.assertEqual(bestand(self.m, self.maus), Decimal("9"))

    # ---- A11 ----
    def esd_beleg(self, art, menge="2"):
        b = Beleg.objects.create(mandant=self.m, art=art, kunde=self.kunde)
        p = Position.objects.create(beleg=b, artikel=self.win, bezeichnung="Windows 11", menge=Decimal(menge), einzelpreis=Decimal("120"))
        return b, p

    def test_a11_esd_zuordnung_nur_bis_zur_positionsmenge(self):
        esd.importieren(self.m, self.win, "K1\nK2\nK3\nK4")
        b, p = self.esd_beleg(Belegart.RECHNUNG)
        url = reverse("esd:zuordnen", args=[b.pk])
        self.client.post(url, {"position": p.pk, "anzahl": "3"})  # mehr als verkauft
        self.assertEqual(b.esd_vergaben.count(), 0)
        self.client.post(url, {"position": p.pk, "anzahl": "0"})
        self.client.post(url, {"position": p.pk, "anzahl": "-1"})
        self.assertEqual(b.esd_vergaben.count(), 0)
        self.client.post(url, {"position": p.pk, "anzahl": "2"})
        self.assertEqual(b.esd_vergaben.count(), 2)
        self.client.post(url, {"position": p.pk, "anzahl": "1"})  # Position ist versorgt
        self.assertEqual(b.esd_vergaben.count(), 2)
        self.assertEqual(esd.bestand(self.m, self.win)["frei"], 2)

    def test_a11_esd_zuordnung_nicht_im_angebot(self):
        esd.importieren(self.m, self.win, "K1")
        b, p = self.esd_beleg(Belegart.ANGEBOT, menge="1")
        self.client.post(reverse("esd:zuordnen", args=[b.pk]), {"position": p.pk, "anzahl": "1"})
        self.assertEqual(b.esd_vergaben.count(), 0)
        self.assertEqual(esd.bestand(self.m, self.win)["frei"], 1)

    # ---- A12 ----
    def test_a12_inventur_bucht_nicht_bei_zwischenzeitlicher_bewegung(self):
        url = reverse("lager:inventur_liste")
        self.assertRegex(self.client.get(url).content.decode(), rf'name="soll_{self.maus.pk}" value="10(.000)?"')
        buche_manuell(self.m, self.maus, Bewegungsart.WARENEINGANG, Decimal("2"))  # während des Zählens gebucht → 12
        antwort = self.client.post(url, {f"gezaehlt_{self.maus.pk}": "9", f"soll_{self.maus.pk}": "10.000"}, follow=True)
        self.assertEqual(bestand(self.m, self.maus), Decimal("12"))
        self.assertContains(antwort, "seit dem Öffnen der Zählliste geändert")
        self.client.post(url, {f"gezaehlt_{self.maus.pk}": "9", f"soll_{self.maus.pk}": "12.000"})
        self.assertEqual(bestand(self.m, self.maus), Decimal("9"))
        self.client.post(url, {f"gezaehlt_{self.maus.pk}": "8"})  # ohne Soll (Artikel war ausgeblendet): wie bisher
        self.assertEqual(bestand(self.m, self.maus), Decimal("8"))
