"""Bestellung → Lieferschein: Markierung „L“ an der Bestellung; ohne Preislistenartikel entsteht kein Angebot."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.kern.models import Mandant
from apps.lager.models import Bewegungsart
from apps.lager.services import buche_manuell
from apps.stammdaten.models import Artikel, Kunde, Steuersatz

from .models import Beleg, Belegart, Position


@override_settings(SECURE_SSL_REDIRECT=False)
class LieferscheinMarkeTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.kunde = Kunde.objects.create(nummer="1", name1="Muster GmbH")
        st = Steuersatz.objects.create(bezeichnung="19", prozent=Decimal("19"))
        self.art = Artikel.objects.create(nummer="A1", bezeichnung="Maus", steuersatz=st, vk1=Decimal("10"))
        buche_manuell(self.m, self.art, Bewegungsart.WARENEINGANG, Decimal("10"))
        self.client.force_login(get_user_model().objects.create_user("t", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    def bestellung(self):
        ab = Beleg.objects.create(mandant=self.m, art=Belegart.AUFTRAG, kunde=self.kunde)
        Position.objects.create(beleg=ab, artikel=self.art, typ="A", bezeichnung="Maus", menge=Decimal("2"),
                                einzelpreis=Decimal("10"), steuer_prozent=Decimal("19"))
        ab.berechne()
        return ab

    def test_ohne_p_artikel_entsteht_kein_angebot(self):
        ab = self.bestellung()
        self.client.post(reverse("verkauf:beleg_folgebeleg", args=[ab.pk]))
        self.assertEqual(Beleg.objects.filter(art=Belegart.LIEFERSCHEIN, vorgaenger=ab).count(), 1)
        self.assertFalse(Beleg.objects.filter(art=Belegart.ANGEBOT).exists())

    def test_bestellung_bekommt_markierung_l_in_kopf_und_listen(self):
        ab = self.bestellung()
        seite = self.client.get(reverse("verkauf:beleg", args=[ab.pk]))
        self.assertNotContains(seite, "Lieferschein aus dieser Bestellung")
        self.assertNotContains(self.client.get(reverse("verkauf:belege"), {"status": ""}), 'aria-label="Lieferschein erstellt"')
        ls = ab.folgebeleg_erzeugen()
        ls_url = reverse("verkauf:beleg", args=[ls.pk])
        kopf = self.client.get(reverse("verkauf:beleg", args=[ab.pk]))
        self.assertContains(kopf, f'href="{ls_url}" title="Lieferschein aus dieser Bestellung">L · {ls.nummer or "(Entwurf)"}</a>')
        for url, daten in ((reverse("verkauf:belege"), {"status": ""}), (reverse("verkauf:kunde", args=[self.kunde.pk]), {})):
            with self.subTest(url=url):
                r = self.client.get(url, daten)
                self.assertContains(r, 'aria-label="Lieferschein erstellt"', count=1)  # nur an der Bestellung, nicht am Lieferschein
                self.assertContains(r, f'href="{ls_url}" class="marke ok"')

    def test_andere_belegarten_ohne_markierung(self):
        an = Beleg.objects.create(mandant=self.m, art=Belegart.ANGEBOT, kunde=self.kunde)
        self.assertEqual(an.lieferscheine_aus_bestellung, [])
        self.assertEqual(an.rechnungen_aus_bestellung, [])
        markiert = Beleg.versand_markieren([an])
        self.assertEqual(markiert[0].lieferscheine_aus_bestellung, [])
        self.assertEqual(markiert[0].rechnungen_aus_bestellung, [])

    def test_bestellung_direkt_in_rechnung_bekommt_markierung_r(self):
        ab = self.bestellung()
        self.assertNotContains(self.client.get(reverse("verkauf:beleg", args=[ab.pk])), "Rechnung aus dieser Bestellung")
        re = ab.kopieren_als(Belegart.RECHNUNG)
        self.assertEqual(re.vorgaenger_id, ab.pk)
        re_url = reverse("verkauf:beleg", args=[re.pk])
        kopf = self.client.get(reverse("verkauf:beleg", args=[ab.pk]))
        self.assertContains(kopf, f'href="{re_url}" title="Rechnung aus dieser Bestellung">R · {re.nummer or "(Entwurf)"}</a>')
        self.assertNotContains(kopf, "Lieferschein aus dieser Bestellung")
        for url, daten in ((reverse("verkauf:belege"), {"status": ""}), (reverse("verkauf:kunde", args=[self.kunde.pk]), {})):
            with self.subTest(url=url):
                r = self.client.get(url, daten)
                self.assertContains(r, 'aria-label="Rechnung erstellt"', count=1)  # nur an der Bestellung
                self.assertNotContains(r, 'aria-label="Lieferschein erstellt"')
                self.assertContains(r, f'href="{re_url}" class="marke ok"')

    def test_lieferschein_und_rechnung_beide_markiert_rechnung_aus_ls_zaehlt_nicht(self):
        ab = self.bestellung()
        ls = ab.folgebeleg_erzeugen()
        aus_ls = Beleg.objects.create(mandant=self.m, art=Belegart.RECHNUNG, kunde=self.kunde, vorgaenger=ls)
        markiert = Beleg.versand_markieren([ab])[0]
        self.assertEqual([x.pk for x in markiert.lieferscheine_aus_bestellung], [ls.pk])
        self.assertEqual(markiert.rechnungen_aus_bestellung, [])  # Rechnung aus dem Lieferschein zählt nicht
        # kopieren_als sperrt eine zweite Abrechnung; die Anzeige muss beide Markierungen trotzdem nebeneinander können
        direkt = Beleg.objects.create(mandant=self.m, art=Belegart.RECHNUNG, kunde=self.kunde, vorgaenger=ab)
        markiert = Beleg.versand_markieren([Beleg.objects.get(pk=ab.pk)])[0]
        self.assertEqual([x.pk for x in markiert.rechnungen_aus_bestellung], [direkt.pk])
        self.assertNotIn(aus_ls.pk, [x.pk for x in markiert.rechnungen_aus_bestellung])
        r = self.client.get(reverse("verkauf:belege"), {"status": ""})
        self.assertContains(r, 'aria-label="Lieferschein erstellt"', count=1)
        self.assertContains(r, 'aria-label="Rechnung erstellt"', count=1)
