"""Stammtabellen Anrede, Zahlungsbedingung („zabe“) und Versandart („vers“): Auswahl per Nummer in Kunden- und
Lieferantenmaske, Pflege in der Verwaltung, Standard-Versandart in neuen Belegen."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.kern.models import Mandant
from apps.verkauf.mail import anrede_text
from apps.verkauf.models import Beleg, Versandart

from .forms import AnsprechpartnerKundeSet, KundeForm, LieferantForm
from .models import Anrede, Ansprechpartner, Kunde, Zahlungsbedingung


class StammtabellenTest(TestCase):
    def test_migration_legt_anreden_zahlungsbedingungen_und_versandarten_an(self):
        self.assertEqual(list(Anrede.objects.values_list("nummer", "bezeichnung")), [(1, "Firma"), (2, "Herr"), (3, "Frau")])
        zb = Zahlungsbedingung.objects.get(nummer=60)
        self.assertEqual((zb.bezeichnung, zb.tage_netto, zb.tage_mahnung, zb.tage_lieferstop), ("Zahlbar innerhalb 30 Tagen", 30, 32, 35))
        self.assertEqual(Zahlungsbedingung.objects.get(nummer=91).tage_lieferstop, 95)
        post = Versandart.objects.get(nummer=6)
        self.assertEqual((post.name, post.versandkosten), ("per Post", Decimal("1.90")))
        self.assertEqual(Versandart.objects.get(nummer=8).versandkosten, Decimal("8.50"))
        self.assertEqual(Versandart.objects.count(), 12)


class KundeAnredeTest(TestCase):
    def daten(self, **extra):
        return {"nummer": "K-ANR-1", "name1": "Muster", "land": "DE", "kundenart": "endkunde", "preisstufe": "1", **extra}

    def test_auswahl_zeigt_nummer_und_name(self):
        form = KundeForm()
        grund = next(f for kennung, _, f in form.TABS if kennung == "grund")
        self.assertIn("anrede", grund)
        html = str(form["anrede"])
        self.assertIn('data-suchauswahl="anrede"', html)
        self.assertIn(">3 – Frau</option>", html)
        self.assertIn(">8 – Vorkasse</option>", str(form["zahlungsbedingung"]))
        self.assertIn(">6 – per Post</option>", str(form["standard_versandart"]))

    def test_speichern(self):
        frau = Anrede.objects.get(nummer=3)
        form = KundeForm(self.daten(anrede=frau.pk, zahlungsbedingung=Zahlungsbedingung.objects.get(nummer=8).pk,
                                    standard_versandart=Versandart.objects.get(nummer=8).pk))
        self.assertTrue(form.is_valid(), form.errors)
        kunde = form.save()
        self.assertEqual((kunde.anrede, kunde.zahlungsbedingung.nummer, kunde.standard_versandart.nummer), (frau, 8, 8))

    def test_ungueltige_anrede(self):
        form = KundeForm(self.daten(anrede="999"))
        self.assertFalse(form.is_valid())
        self.assertIn("anrede", form.errors)

    def test_neue_anrede_ist_waehlbar(self):
        dr = Anrede.objects.create(nummer=4, bezeichnung="Dr.")
        self.assertIn(">4 – Dr.</option>", str(KundeForm()["anrede"]))
        self.assertTrue(KundeForm(self.daten(anrede=dr.pk)).is_valid())

    def test_inaktive_versandart_nur_wenn_schon_gewaehlt(self):
        alt = Versandart.objects.get(nummer=3)
        alt.aktiv = False
        alt.save()
        self.assertNotIn("V-Scheck", str(KundeForm()["standard_versandart"]))
        kunde = Kunde.objects.create(nummer="K-V", name1="X", standard_versandart=alt)
        self.assertIn("V-Scheck", str(KundeForm(instance=kunde)["standard_versandart"]))

    def test_lieferant_hat_anrede_und_versandart(self):
        form = LieferantForm()
        self.assertIn("anrede", form.fields)
        self.assertIn(">10 – Abholung</option>", str(form["standard_versandart"]))


class AnsprechpartnerAnredeTest(TestCase):
    def test_auswahl_und_briefanrede(self):
        kunde = Kunde.objects.create(nummer="K1", name1="Muster GmbH")
        formset = AnsprechpartnerKundeSet(instance=kunde)
        self.assertIn(">2 – Herr</option>", str(formset.forms[0]["anrede"]))
        Ansprechpartner.objects.create(kunde=kunde, nachname="Meier", anrede=Anrede.objects.get(nummer=2), hauptkontakt=True)
        self.assertEqual(anrede_text(kunde), "Sehr geehrter Herr Meier")
        Ansprechpartner.objects.filter(kunde=kunde).update(anrede=Anrede.objects.get(nummer=1))
        self.assertEqual(anrede_text(kunde), "Sehr geehrte Damen und Herren")


@override_settings(SECURE_SSL_REDIRECT=False)
class VerwaltungUndBelegTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.client.force_login(get_user_model().objects.create_superuser("chef", "c@example.test", "x" * 14))
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    def test_verwaltung_listen(self):
        for name, text in (("stammdaten_anrede", "Frau"), ("stammdaten_zahlungsbedingung", "Zahlbar innerhalb 90 Tagen"),
                           ("verkauf_versandart", "per DHL Frei")):
            r = self.client.get(reverse(f"admin:{name}_changelist"))
            self.assertContains(r, text)

    def test_anrede_in_verwaltung_anlegen(self):
        r = self.client.post(reverse("admin:stammdaten_anrede_add"), {"nummer": "4", "bezeichnung": "Dr."})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(Anrede.objects.filter(nummer=4, bezeichnung="Dr.").exists())

    def test_neuer_beleg_uebernimmt_standard_versandart(self):
        dhl = Versandart.objects.get(nummer=8)
        kunde = Kunde.objects.create(nummer="K2", name1="Kunde", standard_versandart=dhl)
        self.client.post(reverse("verkauf:beleg_neu", args=[kunde.pk]), {"art": "AN"})
        beleg = Beleg.objects.get(kunde=kunde)
        self.assertEqual((beleg.versandart, beleg.versandkosten), ("per DHL Frei", Decimal("8.50")))

    def test_nur_inland_bei_auslandskunde_ohne_versandart(self):
        dhl = Versandart.objects.get(nummer=8)
        dhl.nur_inland = True
        dhl.save()
        kunde = Kunde.objects.create(nummer="K3", name1="Kunde", land="AT", standard_versandart=dhl)
        self.client.post(reverse("verkauf:beleg_neu", args=[kunde.pk]), {"art": "AN"})
        self.assertEqual(Beleg.objects.get(kunde=kunde).versandart, "")
