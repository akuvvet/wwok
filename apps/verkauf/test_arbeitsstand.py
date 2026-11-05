"""Positionsänderungen gelten sofort: kein Arbeitsstand, kein Balken, keine Rückfrage beim Verlassen."""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

from apps.kern.models import Mandant
from apps.stammdaten.models import Artikel, Kunde, KundeMandant, Steuersatz

from .models import Beleg, BelegArbeitsstand, Belegart, Position


@override_settings(SECURE_SSL_REDIRECT=False)
class ArbeitsstandTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.k = Kunde.objects.create(nummer="1", name1="K")
        self.st = Steuersatz.objects.create(bezeichnung="19", prozent=Decimal("19"))
        self.a = Artikel.objects.create(nummer="A1", bezeichnung="Gehäuse", steuersatz=self.st, vk1=Decimal("100"))
        self.client.force_login(get_user_model().objects.create_user("t", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()
        self.beleg = Beleg.objects.create(mandant=self.m, art=Belegart.ANGEBOT, kunde=self.k)
        self.p = Position.objects.create(beleg=self.beleg, artikel=self.a, bezeichnung="Gehäuse", einzelpreis=Decimal("100"))
        self.beleg.berechne()

    def url(self, name, *args):
        return reverse("verkauf:" + name, args=[self.beleg.pk, *args])

    def neu(self, **felder):
        return self.client.post(self.url("position_neu"), {"typ": "A", "artikel": "A1", "menge": "1", **felder})

    def stand(self):
        return BelegArbeitsstand.objects.filter(beleg=self.beleg).exists()

    def alter_stand(self, **felder):
        return BelegArbeitsstand.objects.create(beleg=self.beleg, daten="{}", **felder)

    def test_positionsaenderung_gilt_sofort_ohne_balken(self):
        r = self.neu()
        self.assertEqual(r.status_code, 302)
        self.assertFalse(self.stand())
        self.assertEqual(self.beleg.positionen.count(), 2)
        r = self.client.get(self.url("beleg"))  # nach dem Neuladen sichtbar
        self.assertContains(r, "Gespeichert.")
        self.assertNotContains(r, "Nicht gespeicherte Änderungen")
        self.assertNotContains(r, "beforeunload")
        self.assertNotContains(r, "Dieser Beleg hat nicht gespeicherte Änderungen")

    def test_alle_positionsaktionen_legen_keinen_stand_an(self):
        self.neu()
        self.client.post(self.url("position_aendern", self.p.pk), {"menge": "3", "preis": "90", "rabatt": "0"})
        self.p.refresh_from_db()
        self.assertEqual((self.p.menge, self.p.einzelpreis), (Decimal("3"), Decimal("90")))
        self.client.post(self.url("position_loeschen", self.p.pk))
        self.assertEqual(self.beleg.positionen.count(), 1)
        self.assertFalse(self.stand())

    def test_bestehender_stand_fuehrt_beim_oeffnen_nicht_zum_balken(self):
        self.neu()
        self.alter_stand()
        r = self.client.get(self.url("beleg"))
        self.assertNotContains(r, "Nicht gespeicherte Änderungen")
        self.assertFalse(self.stand())
        self.assertEqual(self.beleg.positionen.count(), 2)  # der aktuelle Stand gilt

    def test_alter_stand_von_anderem_bearbeiter_wird_still_beendet(self):
        anderer = get_user_model().objects.create_user("b", password="x" * 14)
        self.neu()
        self.alter_stand(benutzer=anderer, geaendert_am=timezone.now() - timedelta(hours=9))
        BelegArbeitsstand.objects.filter(beleg=self.beleg).update(geaendert_am=timezone.now() - timedelta(hours=9))
        r = self.client.get(self.url("beleg"), follow=True)
        self.assertNotContains(r, "zu alt")
        self.assertNotContains(r, "Nicht gespeicherte Änderungen")
        self.assertEqual(self.beleg.positionen.count(), 2)

    def test_speichern_und_verwerfen_gibt_es_nicht_mehr(self):
        for name in ("beleg_speichern", "beleg_verwerfen"):
            with self.assertRaises(NoReverseMatch):
                self.url(name)

    def test_alter_stand_in_der_kundenliste_beendet_ohne_positionen_zu_aendern(self):
        self.neu()
        self.alter_stand()
        BelegArbeitsstand.objects.filter(beleg=self.beleg).update(geaendert_am=timezone.now() - timedelta(hours=9))
        self.assertEqual(self.client.get(reverse("verkauf:kunde", args=[self.k.pk])).status_code, 200)
        self.assertEqual(self.beleg.positionen.count(), 2)
        self.assertFalse(self.stand())

    def test_alter_stand_blockiert_mail_und_bestellung_nicht(self):
        self.alter_stand()
        r = self.client.post(reverse("verkauf:beleg_mail", args=[self.beleg.pk]), {"an": "a@example.org"}, follow=True)
        self.assertNotContains(r, "nicht gespeicherte Änderungen")
        self.assertFalse(self.stand())

    def test_abschliessen_und_kopfdaten_beenden_alten_stand(self):
        self.alter_stand()
        self.client.post(self.url("beleg_kopf"), {"versandkosten": "0"})
        self.assertFalse(self.stand())
        self.alter_stand()
        self.assertEqual(self.client.post(self.url("beleg_abschliessen"), {"danach": ""}).status_code, 302)
        self.beleg.refresh_from_db()
        self.assertTrue(self.beleg.abgeschlossen)
        self.assertFalse(self.stand())

    def test_abgeschlossener_beleg_bleibt_gesperrt(self):
        re = Beleg.objects.create(mandant=self.m, art=Belegart.RECHNUNG, kunde=self.k)
        Position.objects.create(beleg=re, bezeichnung="Gehäuse", einzelpreis=Decimal("100"))  # freie Position: braucht keinen Lagerbestand
        re.berechne()
        re.abschliessen()
        re.refresh_from_db()
        self.assertTrue(re.positionen_gesperrt)
        r = self.client.post(reverse("verkauf:position_neu", args=[re.pk]), {"typ": "A", "artikel": "A1", "menge": "1"}, follow=True)
        self.assertEqual(re.positionen.count(), 1)
        self.assertContains(r, "abgeschlossen")
        self.assertFalse(BelegArbeitsstand.objects.filter(beleg=re).exists())

    def test_kreditwarnung_steht_in_der_kopfkarte_unter_netto(self):
        KundeMandant.objects.create(kunde=self.k, mandant=self.m, kreditlimit=Decimal("10"))
        Beleg.objects.filter(pk=self.beleg.pk).update(art=Belegart.AUFTRAG)
        html = self.client.get(self.url("beleg")).content.decode()
        self.assertIn("kreditwarnung", html)
        self.assertIn("Kreditlimit −", html)  # kompakt: nur der Überschreitungsbetrag
        self.assertIn('title="offen ', html)  # volle Rechnung als Tooltip
        self.assertNotIn("würde überschritten", html)
        self.assertLess(html.index("netto "), html.index('class="kreditwarnung"'))
        self.assertLess(html.index('class="kreditwarnung"'), html.index('class="aktionen"'))
