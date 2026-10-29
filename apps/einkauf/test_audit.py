"""Regressionstests zur Projektprüfung: Wareneingang, Rechnungen, Preislisten-Schutz, Rechte, Lagerbuchung."""
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.kern.models import Mandant
from apps.lager.models import Bewegungsart, Lagerbewegung, bestand
from apps.stammdaten.models import Artikel, Lieferant, Steuersatz

from . import services
from .models import Bestellung, BestellPosition, Lieferantenrechnung, RechnungStatus


@override_settings(SECURE_SSL_REDIRECT=False)
class AuditEinkaufTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.m2 = Mandant.objects.create(kurzname="ZONE", firmenname="ZONE Handel")
        self.lf = Lieferant.objects.create(nummer="L1", name1="API GmbH", kurzname="API")
        st = Steuersatz.objects.create(bezeichnung="19", prozent=Decimal("19"))
        self.a = Artikel.objects.create(nummer="A1", bezeichnung="Maus", steuersatz=st)
        self.user = get_user_model().objects.create_user("t", password="x" * 14)
        self.client.force_login(self.user)
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    def we(self, mandant=None, bestellung=None):
        return services.wareneingang_anlegen(mandant or self.m, self.lf, bestellung=bestellung) if bestellung else \
            services.wareneingang_anlegen(mandant or self.m, self.lf)

    # Wareneingang
    def test_zwei_zeilen_desselben_artikels_ergeben_richtigen_durchschnitts_ek(self):
        we = self.we()
        services.wareneingang_erfassen(we, self.a, Decimal("10"), Decimal("20"), seriennummern="\n".join(f"S{i}" for i in range(10)))
        services.wareneingang_erfassen(we, self.a, Decimal("10"), Decimal("30"), seriennummern="\n".join(f"T{i}" for i in range(10)))
        services.wareneingang_buchen(we)
        self.a.refresh_from_db()
        self.assertEqual(self.a.durchschnitts_ek, Decimal("25.0000"))
        self.assertEqual(bestand(self.m, self.a), Decimal("20"))

    def test_durchschnitts_ek_zaehlt_bestand_aller_mandanten(self):
        zone = self.we(self.m2)
        services.wareneingang_erfassen(zone, self.a, Decimal("100"), Decimal("10"))
        services.wareneingang_buchen(zone)
        okay = self.we()
        services.wareneingang_erfassen(okay, self.a, Decimal("1"), Decimal("20"))
        services.wareneingang_buchen(okay)
        self.a.refresh_from_db()
        self.assertEqual(self.a.durchschnitts_ek, Decimal("10.0990"))  # (100*10 + 1*20) / 101

    def test_liefermenge_der_bestellung_summiert_mehrere_zeilen(self):
        b = Bestellung.objects.create(mandant=self.m, lieferant=self.lf, status="bestellt")
        bp = BestellPosition.objects.create(bestellung=b, artikel=self.a, menge=Decimal("8"), ek=Decimal("5"))
        we = self.we(bestellung=b)
        from .models import WareneingangPosition
        WareneingangPosition.objects.create(wareneingang=we, bestellposition=bp, artikel=self.a, menge=Decimal("5"), ek=Decimal("5"))
        WareneingangPosition.objects.create(wareneingang=we, bestellposition=bp, artikel=self.a, menge=Decimal("3"), ek=Decimal("5"))
        services.wareneingang_buchen(we)
        bp.refresh_from_db()
        self.assertEqual(bp.geliefert, Decimal("8"))
        b.refresh_from_db()
        self.assertEqual(b.status, "geliefert")

    def test_menge_und_ek_muessen_gueltig_sein(self):
        we = self.we()
        for menge, ek in ((Decimal("-5"), None), (Decimal("0"), None), (Decimal("1"), Decimal("-1"))):
            with self.assertRaises(ValueError):
                services.wareneingang_erfassen(we, self.a, menge, ek)
        self.assertEqual(bestand(self.m, self.a), Decimal("0"))

    def test_veralteter_zweiter_buchungsversuch_bucht_nicht_doppelt(self):
        we = self.we()
        services.wareneingang_erfassen(we, self.a, Decimal("2"), Decimal("5"))
        zweiter_tab = type(we).objects.get(pk=we.pk)
        services.wareneingang_buchen(we)
        with self.assertRaises(ValueError):
            services.wareneingang_buchen(zweiter_tab)
        self.assertEqual(bestand(self.m, self.a), Decimal("2"))

    # Lieferantenrechnung
    def test_rechnung_doppelt_oder_negativ_wird_abgelehnt(self):
        services.rechnung_erfassen(self.m, self.lf, "R-1", date.today(), Decimal("100"), Decimal("119"))
        with self.assertRaises(ValueError):
            services.rechnung_erfassen(self.m, self.lf, "R-1", date.today(), Decimal("100"), Decimal("119"))
        with self.assertRaises(ValueError):
            services.rechnung_erfassen(self.m, self.lf, "R-2", date.today(), Decimal("-100"), Decimal("-119"))
        r = self.client.post(reverse("einkauf:rechnung_neu"), {"lieferant": self.lf.pk, "rechnungsnummer": "R-1",
                                                               "brutto": "119", "netto": "100", "datum": date.today().isoformat()})
        self.assertEqual(r.status_code, 302)  # Meldung statt 500

    def test_bezahlen_prueft_betrag_und_nur_einmal(self):
        r = services.rechnung_erfassen(self.m, self.lf, "R-1", date.today(), Decimal("100"), Decimal("119"))
        url = reverse("einkauf:rechnung_bezahlt", args=[r.pk])
        for betrag in ("-5", "nan", "0"):
            self.assertEqual(self.client.post(url, {"betrag": betrag, "datum": date.today().isoformat()}).status_code, 302)
        r.refresh_from_db()
        self.assertEqual(r.status, RechnungStatus.OFFEN)
        self.client.post(url, {"betrag": "119", "datum": date.today().isoformat()})
        self.client.post(url, {"betrag": "1", "datum": date.today().isoformat()})  # zweites Mal: ignoriert
        r.refresh_from_db()
        self.assertEqual((r.status, r.bezahlter_betrag), (RechnungStatus.BEZAHLT, Decimal("119.00")))

    def test_exportierte_rechnung_nicht_korrigierbar(self):
        r = services.rechnung_erfassen(self.m, self.lf, "R-1", date.today(), Decimal("100"), Decimal("119"))
        r.status = RechnungStatus.EXPORTIERT
        r.save()
        with self.assertRaises(ValueError):
            r.betraege_korrigieren(Decimal("90"), Decimal("17.10"), Decimal("107.10"))

    def test_wareneingang_gehoert_zu_hoechstens_einer_rechnung(self):
        we = self.we()
        services.wareneingang_erfassen(we, self.a, Decimal("1"), Decimal("100"))
        services.wareneingang_buchen(we)
        r1 = services.rechnung_erfassen(self.m, self.lf, "R-1", date.today(), Decimal("100"), Decimal("119"), wareneingaenge=[we])
        r2 = services.rechnung_erfassen(self.m, self.lf, "R-2", date.today(), Decimal("100"), Decimal("119"))
        self.client.post(reverse("einkauf:rechnung", args=[r2.pk]), {"wareneingang": [we.pk]})
        self.assertEqual(r2.wareneingaenge.count(), 0)
        self.assertEqual(r1.wareneingaenge.count(), 1)

    # Rechte
    def test_preislisten_import_und_bestpreise_nur_fuer_admins(self):
        from .models import Preislistenprofil
        profil = Preislistenprofil.objects.create(lieferant=self.lf, name="Test", quelle="upload")
        for name, args in (("einkauf:preisliste", [profil.pk]), ("einkauf:bestpreise", []), ("einkauf:artikelpreise", [])):
            self.assertEqual(self.client.get(reverse(name, args=args)).status_code, 200, name)  # ansehen darf jeder
            antwort = self.client.post(reverse(name, args=args), {}, follow=True)
            self.assertContains(antwort, "Berechtigung", msg_prefix=name)
        self.user.is_staff = True
        self.user.save()
        self.assertEqual(self.client.get(reverse("einkauf:bestpreise")).status_code, 200)

    # Lager
    def test_lager_buchen_nur_manuelle_arten_und_alles_oder_nichts(self):
        url = reverse("lager:buchen", args=[self.a.pk])
        for art in ("UM", "ZZ", "VK"):
            self.client.post(url, {"art": art, "menge": "7"})
        self.assertEqual(Lagerbewegung.objects.count(), 0)
        self.client.post(url, {"art": Bewegungsart.WARENEINGANG, "menge": "1", "seriennummern": "SN1"})
        self.client.post(url, {"art": Bewegungsart.WARENEINGANG, "menge": "1", "seriennummern": "SN1"})  # Seriennummer doppelt
        self.assertEqual(bestand(self.m, self.a), Decimal("1"))  # zweite Buchung vollständig zurückgenommen
        self.assertEqual(self.client.post(url, {"art": Bewegungsart.WARENEINGANG, "menge": "NaN"}).status_code, 302)
