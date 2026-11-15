"""Vorgabe des Nutzers (15.11.2026): Kein Bestandswert geht ins Minus – weder Lagerbestand noch Reparaturbestand noch „verfügbar“."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.kern.models import Mandant
from apps.stammdaten.models import Artikel, Kunde, Steuersatz
from apps.verkauf.models import Beleg, Belegart, Position

from .models import BestandReichtNicht, Bewegungsart, Lagerbewegung, bestand, reserviert, verfuegbar
from .services import buche_manuell, inventur, reparatur_ausbuchen, umbuchen


@override_settings(SECURE_SSL_REDIRECT=False)
class NieMinusTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.m2 = Mandant.objects.create(kurzname="ZONE", firmenname="ZONE Handel")
        self.kunde = Kunde.objects.create(nummer="1", name1="Kunde")
        st = Steuersatz.objects.create(bezeichnung="19", prozent=Decimal("19"))
        self.a = Artikel.objects.create(nummer="A1", bezeichnung="Maus", steuersatz=st, vk1=Decimal("10"))
        buche_manuell(self.m, self.a, Bewegungsart.WARENEINGANG, Decimal("5"))

    def bestellung(self, menge):
        b = Beleg.objects.create(mandant=self.m, art=Belegart.AUFTRAG, kunde=self.kunde)
        Position.objects.create(beleg=b, artikel=self.a, bezeichnung="Maus", menge=Decimal(menge), einzelpreis=Decimal("10"))
        return b

    def test_jeder_abgang_unter_null_wird_abgelehnt(self):
        with self.assertRaises(BestandReichtNicht):
            buche_manuell(self.m, self.a, Bewegungsart.EIGENVERBRAUCH, Decimal("6"))
        with self.assertRaises(BestandReichtNicht):
            Lagerbewegung.objects.create(mandant=self.m, artikel=self.a, art=Bewegungsart.KORREKTUR, menge=Decimal("-5.001"))
        with self.assertRaises(ValueError):
            umbuchen(self.m, self.m2, self.a, Decimal("6"))
        with self.assertRaises(ValueError):
            inventur(self.m, self.a, Decimal("-1"))
        with self.assertRaises(ValueError):
            reparatur_ausbuchen(self.m, self.a, Decimal("1"), "Schrott")  # Reparaturbestand ist 0
        self.assertEqual((bestand(self.m, self.a), bestand(self.m2, self.a)), (Decimal("5"), Decimal("0")))

    def test_verfuegbar_ist_bei_ueberbestellung_null_statt_negativ(self):
        self.bestellung("8")  # mehr bestellt als am Lager
        self.assertEqual((bestand(self.m, self.a), reserviert(self.m, self.a)), (Decimal("5"), Decimal("8")))
        self.assertEqual(verfuegbar(self.m, self.a), Decimal("0"))

    def test_masken_zeigen_kein_minus(self):
        self.bestellung("8")
        self.client.force_login(get_user_model().objects.create_superuser("chef", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()
        liste = self.client.get(reverse("lager:uebersicht"))
        self.assertEqual(liste.context["zeilen"][0]["verfuegbar"], Decimal("0"))
        self.assertEqual(self.client.get(reverse("lager:artikel", args=[self.a.pk])).context["verfuegbar"], Decimal("0"))
