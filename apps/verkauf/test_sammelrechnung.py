from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.kern.models import Mandant
from apps.lager.models import Bewegungsart, bestand
from apps.lager.services import buche_manuell
from apps.stammdaten.models import Artikel, Kunde, Steuersatz

from .mail import MailFehler, beleg_senden
from .models import Beleg, Belegart, Position, rechnung_aus_lieferscheinen
from .pdf import beleg_pdf


@override_settings(SECURE_SSL_REDIRECT=False)
class SammelrechnungUndLoeschenTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.kunde = Kunde.objects.create(nummer="1", name1="Kunde GmbH")
        self.andere = Kunde.objects.create(nummer="2", name1="Anderer")
        st = Steuersatz.objects.create(bezeichnung="19", prozent=Decimal("19"))
        self.a = Artikel.objects.create(nummer="A1", bezeichnung="Maus", steuersatz=st, vk1=Decimal("10"))
        buche_manuell(self.m, self.a, Bewegungsart.WARENEINGANG, Decimal("20"))
        self.user = get_user_model().objects.create_user("t", password="x" * 14)
        self.client.force_login(self.user)
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    def lieferschein(self, kunde=None, menge="2", datum=None):
        ls = Beleg.objects.create(mandant=self.m, art=Belegart.LIEFERSCHEIN, kunde=kunde or self.kunde,
                                  belegdatum=datum or date(2026, 3, 1))
        Position.objects.create(beleg=ls, artikel=self.a, bezeichnung="Maus", menge=Decimal(menge), einzelpreis=Decimal("10"))
        ls.abschliessen()
        return ls

    def test_mehrere_lieferscheine_in_eine_rechnung(self):
        ls1 = self.lieferschein(menge="2", datum=date(2026, 3, 1))
        ls2 = self.lieferschein(menge="3", datum=date(2026, 3, 5))
        fremd = self.lieferschein(kunde=self.andere)
        self.assertEqual(bestand(self.m, self.a), Decimal("13"))
        self.assertTrue(ls1.abrechenbar and ls2.abrechenbar)
        with self.assertRaises(ValueError):
            rechnung_aus_lieferscheinen([ls1, fremd])  # anderer Kunde
        r = rechnung_aus_lieferscheinen([ls2, ls1])
        self.assertEqual((r.art, r.positionen.count(), r.summe_netto), (Belegart.RECHNUNG, 2, Decimal("50.00")))
        self.assertEqual([p.lieferschein for p in r.positionen.order_by("pos")], [ls1, ls2])  # nach Datum sortiert
        self.assertFalse(ls1.abrechenbar or ls2.abrechenbar)
        with self.assertRaises(ValueError):
            rechnung_aus_lieferscheinen([ls1])  # nicht noch einmal
        r.abschliessen()
        self.assertEqual(bestand(self.m, self.a), Decimal("13"))  # Rechnung bucht nicht erneut ab
        self.assertTrue(beleg_pdf(r).startswith(b"%PDF"))
        self.assertContains(self.client.get(reverse("verkauf:beleg", args=[r.pk])), f"Lieferschein {ls1.nummer} vom 01.03.2026")

    def test_seriennummern_und_lieferschein_im_pdf(self):
        from apps.lager.services import seriennummern_einlagern, seriennummern_zuordnen
        seriennummern_einlagern(self.m, self.a, "SN-77")
        ls = Beleg.objects.create(mandant=self.m, art=Belegart.LIEFERSCHEIN, kunde=self.kunde, belegdatum=date(2026, 3, 1))
        pos = Position.objects.create(beleg=ls, artikel=self.a, bezeichnung="Maus", einzelpreis=Decimal("10"))
        seriennummern_zuordnen(pos, "SN-77")
        ls.abschliessen()
        r = rechnung_aus_lieferscheinen([ls])
        kopie = r.positionen.get()
        self.assertEqual(kopie.quellposition, pos)
        import io

        from pypdf import PdfReader
        text = PdfReader(io.BytesIO(beleg_pdf(r))).pages[0].extract_text()
        self.assertIn("SN-77", text)  # Seriennummer aus dem Lieferschein steht in der Rechnung
        self.assertIn(f"Lieferschein {ls.nummer} vom 01.03.2026", " ".join(text.split()))

    def test_maske_lieferscheine_abrechnen(self):
        ls1, ls2 = self.lieferschein(), self.lieferschein()
        seite = self.client.get(reverse("verkauf:kunde", args=[self.kunde.pk]))
        self.assertContains(seite, 'name="auswahl"')
        r = self.client.post(reverse("verkauf:lieferscheine_abrechnen", args=[self.kunde.pk]), {"auswahl": [ls1.pk, ls2.pk]})
        rechnung = Beleg.objects.get(art=Belegart.RECHNUNG)
        self.assertRedirects(r, reverse("verkauf:beleg", args=[rechnung.pk]))
        self.assertEqual(rechnung.lieferscheine.count(), 2)

    def test_loeschen_regeln(self):
        an = Beleg.objects.create(mandant=self.m, art=Belegart.ANGEBOT, kunde=self.kunde)
        self.assertIsNone(an.loeschhindernis())
        self.client.post(reverse("verkauf:beleg_loeschen", args=[an.pk]))
        self.assertFalse(Beleg.objects.filter(pk=an.pk).exists())

        ab = Beleg.objects.create(mandant=self.m, art=Belegart.AUFTRAG, kunde=self.kunde)
        Position.objects.create(beleg=ab, artikel=self.a, bezeichnung="Maus", einzelpreis=Decimal("10"))
        ab.folgebeleg_erzeugen()
        self.assertIn("Folgebelege", ab.loeschhindernis())
        self.client.post(reverse("verkauf:beleg_loeschen", args=[ab.pk]))
        self.assertTrue(Beleg.objects.filter(pk=ab.pk).exists())

        entwurf = Beleg.objects.create(mandant=self.m, art=Belegart.RECHNUNG, kunde=self.kunde)
        self.assertIsNone(entwurf.loeschhindernis())  # nicht nummerierter Rechnungsentwurf
        fertig = self.lieferschein()
        self.assertIn("nicht gelöscht", fertig.loeschhindernis())
        re = fertig.folgebeleg_erzeugen()
        re.abschliessen()
        self.assertIn("Storno", re.loeschhindernis())
        self.client.post(reverse("verkauf:beleg_loeschen", args=[re.pk]))
        self.assertTrue(Beleg.objects.filter(pk=re.pk).exists())

    def test_beleg_ohne_position(self):
        leer = Beleg.objects.create(mandant=self.m, art=Belegart.AUFTRAG, kunde=self.kunde)
        with self.assertRaises(ValueError):
            leer.folgebeleg_erzeugen()
        with self.assertRaises(ValueError):
            leer.abschliessen()
        with self.assertRaises(MailFehler):
            beleg_senden(leer, "x@example.test", "s", "t")
