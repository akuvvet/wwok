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

    def test_rechnung_aus_lieferschein_bucht_nur_die_mehrmenge(self):
        st = self.a.steuersatz
        kabel = Artikel.objects.create(nummer="K1", bezeichnung="Kabel", steuersatz=st, vk1=Decimal("3"))
        buche_manuell(self.m, kabel, Bewegungsart.WARENEINGANG, Decimal("2"))
        ls = self.lieferschein(menge="2")  # Maus 20 → 18
        r = rechnung_aus_lieferscheinen([ls])
        p = r.positionen.get()
        p.menge = Decimal("5")  # 3 Stück mehr als geliefert
        p.save()
        neu = Position.objects.create(beleg=r, artikel=kabel, bezeichnung="Kabel", menge=Decimal("3"), einzelpreis=Decimal("3"))
        from apps.lager.services import druckhindernis
        self.assertIn("K1", druckhindernis(r))  # nur 2 Kabel da
        with self.assertRaises(ValueError):
            r.abschliessen()
        r.refresh_from_db()
        self.assertFalse(r.abgeschlossen)
        self.assertEqual(bestand(self.m, self.a), Decimal("18"))
        neu.menge = Decimal("2")
        neu.save()
        r.abschliessen()
        self.assertEqual(bestand(self.m, self.a), Decimal("15"))  # nur die 3 zusätzlichen Mäuse
        self.assertEqual(bestand(self.m, kabel), Decimal("0"))  # in der Rechnung ergänzter Artikel wird abgebucht

    def test_zwei_rechnungen_zu_einem_lieferschein_buchen_nicht_doppelt(self):
        ls = self.lieferschein(menge="5")  # 20 → 15
        r1 = Beleg.objects.create(mandant=self.m, art=Belegart.RECHNUNG, kunde=self.kunde, vorgaenger=ls)
        Position.objects.create(beleg=r1, artikel=self.a, bezeichnung="Maus", menge=Decimal("2"), einzelpreis=Decimal("10"))
        r2 = Beleg.objects.create(mandant=self.m, art=Belegart.RECHNUNG, kunde=self.kunde, vorgaenger=ls)
        Position.objects.create(beleg=r2, artikel=self.a, bezeichnung="Maus", menge=Decimal("4"), einzelpreis=Decimal("10"))
        r1.abschliessen()
        self.assertEqual(bestand(self.m, self.a), Decimal("15"))  # 2 von 5 gelieferten
        r2.abschliessen()
        self.assertEqual(bestand(self.m, self.a), Decimal("14"))  # 2 + 4 = 6, eine mehr als geliefert

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
        self.assertIsNone(ab.loeschhindernis())  # Urbeleg darf nach dem Übernehmen gelöscht werden
        self.client.post(reverse("verkauf:beleg_loeschen", args=[ab.pk]))
        self.assertFalse(Beleg.objects.filter(pk=ab.pk).exists())

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


class TeillieferungSammelrechnungTest(TestCase):
    def test_teillieferung_und_nachlieferung_in_einer_rechnung(self):
        m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        kunde = Kunde.objects.create(nummer="1", name1="Kunde GmbH")
        st = Steuersatz.objects.create(bezeichnung="19", prozent=Decimal("19"))
        maus = Artikel.objects.create(nummer="A1", bezeichnung="Maus", steuersatz=st, vk1=Decimal("10"))
        tast = Artikel.objects.create(nummer="A2", bezeichnung="Tastatur", steuersatz=st, vk1=Decimal("30"))
        buche_manuell(m, maus, Bewegungsart.WARENEINGANG, Decimal("5"))
        ls = Beleg.objects.create(mandant=m, art=Belegart.LIEFERSCHEIN, kunde=kunde, versandkosten=Decimal("5"))
        Position.objects.create(beleg=ls, artikel=maus, bezeichnung="Maus", menge=Decimal("2"), einzelpreis=Decimal("10"))
        Position.objects.create(beleg=ls, artikel=tast, bezeichnung="Tastatur", menge=Decimal("1"), einzelpreis=Decimal("30"))
        teil = ls.teillieferung_abschliessen()  # Maus geliefert, Tastatur bleibt offen
        self.assertTrue(teil.abgeschlossen)
        ls.refresh_from_db()
        self.assertEqual((ls.abgeschlossen, ls.positionen.count()), (False, 1))
        buche_manuell(m, tast, Bewegungsart.WARENEINGANG, Decimal("1"))
        ls.abschliessen()  # Nachlieferung
        self.assertTrue(teil.abrechenbar and ls.abrechenbar)
        r = rechnung_aus_lieferscheinen([teil, ls])
        self.assertEqual((r.positionen.count(), r.summe_netto), (2, Decimal("55.00")))  # 20 + 30 + 5 Versand (nur einmal)
        r.abschliessen()
        self.assertEqual((bestand(m, maus), bestand(m, tast)), (Decimal("3"), Decimal("0")))
