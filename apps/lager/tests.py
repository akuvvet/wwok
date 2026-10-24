from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.kern.models import Mandant
from apps.stammdaten.models import Artikel, Kunde, Steuersatz
from apps.verkauf.models import Beleg, Belegart, Position

from .models import Bewegungsart, Lagerbewegung, bestand, reserviert
from .services import buche_manuell, inventur


@override_settings(SECURE_SSL_REDIRECT=False)
class LagerTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.m2 = Mandant.objects.create(kurzname="ZONE", firmenname="ZONE Handel")
        self.kunde = Kunde.objects.create(nummer="1", name1="Kunde")
        st = Steuersatz.objects.create(bezeichnung="19", prozent=Decimal("19"))
        self.a = Artikel.objects.create(nummer="A1", bezeichnung="Maus", steuersatz=st, vk1=Decimal("10"))
        self.leistung = Artikel.objects.create(nummer="L1", bezeichnung="Montage", typ="L", steuersatz=st, vk1=Decimal("50"))
        buche_manuell(self.m, self.a, Bewegungsart.WARENEINGANG, Decimal("10"))

    def beleg(self, art, menge="3", vorgaenger=None, mandant=None):
        b = Beleg.objects.create(mandant=mandant or self.m, art=art, kunde=self.kunde, vorgaenger=vorgaenger)
        Position.objects.create(beleg=b, artikel=self.a, bezeichnung="Maus", menge=Decimal(menge), einzelpreis=Decimal("10"))
        Position.objects.create(beleg=b, artikel=self.leistung, bezeichnung="Montage", einzelpreis=Decimal("50"))
        return b

    def test_bestand_pro_mandant(self):
        buche_manuell(self.m2, self.a, Bewegungsart.WARENEINGANG, Decimal("4"))
        self.assertEqual((bestand(self.m, self.a), bestand(self.m2, self.a)), (Decimal("10"), Decimal("4")))

    def test_lieferschein_bucht_ab_und_rechnung_danach_nicht_nochmal(self):
        ls = self.beleg(Belegart.LIEFERSCHEIN)
        ls.abschliessen()
        self.assertEqual(bestand(self.m, self.a), Decimal("7"))  # Leistung wird nicht gebucht
        re = ls.folgebeleg_erzeugen()
        re.abschliessen()
        self.assertEqual(bestand(self.m, self.a), Decimal("7"))
        ls.abschliessen()  # zweiter Abschluss bucht nicht doppelt
        self.assertEqual(Lagerbewegung.objects.filter(art=Bewegungsart.VERKAUF).count(), 1)

    def test_rechnung_ohne_lieferschein_bucht_ab(self):
        re = self.beleg(Belegart.RECHNUNG, "2")
        re.abschliessen()
        self.assertEqual(bestand(self.m, self.a), Decimal("8"))

    def test_reservierung_durch_auftrag(self):
        ab = self.beleg(Belegart.AUFTRAG, "4")
        self.assertEqual(reserviert(self.m, self.a), Decimal("4"))
        ab.folgebeleg_erzeugen()  # Lieferschein erstellt -> keine Reservierung mehr
        self.assertEqual(reserviert(self.m, self.a), Decimal("0"))

    def test_inventur_und_unveraenderlich(self):
        inventur(self.m, self.a, Decimal("6"))
        self.assertEqual(bestand(self.m, self.a), Decimal("6"))
        self.assertIsNone(inventur(self.m, self.a, Decimal("6")))
        with self.assertRaises(PermissionError):
            Lagerbewegung.objects.first().delete()
        buche_manuell(self.m, self.a, Bewegungsart.EIGENVERBRAUCH, Decimal("2"))
        self.assertEqual(bestand(self.m, self.a), Decimal("4"))  # Eigenverbrauch immer Abgang

    def test_masken(self):
        user = get_user_model().objects.create_user("t", password="x" * 14)
        self.client.force_login(user)
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()
        r = self.client.get(reverse("lager:uebersicht"), {"q": "maus"})
        self.assertContains(r, "A1")
        r = self.client.post(reverse("lager:buchen", args=[self.a.pk]),
                             {"art": "WE", "menge": "5", "bemerkung": "Lieferung"})
        self.assertEqual(bestand(self.m, self.a), Decimal("15"))
        self.assertContains(self.client.get(reverse("lager:artikel", args=[self.a.pk])), "Lieferung")


@override_settings(SECURE_SSL_REDIRECT=False)
class LagerErgaenzungTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.m2 = Mandant.objects.create(kurzname="ZONE", firmenname="ZONE Handel")
        self.kunde = Kunde.objects.create(nummer="1", name1="Kunde")
        st = Steuersatz.objects.create(bezeichnung="19", prozent=Decimal("19"))
        self.a = Artikel.objects.create(nummer="N1", bezeichnung="Notebook", steuersatz=st, vk1=Decimal("500"))
        self.client_user = get_user_model().objects.create_user("t", password="x" * 14)

    def login(self):
        self.client.force_login(self.client_user)
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    def test_seriennummern_wareneingang_bis_verkauf_und_retoure(self):
        from .models import Seriennummer
        from .services import retoure_aus_gutschrift, seriennummern_einlagern, seriennummern_zuordnen
        buche_manuell(self.m, self.a, Bewegungsart.WARENEINGANG, Decimal("2"))
        seriennummern_einlagern(self.m, self.a, "SN1\nSN2")
        with self.assertRaises(ValueError):
            seriennummern_einlagern(self.m, self.a, "SN1")
        re = Beleg.objects.create(mandant=self.m, art=Belegart.RECHNUNG, kunde=self.kunde)
        pos = Position.objects.create(beleg=re, artikel=self.a, bezeichnung="Notebook", einzelpreis=Decimal("500"))
        with self.assertRaises(ValueError):
            seriennummern_zuordnen(pos, "SN9")  # nicht im Lager
        with self.assertRaises(ValueError):
            seriennummern_zuordnen(pos, "SN1\nSN2")  # mehr als Menge 1
        seriennummern_zuordnen(pos, "SN2")
        self.assertEqual(Seriennummer.objects.get(nummer="SN2").status, "verkauft")
        re.abschliessen()
        self.assertEqual(bestand(self.m, self.a), Decimal("1"))
        from apps.verkauf.pdf import beleg_pdf
        self.assertTrue(beleg_pdf(re).startswith(b"%PDF"))
        g = re.gutschrift_erzeugen()
        g.abschliessen()
        self.assertEqual(retoure_aus_gutschrift(g), 1)
        self.assertEqual(bestand(self.m, self.a), Decimal("2"))
        with self.assertRaises(ValueError):
            retoure_aus_gutschrift(g)  # nur einmal

    def test_umbuchung_inventurliste_und_masken(self):
        from .services import umbuchen
        buche_manuell(self.m, self.a, Bewegungsart.WARENEINGANG, Decimal("5"))
        umbuchen(self.m, self.m2, self.a, Decimal("2"))
        self.assertEqual((bestand(self.m, self.a), bestand(self.m2, self.a)), (Decimal("3"), Decimal("2")))
        with self.assertRaises(ValueError):
            umbuchen(self.m, self.m, self.a, Decimal("1"))
        self.login()
        r = self.client.get(reverse("lager:inventur_liste"))
        self.assertContains(r, "N1")
        self.client.post(reverse("lager:inventur_liste"), {f"gezaehlt_{self.a.pk}": "4"})
        self.assertEqual(bestand(self.m, self.a), Decimal("4"))
        self.client.post(reverse("lager:buchen", args=[self.a.pk]),
                         {"art": "WE", "menge": "2", "seriennummern": "A\nB\nC"})  # falsche Anzahl
        self.assertEqual(bestand(self.m, self.a), Decimal("4"))
        self.client.post(reverse("lager:buchen", args=[self.a.pk]), {"art": "WE", "menge": "2", "seriennummern": "A\nB"})
        self.assertEqual(bestand(self.m, self.a), Decimal("6"))
        self.assertContains(self.client.get(reverse("lager:seriennummern"), {"q": "A"}), "Notebook")
        self.assertContains(self.client.get(reverse("lager:artikel", args=[self.a.pk])), "Seriennummern")
