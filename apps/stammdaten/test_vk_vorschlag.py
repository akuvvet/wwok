from decimal import Decimal as D

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.kern.models import Mandant

from .forms import ArtikelForm
from .models import Artikel, PreisVorgabe, Steuersatz
from .preise import stufenpreis


@override_settings(SECURE_SSL_REDIRECT=False)
class VkVorschlagTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.st = Steuersatz.objects.create(bezeichnung="19", prozent=D("19"))
        PreisVorgabe.objects.create(aufschlag_vk1=D("50"), aufschlag_vk2=D("40"))
        self.a = Artikel.objects.create(nummer="1", bezeichnung="Maus", steuersatz=self.st, durchschnitts_ek=D("10"))
        self.client.force_login(get_user_model().objects.create_superuser("chef", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()
        self.admin = reverse("admin:stammdaten_artikel_change", args=[self.a.pk])
        self.dialog = reverse("stammdaten:artikel_bearbeiten", args=[self.a.pk])

    def daten(self, **extra):
        return {"nummer": "1", "typ": "A", "bezeichnung": "Maus", "mengeneinheit": "Stück", "steuersatz": self.st.pk,
                "vk1": "15.00", "vk2": "14.00", "vk3": "", "initial-vk1": "15.00", "initial-vk2": "14.00", "md-lagerplatz": "", "md-mindestbestand": "", "md-maximalbestand": "", **extra}

    def test_leere_vk_zeigen_den_vorschlag(self):
        f = ArtikelForm(instance=self.a)
        self.assertEqual((f["vk1"].value(), f["vk2"].value(), f["vk3"].value()), (D("15.00"), D("14.00"), None))  # VK3 ohne Aufschlag

    def test_fester_preis_wird_nicht_ueberschrieben(self):
        self.a.vk1 = D("99")
        self.a.save()
        f = ArtikelForm(instance=self.a)
        self.assertEqual(f["vk1"].value(), D("99"))
        self.assertNotIn("data-vorschlag", f.fields["vk1"].widget.attrs)

    def test_neuer_artikel_und_leistung_ohne_vorschlag(self):
        self.assertNotIn("data-vorschlag", ArtikelForm(instance=Artikel(steuersatz=self.st)).fields["vk1"].widget.attrs)
        leistung = Artikel.objects.create(nummer="L1", typ="L", bezeichnung="x", steuersatz=self.st, kosten=D("5"))
        self.assertNotIn("data-vorschlag", ArtikelForm(instance=leistung).fields["vk1"].widget.attrs)

    def test_unveraenderter_vorschlag_folgt_weiter_dem_ek(self):
        r = self.client.post(self.dialog, self.daten())
        self.assertEqual(r.status_code, 200)
        self.a.refresh_from_db()
        self.assertEqual((self.a.vk1, self.a.vk2), (None, None))
        self.a.durchschnitts_ek = D("20")
        self.a.save()
        self.assertEqual(stufenpreis(self.a, 1), D("30.00"))

    def test_veralteter_vorschlag_wird_nicht_zum_festpreis(self):
        seite = self.client.get(self.dialog)
        self.assertContains(seite, 'name="initial-vk1"')
        self.assertContains(seite, 'data-vorschlag="15.00"')
        self.a.durchschnitts_ek = D("20")  # Wareneingang, während die Maske offen ist
        self.a.save()
        self.client.post(self.dialog, self.daten())
        self.a.refresh_from_db()
        self.assertEqual((self.a.vk1, self.a.vk2), (None, None))
        self.assertEqual(stufenpreis(self.a, 1), D("30.00"))

    def test_im_selben_speichern_geaenderter_aufschlag_gilt(self):
        self.client.post(self.dialog, self.daten(aufschlag_vk1="100"))
        self.a.refresh_from_db()
        self.assertIsNone(self.a.vk1)
        self.assertEqual(stufenpreis(self.a, 1), D("20.00"))

    def test_kaputtes_verstecktes_feld_macht_keinen_fehler(self):
        r = self.client.post(self.dialog, self.daten(**{"initial-vk1": "abc"}))
        self.assertEqual(r.status_code, 200)

    def test_eigener_preis_ist_fest(self):
        self.client.post(self.dialog, self.daten(vk1="17.50"))
        self.a.refresh_from_db()
        self.assertEqual((self.a.vk1, self.a.vk2), (D("17.50"), None))

    def test_geleerter_fester_preis_kehrt_zum_vorschlag_zurueck(self):
        self.a.vk1 = D("99")
        self.a.save()
        self.client.post(self.dialog, self.daten(vk1=""))
        self.a.refresh_from_db()
        self.assertIsNone(self.a.vk1)
        self.assertEqual(ArtikelForm(instance=self.a)["vk1"].value(), D("15.00"))

    def test_haken_uebernimmt_vk1_fest_fuer_alle(self):
        self.client.post(self.dialog, self.daten(vk1="17.50", vk1_fuer_alle="on"))
        self.a.refresh_from_db()
        self.assertEqual((self.a.vk1, self.a.vk2, self.a.vk3), (D("17.50"), D("17.50"), D("17.50")))
        self.assertNotIn("vk1_fuer_alle", self.a.__dict__)

    def test_haken_mit_vorschlag_macht_ihn_fest(self):
        self.client.post(self.dialog, self.daten(vk1_fuer_alle="on"))
        self.a.refresh_from_db()
        self.assertEqual((self.a.vk1, self.a.vk2, self.a.vk3), (D("15.00"), D("15.00"), D("15.00")))

    def test_haken_ohne_vk1_wird_abgelehnt(self):
        r = self.client.post(self.dialog, self.daten(vk1="", vk1_fuer_alle="on"))
        self.assertEqual(r.status_code, 422)
        self.assertContains(r, "Bitte zuerst VK1 eintragen", status_code=422)

    def test_verwaltung_zeigt_vorschlag_und_haken(self):
        r = self.client.get(self.admin)
        self.assertContains(r, "VK1 für alle VKs übernehmen")
        self.assertContains(r, 'value="15.00"')
        self.assertContains(r, 'value="14.00"')

    def test_verwaltung_speichert_haken(self):
        seite = self.client.get(self.admin)
        daten = {}
        form = seite.context["adminform"].form
        for name in form.fields:
            wert = form[name].value()
            daten[name] = "" if wert is None else wert
        for formset in seite.context["inline_admin_formsets"]:
            daten.update({f"{formset.formset.prefix}-{k}": v for k, v in
                          {"TOTAL_FORMS": "0", "INITIAL_FORMS": "0", "MIN_NUM_FORMS": "0", "MAX_NUM_FORMS": "1000"}.items()})
        daten.pop("nachfolger", None)
        daten.update(vk1="12.00", vk2="", vk3="", vk1_fuer_alle="on", online="", nicht_aktiv="", esd_key="")
        r = self.client.post(self.admin, {k: v for k, v in daten.items() if v not in (False, None)})
        self.assertEqual(r.status_code, 302, getattr(r, "context", None) and r.context["errors"])
        self.a.refresh_from_db()
        self.assertEqual((self.a.vk1, self.a.vk2, self.a.vk3), (D("12.00"), D("12.00"), D("12.00")))
