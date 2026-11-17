"""Standard-VK je Mandant: in der Verwaltung wählbar, die Artikellisten zeigen diese Preisstufe."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.stammdaten.models import Artikel, Kunde, Steuersatz
from apps.stammdaten.preise import verkaufspreis

from .models import Mandant


@override_settings(SECURE_SSL_REDIRECT=False)
class StandardVkTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        st = Steuersatz.objects.create(bezeichnung="19", prozent=Decimal("19"))
        self.a = Artikel.objects.create(nummer="A1", bezeichnung="Maus", steuersatz=st, vk1=Decimal("111.11"),
                                        vk2=Decimal("222.22"), vk3=Decimal("333.33"))
        self.client.force_login(get_user_model().objects.create_superuser("chef", "c@okay.test", "x" * 14))
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    def stufe(self, n):
        self.m.standard_vk = n
        self.m.save()

    def test_vorgabe_ist_vk3_wie_bisher(self):
        self.assertEqual(self.m.standard_vk, 3)
        r = self.client.get(reverse("lager:uebersicht"))
        self.assertContains(r, "VK3 netto")
        self.assertContains(r, "333,33")
        self.assertNotContains(r, "111,11")

    def test_artikel_und_bestand_zeigt_gewaehlte_stufe(self):
        for n, preis in ((1, "111,11"), (2, "222,22")):
            self.stufe(n)
            r = self.client.get(reverse("lager:uebersicht"))
            self.assertContains(r, f"VK{n} netto")
            self.assertContains(r, preis)
            self.assertNotContains(r, "333,33")

    def test_tabellensuche_liefert_gewaehlte_stufe(self):
        self.stufe(2)
        daten = self.client.get(reverse("stammdaten:artikel_tabelle"), {"q": "*"}).json()
        self.assertEqual(daten["treffer"][0]["vk"], "222,22")
        maske = self.client.get(reverse("stammdaten:artikel_bearbeiten", args=[self.a.pk]))
        self.assertContains(maske, "VK2 netto")  # Spaltenkopf der Tabellensuche

    def test_verwaltung_artikelliste_zeigt_gewaehlte_stufe(self):
        self.stufe(1)
        r = self.client.get(reverse("admin:stammdaten_artikel_changelist"))
        self.assertContains(r, "111,11")
        self.assertNotContains(r, "333,33")
        self.stufe(3)
        r = self.client.get(reverse("admin:stammdaten_artikel_changelist"))
        self.assertContains(r, "333,33")
        self.assertContains(r, "VK3")

    def test_je_mandant_getrennt(self):
        zone = Mandant.objects.create(kurzname="ZONE", firmenname="ZONE Handel", standard_vk=1)
        s = self.client.session
        s["mandant_id"] = zone.pk
        s.save()
        self.assertContains(self.client.get(reverse("lager:uebersicht")), "111,11")
        s["mandant_id"] = self.m.pk
        s.save()
        self.assertContains(self.client.get(reverse("lager:uebersicht")), "333,33")

    def test_in_verwaltung_einstellbar(self):
        seite = self.client.get(reverse("admin:kern_mandant_change", args=[self.m.pk]))
        self.assertContains(seite, 'name="standard_vk"')
        self.assertContains(seite, "VK2 – gewerbliche Endkunden")

    def test_belegpreis_folgt_weiter_der_preisstufe_des_kunden(self):
        self.stufe(3)
        kunde = Kunde.objects.create(nummer="1", name1="Endkunde", preisstufe=1)
        self.assertEqual(verkaufspreis(self.a, kunde)[0], Decimal("111.11"))
