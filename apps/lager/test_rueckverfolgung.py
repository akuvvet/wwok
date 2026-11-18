"""Rückverfolgung für Reklamationen: Seriennummer -> Wareneingang -> Lieferant, RMA vorbelegt."""
from decimal import Decimal as D

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.einkauf import services as ek
from apps.kern.models import Mandant
from apps.stammdaten.models import Artikel, Lieferant, Steuersatz

from . import services
from .models import Bewegungsart, Lagerbewegung, Seriennummer


class RueckverfolgungTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.zone = Mandant.objects.create(kurzname="ZONE", firmenname="ZONE Handel")
        st = Steuersatz.objects.create(bezeichnung="19", prozent=D("19"))
        self.a = Artikel.objects.create(nummer="NT1", bezeichnung="Netzteil 650W", steuersatz=st)
        self.api = Lieferant.objects.create(nummer="L1", name1="API GmbH", kurzname="API")
        self.wave = Lieferant.objects.create(nummer="L2", name1="Wave AG", kurzname="WAVE")
        self.client.force_login(get_user_model().objects.create_superuser("chef", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    def eingang(self, lieferant, nummern, mandant=None):
        we = ek.wareneingang_anlegen(mandant or self.m, lieferant)
        pos = ek.wareneingang_erfassen(we, self.a, D(len(nummern)), D("40"))
        pos.seriennummern = "\n".join(nummern)
        pos.save()
        ek.wareneingang_buchen(we, bestaetigt=True)
        return we

    def test_wareneingang_merkt_sich_die_herkunft(self):
        we = self.eingang(self.api, ["SN1", "SN2"])
        self.eingang(self.wave, ["SN3"])
        self.assertEqual(Seriennummer.objects.get(nummer="SN1").wareneingang, we)
        self.assertEqual(Seriennummer.objects.get(nummer="SN3").wareneingang.lieferant, self.wave)

    def test_retoure_ohne_wareneingang_behaelt_die_herkunft(self):
        we = self.eingang(self.api, ["SN1"])
        sn = Seriennummer.objects.get(nummer="SN1")
        sn.status = Seriennummer.VERKAUFT
        sn.save()
        services.seriennummern_einlagern(self.m, self.a, "SN1", "Retoure")
        sn.refresh_from_db()
        self.assertEqual(sn.status, Seriennummer.IM_LAGER)
        self.assertEqual(sn.wareneingang, we)

    def test_manuell_eingelagert_hat_keinen_lieferanten(self):
        services.seriennummern_einlagern(self.m, self.a, "SNX", "manuell")
        self.assertIsNone(Seriennummer.objects.get(nummer="SNX").wareneingang)
        r = self.client.get(reverse("lager:seriennummern"), {"q": "SNX"})
        self.assertContains(r, "unbekannt")

    def test_umbuchung_nimmt_die_herkunft_mit(self):
        we = self.eingang(self.api, ["SN1"])
        services.umbuchen(self.m, self.zone, self.a, D("1"), seriennummern=["SN1"])
        sn = Seriennummer.objects.get(nummer="SN1")
        self.assertEqual((sn.mandant, sn.wareneingang), (self.zone, we))
        # im Zielmandanten: Lieferant sichtbar, aber kein Link auf den fremden Wareneingang
        s = self.client.session
        s["mandant_id"] = self.zone.pk
        s.save()
        r = self.client.get(reverse("lager:seriennummern"), {"q": "SN1"})
        self.assertContains(r, "API")
        self.assertNotContains(r, reverse("einkauf:wareneingang", args=[we.pk]))

    def test_suche_zeigt_lieferant_wareneingang_und_rma_link(self):
        we = self.eingang(self.api, ["SN1"])
        r = self.client.get(reverse("lager:seriennummern"), {"q": "SN1"})
        self.assertContains(r, reverse("einkauf:wareneingang", args=[we.pk]))
        self.assertContains(r, reverse("einkauf:lieferant", args=[self.api.pk]))
        self.assertContains(r, f"lieferant={self.api.pk}&amp;artikel=NT1&amp;seriennummer=SN1")

    def test_suche_nur_im_eigenen_mandanten(self):
        self.eingang(self.api, ["SN9"], mandant=self.zone)
        r = self.client.get(reverse("lager:seriennummern"), {"q": "SN9"})
        self.assertNotContains(r, "API")

    def test_artikelseite_zeigt_wareneingaenge_je_lieferant(self):
        self.eingang(self.api, ["SN1"])
        we2 = ek.wareneingang_anlegen(self.m, self.wave)  # ohne Seriennummer
        ek.wareneingang_erfassen(we2, self.a, D("3"), D("38"))
        ek.wareneingang_buchen(we2, bestaetigt=True)
        offen = ek.wareneingang_anlegen(self.m, self.wave)  # ungebucht: zählt nicht
        ek.wareneingang_erfassen(offen, self.a, D("1"), D("38"))
        r = self.client.get(reverse("lager:artikel", args=[self.a.pk]))
        self.assertEqual([p.wareneingang.lieferant for p in r.context["wareneingaenge"]].count(self.wave), 1)
        self.assertEqual(len(r.context["wareneingaenge"]), 2)
        self.assertContains(r, reverse("einkauf:wareneingang", args=[we2.pk]))
        self.assertContains(r, f"lieferant={self.wave.pk}&amp;artikel=NT1")
        self.assertEqual(Lagerbewegung.objects.filter(art=Bewegungsart.WARENEINGANG).count(), 2)

    def test_rma_formular_ist_vorbelegt_und_legt_an(self):
        self.eingang(self.api, ["SN1"])
        r = self.client.get(reverse("einkauf:rma_liste"), {"lieferant": self.wave.pk, "artikel": "nt1", "seriennummer": "SN1"})
        self.assertContains(r, f'<option value="{self.wave.pk}" selected>')
        self.assertContains(r, 'value="Netzteil 650W"')
        self.assertContains(r, 'value="SN1"')
        self.assertContains(r, "Reklamation vorbelegt")
        self.assertNotContains(self.client.get(reverse("einkauf:rma_liste")), "Reklamation vorbelegt")
        self.client.post(reverse("einkauf:rma_neu"), {"lieferant": self.wave.pk, "artikel": "NT1", "bezeichnung": "Netzteil 650W",
                                                      "seriennummer": "SN1", "fehler": "startet nicht"})
        from apps.einkauf.models import Rma
        rma = Rma.objects.get()
        self.assertEqual((rma.lieferant, rma.artikel, rma.seriennummer), (self.wave, self.a, "SN1"))

    def test_rma_gesperrter_lieferant_bleibt_vorgewaehlt(self):
        self.wave.gesperrt = True
        self.wave.save()
        r = self.client.get(reverse("einkauf:rma_liste"), {"lieferant": self.wave.pk, "artikel": "NT1"})
        self.assertContains(r, f'<option value="{self.wave.pk}" selected>')
        self.assertNotContains(r, "bitte wählen")

    def test_rma_ohne_bekannten_lieferanten_muss_gewaehlt_werden(self):
        for parameter in ({"artikel": "NT1", "seriennummer": "SNX"}, {"lieferant": "99999", "artikel": "NT1"}, {"lieferant": "x1"}):
            r = self.client.get(reverse("einkauf:rma_liste"), parameter)
            self.assertContains(r, '<option value="" selected>– bitte wählen –</option>')
            self.assertContains(r, "Der Lieferant ist nicht hinterlegt")
        self.assertNotContains(self.client.get(reverse("einkauf:rma_liste")), "bitte wählen")

    def test_rma_vorbelegung_wird_maskiert(self):
        r = self.client.get(reverse("einkauf:rma_liste"), {"seriennummer": '"><script>x</script>'})
        self.assertNotContains(r, "<script>x</script>")


class HerkunftNachtragenTest(TestCase):
    """Die Migration trägt bestehende Nummern über die Wareneingangsposition nach."""

    def test_nachtragen(self):
        import importlib

        from django.apps import apps as django_apps
        m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        st = Steuersatz.objects.create(bezeichnung="19", prozent=D("19"))
        a = Artikel.objects.create(nummer="NT1", bezeichnung="Netzteil", steuersatz=st)
        lf = Lieferant.objects.create(nummer="L1", name1="API GmbH", kurzname="API")
        we = ek.wareneingang_anlegen(m, lf)
        pos = ek.wareneingang_erfassen(we, a, D("2"), D("40"))
        pos.seriennummern = "SN10\nSN1"
        pos.save()
        ek.wareneingang_buchen(we, bestaetigt=True)
        Seriennummer.objects.update(wareneingang=None)  # Altbestand vor der Verknüpfung
        fremd = Seriennummer.objects.create(mandant=m, artikel=a, nummer="SN")  # nur Teil einer anderen Nummer
        importlib.import_module("apps.lager.migrations.0007_seriennummer_herkunft_nachtragen").herkunft_nachtragen(django_apps, None)
        self.assertEqual(Seriennummer.objects.get(nummer="SN1").wareneingang, we)
        self.assertEqual(Seriennummer.objects.get(nummer="SN10").wareneingang, we)
        fremd.refresh_from_db()
        self.assertIsNone(fremd.wareneingang)
