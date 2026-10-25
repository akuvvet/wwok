import io
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from pypdf import PdfReader

from apps.kern.models import Mandant
from apps.stammdaten.models import Artikel, Kunde, Steuersatz

from .models import Beleg, BelegGesperrt, Belegart, Position
from .pdf import beleg_pdf


@override_settings(SECURE_SSL_REDIRECT=False)
class MaskeTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.kunde = Kunde.objects.create(nummer="1", name1="Kunde GmbH")
        st = Steuersatz.objects.create(bezeichnung="19", prozent=Decimal("19"))
        self.a = Artikel.objects.create(nummer="A1", bezeichnung="Maus", steuersatz=st, vk1=Decimal("10"))
        self.client.force_login(get_user_model().objects.create_user("t", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()
        self.beleg = Beleg.objects.create(mandant=self.m, art=Belegart.ANGEBOT, kunde=self.kunde)
        self.pos = Position.objects.create(beleg=self.beleg, artikel=self.a, bezeichnung="Maus", menge=Decimal("2"),
                                           einzelpreis=Decimal("10"))

    def test_position_aendern(self):
        url = reverse("verkauf:position_aendern", args=[self.beleg.pk, self.pos.pk])
        self.client.post(url, {"menge": "5", "preis": "9,50", "rabatt": "10", "zusatztext": "mit Kabel"})
        self.pos.refresh_from_db()
        self.beleg.refresh_from_db()
        self.assertEqual((self.pos.menge, self.pos.einzelpreis, self.pos.rabatt_prozent, self.pos.zusatztext),
                         (Decimal("5"), Decimal("9.50"), Decimal("10"), "mit Kabel"))
        self.assertEqual(self.beleg.summe_netto, Decimal("42.75"))  # 5 × 9,50 − 10 %
        self.client.post(url, {"menge": "0", "preis": "1"})  # ungültig
        self.client.post(url, {"menge": "1", "preis": "1", "rabatt": "150"})  # ungültig
        self.pos.refresh_from_db()
        self.assertEqual(self.pos.menge, Decimal("5"))

    def test_gesperrte_rechnung_nicht_aenderbar(self):
        re = Beleg.objects.create(mandant=self.m, art=Belegart.RECHNUNG, kunde=self.kunde)
        p = Position.objects.create(beleg=re, bezeichnung="Dienst", einzelpreis=Decimal("100"))
        re.abschliessen()
        self.client.post(reverse("verkauf:position_aendern", args=[re.pk, p.pk]), {"menge": "9", "preis": "1"})
        p.refresh_from_db()
        self.assertEqual(p.einzelpreis, Decimal("100.00"))
        with self.assertRaises(BelegGesperrt):
            p.save()

    def test_nebenkosten_referenz_und_texte_im_beleg_und_pdf(self):
        self.client.post(reverse("verkauf:beleg_kopf", args=[self.beleg.pk]), {
            "versandart": "DHL", "versandkosten": "5", "verpackung": "2", "versicherung": "1,50",
            "referenz": "PO-4711", "vortext": "Gerne bieten wir an:", "schlusstext": "Gültig 14 Tage.", "bemerkung": "intern: eilig"})
        self.beleg.refresh_from_db()
        self.assertEqual(self.beleg.summe_netto, Decimal("28.50"))  # 20 + 5 + 2 + 1,50
        self.assertEqual(self.beleg.summe_brutto, Decimal("33.92"))
        text = " ".join(PdfReader(io.BytesIO(beleg_pdf(self.beleg))).pages[0].extract_text().split())
        for erwartet in ("PO-4711", "Gerne bieten wir an:", "Gültig 14 Tage.", "Verpackung netto", "Versicherung netto"):
            self.assertIn(erwartet, text)
        self.assertNotIn("eilig", text)  # interne Bemerkung wird nicht gedruckt
        folge = self.beleg.folgebeleg_erzeugen()
        self.assertEqual((folge.referenz, folge.verpackung, folge.schlusstext), ("PO-4711", Decimal("2.00"), "Gültig 14 Tage."))
        seite = self.client.get(reverse("verkauf:beleg", args=[self.beleg.pk]))
        self.assertContains(seite, "Versand &amp; Zahlung")
        self.assertContains(seite, "PO-4711")

    def test_belegliste_spalten_filter_und_seiten(self):
        from apps.finanzen import services
        zb = None
        heute = date.today()
        alt = Beleg.objects.create(mandant=self.m, art=Belegart.RECHNUNG, kunde=self.kunde, belegdatum=heute - timedelta(days=40))
        Position.objects.create(beleg=alt, bezeichnung="Dienst", einzelpreis=Decimal("100"))
        alt.abschliessen()
        services.zahlung_buchen(alt, Decimal("19"), heute, "ue")
        gs = alt.gutschrift_erzeugen()
        for i in range(30):  # mehr als eine Seite
            Position.objects.create(beleg=Beleg.objects.create(mandant=self.m, art=Belegart.ANGEBOT, kunde=self.kunde),
                                    bezeichnung="X", einzelpreis=Decimal("1"))
        url = reverse("verkauf:kunde", args=[self.kunde.pk])
        r = self.client.get(url)
        self.assertEqual(len(r.context["zeilen"]), 25)
        self.assertContains(r, "Seite 1 von 2")
        zeile = next(z for z in self.client.get(url, {"art": "RE"}).context["zeilen"] if z["beleg"] == alt)
        self.assertEqual((zeile["bezahlt"], zeile["offen"]), (Decimal("19.00"), Decimal("100.00")))
        self.assertEqual(gs.vorgaenger, alt)
        neu = self.client.get(url, {"ab": (heute - timedelta(days=10)).isoformat(), "art": "RE"})
        self.assertEqual([z["beleg"] for z in neu.context["zeilen"]], [])  # Rechnung ist älter
        gs.abschliessen()
        ohne = self.client.get(url, {"ohne_storno": "1", "art": ""}).context["seite"].paginator.count
        mit = self.client.get(url).context["seite"].paginator.count
        self.assertEqual(mit - ohne, 2)  # Rechnung und ihre Gutschrift verschwinden
