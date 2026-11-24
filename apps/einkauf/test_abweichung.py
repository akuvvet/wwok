from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.kern.models import Mandant
from apps.lager.models import Bewegungsart
from apps.lager.services import buche_manuell
from apps.stammdaten.models import Artikel, Lieferant, Steuersatz

from . import services
from .models import RechnungStatus


@override_settings(SECURE_SSL_REDIRECT=False)
class AbweichungTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer", iban="DE02120300000000202051")
        self.lf = Lieferant.objects.create(nummer="L1", name1="API GmbH", kurzname="API", iban="DE89370400440532013000")
        st = Steuersatz.objects.create(bezeichnung="19", prozent=Decimal("19"))
        self.maus = Artikel.objects.create(nummer="M1", bezeichnung="Maus", steuersatz=st)
        we = services.wareneingang_anlegen(self.m, self.lf)
        services.wareneingang_erfassen(we, self.maus, Decimal("10"), Decimal("9.50"))
        services.wareneingang_buchen(we)
        self.we = we
        self.user = get_user_model().objects.create_user("t", password="x" * 14)
        self.client.force_login(self.user)
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    def rechnung(self, netto="100.00", brutto="119.00", nummer="R-1"):
        return services.rechnung_erfassen(self.m, self.lf, nummer, date.today(), Decimal(netto), Decimal(brutto),
                                          wareneingaenge=[self.we])

    def test_abweichung_wird_gemeldet_zahlung_bleibt_erlaubt(self):
        """Entscheidung des Nutzers 24.11.2026: Abweichung zum Wareneingang nur melden, Zahlung trotzdem erlauben."""
        r = self.rechnung()  # Wareneingang netto 95,00, Rechnung 100,00
        a = r.abgleich()
        self.assertEqual((a["ok"], a["offen"], a["differenz"]), (False, True, Decimal("5.00")))
        self.assertIn("weicht um 5.00", r.abweichung_hinweis())
        seite = self.client.get(reverse("einkauf:rechnung", args=[r.pk]))
        self.assertContains(seite, "Zahlung und SEPA-Export sind trotzdem möglich")
        self.assertNotContains(seite, "Erst Abweichung klären")  # Knopf „Als bezahlt buchen“ nicht gesperrt
        for text in ('id="korrektur"', "data-ust-gruppe", 'data-ust="netto"', 'data-ust="brutto"'):  # Netto/USt/Brutto gegenseitig
            self.assertContains(seite, text)
        antwort = self.client.post(reverse("einkauf:rechnung_bezahlt", args=[r.pk]), {"datum": date.today().isoformat()}, follow=True)
        self.assertContains(antwort, "Bezahlt gebucht trotz Abweichung")
        r.refresh_from_db()
        self.assertEqual((r.status, r.bezahlter_betrag), (RechnungStatus.BEZAHLT, Decimal("119.00")))

    def test_passende_rechnung_ist_frei(self):
        r = self.rechnung("95.00", "113.05")
        self.assertIsNone(r.abweichung_hinweis())
        self.client.post(reverse("einkauf:rechnung_bezahlt", args=[r.pk]), {"datum": date.today().isoformat()})
        r.refresh_from_db()
        self.assertEqual(r.status, RechnungStatus.BEZAHLT)

    def test_bestaetigung_braucht_begruendung_und_wird_protokolliert(self):
        r = self.rechnung()
        with self.assertRaises(ValueError):
            r.abweichung_bestaetigen("  ", self.user)
        self.client.post(reverse("einkauf:rechnung_abweichung_bestaetigen", args=[r.pk]), {"grund": "Preis telefonisch abgestimmt"})
        r.refresh_from_db()
        a = r.abgleich()
        self.assertEqual((a["offen"], a["bestaetigt"]), (False, True))
        self.assertIn("Preis telefonisch abgestimmt", r.historie)
        self.assertIn("t:", r.historie)
        self.assertIsNone(r.abweichung_hinweis())  # bestätigt: kein Hinweis mehr

    def test_bestaetigung_verfaellt_bei_geaenderter_differenz(self):
        r = self.rechnung()
        r.abweichung_bestaetigen("ok", self.user)
        r.betraege_korrigieren(Decimal("110.00"), Decimal("20.90"), Decimal("130.90"), self.user)
        self.assertTrue(r.abgleich()["offen"])  # andere Differenz (15,00) -> erneut klären

    def test_korrektur_loest_abweichung_und_prueft_betraege(self):
        r = self.rechnung()
        with self.assertRaises(ValueError):
            r.betraege_korrigieren(Decimal("95.00"), Decimal("18.05"), Decimal("120.00"))  # Summe stimmt nicht
        self.client.post(reverse("einkauf:rechnung_korrigieren", args=[r.pk]), {"netto": "95.00", "steuer": "18.05", "brutto": "113.05"})
        r.refresh_from_db()
        self.assertTrue(r.abgleich()["ok"])
        self.assertIn("Beträge korrigiert", r.historie)
        self.assertEqual(r.brutto, Decimal("113.05"))

    def test_bezahlte_rechnung_nicht_korrigierbar(self):
        r = self.rechnung("95.00", "113.05")
        r.status = RechnungStatus.BEZAHLT
        r.save()
        with self.assertRaises(ValueError):
            r.betraege_korrigieren(Decimal("90.00"), Decimal("17.10"), Decimal("107.10"))

    def test_sepa_export_mit_offener_abweichung_erlaubt_mit_meldung(self):
        gut = self.rechnung("95.00", "113.05", "R-OK")
        schlecht = self.rechnung("100.00", "119.00", "R-BAD")
        antwort = self.client.post(reverse("einkauf:sepa"), {"auswahl": [gut.pk, schlecht.pk], "ausfuehrung": date.today().isoformat()})
        self.assertEqual(antwort["Content-Type"], "application/xml")
        for r in (gut, schlecht):
            r.refresh_from_db()
            self.assertEqual(r.status, RechnungStatus.EXPORTIERT)
        meldungen = [str(m) for m in self.client.get(reverse("einkauf:rechnungen")).context["messages"]]
        self.assertTrue(any("Exportiert trotz Abweichung" in m and "R-BAD" in m and "R-OK" not in m for m in meldungen), meldungen)

    def test_notiz_und_meldung_in_der_liste(self):
        r = self.rechnung()
        self.client.post(reverse("einkauf:rechnung_notiz", args=[r.pk]), {"bemerkung": "Rückfrage bei Lieferant läuft"})
        r.refresh_from_db()
        self.assertEqual(r.bemerkung, "Rückfrage bei Lieferant läuft")
        liste = self.client.get(reverse("einkauf:rechnungen"))
        self.assertContains(liste, "weicht vom Wareneingang ab")
        self.assertContains(liste, "Rückfrage bei Lieferant läuft")
        self.assertEqual(liste.context["abweichungen"], 1)

    def test_andere_mandanten_nicht_erreichbar(self):
        r = self.rechnung()
        fremd = Mandant.objects.create(kurzname="ZONE", firmenname="ZONE Handel")
        s = self.client.session
        s["mandant_id"] = fremd.pk
        s.save()
        self.assertEqual(self.client.post(reverse("einkauf:rechnung_notiz", args=[r.pk]), {"bemerkung": "x"}).status_code, 404)
