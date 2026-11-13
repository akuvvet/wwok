"""ESD ohne Key: Lieferschein oder Rechnung lassen sich nach Bestätigung auch ohne (alle) Keys abschließen und drucken."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.kern.models import Mandant
from apps.lager.models import Bewegungsart
from apps.lager.services import buche_manuell
from apps.stammdaten.models import Artikel, Kunde, Steuersatz
from apps.verkauf.models import Beleg, Belegart, Position

from . import services
from .models import ESDVergabe


@override_settings(SECURE_SSL_REDIRECT=False)
class OhneKeyTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.k = Kunde.objects.create(nummer="1", name1="Kunde Eins")
        st = Steuersatz.objects.create(bezeichnung="19x", prozent=Decimal("19"))
        self.win = Artikel.objects.create(nummer="WIN11", bezeichnung="Windows 11 Pro", steuersatz=st, vk1=Decimal("120"), esd_key=True)
        buche_manuell(self.m, self.win, Bewegungsart.WARENEINGANG, Decimal("50"))
        self.client.force_login(get_user_model().objects.create_superuser("t", "t@example.test", "x" * 14))
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()
        services.importieren(self.m, self.win, "KEY-A")  # ein Key vorhanden, zwei werden gebraucht
        self.b = Beleg.objects.create(mandant=self.m, art=Belegart.RECHNUNG, kunde=self.k)
        Position.objects.create(beleg=self.b, artikel=self.win, bezeichnung="Windows 11 Pro", menge=Decimal("2"), einzelpreis=Decimal("120"))
        self.url = reverse("verkauf:beleg_abschliessen", args=[self.b.pk])

    def test_ohne_bestaetigung_rueckfrage_und_nichts_abgeschlossen(self):
        r = self.client.post(self.url, {"danach": "pdf"})
        self.assertContains(r, "Ja, ohne Key abschließen")
        self.assertContains(r, 'name="danach" value="pdf"')
        self.b.refresh_from_db()
        self.assertFalse(self.b.abgeschlossen)
        self.assertEqual(ESDVergabe.objects.count(), 0)

    def test_nach_bestaetigung_abgeschlossen_mit_vorhandenem_key(self):
        r = self.client.post(self.url, {"esd_ohne_key": "1", "danach": "pdf"})
        self.assertEqual(r.status_code, 302)
        self.assertIn(reverse("verkauf:beleg_ansicht", args=[self.b.pk]), r["Location"])
        self.b.refresh_from_db()
        self.assertTrue(self.b.abgeschlossen)
        self.assertEqual(ESDVergabe.objects.count(), 1)  # vorhandener Key vergeben, einer fehlt
        self.assertEqual(services.fehlende_keys(self.b)[0][1], 1)

    def test_ohne_freigabe_bleibt_die_regel(self):
        with self.assertRaises(services.KeysFehlen):
            self.b.abschliessen()
