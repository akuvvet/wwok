import io
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from pypdf import PdfReader

from apps.kern.models import Mandant
from apps.lager.models import Bewegungsart
from apps.lager.services import buche_manuell
from apps.stammdaten.models import Artikel, Kunde, Steuersatz
from apps.verkauf.models import Beleg, Belegart, Position
from apps.verkauf.pdf import beleg_pdf

from . import services
from .models import ESDKey, KeyStatus, VergabeStatus


@override_settings(SECURE_SSL_REDIRECT=False)
class ESDTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.m2 = Mandant.objects.create(kurzname="ZONE", firmenname="ZONE Handel")
        self.k1 = Kunde.objects.create(nummer="1", name1="Kunde Eins", email="eins@example.test")
        self.k2 = Kunde.objects.create(nummer="2", name1="Kunde Zwei")
        st = Steuersatz.objects.create(bezeichnung="19", prozent=Decimal("19"))
        self.win = Artikel.objects.create(nummer="WIN11", bezeichnung="Windows 11 Pro", steuersatz=st, vk1=Decimal("120"), esd_key=True)
        self.user = get_user_model().objects.create_user("t", password="x" * 14)
        buche_manuell(self.m, self.win, Bewegungsart.WARENEINGANG, Decimal("50"))  # Rechnungen brauchen Lagerbestand

    def beleg(self, kunde=None, art=Belegart.RECHNUNG, menge="1"):
        b = Beleg.objects.create(mandant=self.m, art=art, kunde=kunde or self.k1)
        Position.objects.create(beleg=b, artikel=self.win, bezeichnung="Windows 11 Pro", menge=Decimal(menge), einzelpreis=Decimal("120"))
        return b

    def test_import_und_bestand_pro_mandant(self):
        neu, doppelt = services.importieren(self.m, self.win, "KEY-A\nKEY-B\n\nVOL-1;3\nKEY-A")
        self.assertEqual((neu, doppelt), (3, 1))
        services.importieren(self.m2, self.win, "KEY-A")  # gleicher Key bei anderem Mandanten ist ein anderer Bestand
        self.assertEqual(services.bestand(self.m, self.win), {"keys": 3, "frei": 5, "gesperrt": 0, "vergeben": 0})
        self.assertEqual(services.bestand(self.m2, self.win)["frei"], 1)

    def test_automatische_zuordnung_beim_abschluss_und_pdf(self):
        services.importieren(self.m, self.win, "KEY-A\nKEY-B")
        b = self.beleg(menge="2")
        b.abschliessen()
        self.assertEqual(sorted(v.key.key for v in b.esd_vergaben.all()), ["KEY-A", "KEY-B"])
        self.assertEqual(services.bestand(self.m, self.win)["frei"], 0)
        text = " ".join(PdfReader(io.BytesIO(beleg_pdf(b))).pages[0].extract_text().split())
        self.assertIn("Lizenzschlüssel: KEY-A, KEY-B", text)

    def test_zu_wenig_keys_verhindert_abschluss(self):
        services.importieren(self.m, self.win, "KEY-A")
        b = self.beleg(menge="2")
        with self.assertRaises(ValueError):
            b.abschliessen()
        b.refresh_from_db()
        self.assertFalse(b.abgeschlossen)  # alles zurückgerollt
        self.assertEqual(services.bestand(self.m, self.win)["frei"], 1)
        self.assertEqual(b.esd_vergaben.count(), 0)

    def test_volumenkey_begrenzt_und_nicht_doppelt_an_denselben_kunden(self):
        services.importieren(self.m, self.win, "VOL-1;2")
        b1 = self.beleg(self.k1)
        b1.abschliessen()
        b2 = self.beleg(self.k1)  # derselbe Kunde: Volumenkey darf nicht noch einmal an ihn gehen
        with self.assertRaises(ValueError) as fehler:
            b2.abschliessen()
        self.assertIn("Verkäufer", str(fehler.exception))
        pos = b2.positionen.get()
        with self.assertRaises(ValueError):
            services.zuordnen(pos, 1, erlaube_doppelt=True, bemerkung="")  # Begründung fehlt
        services.zuordnen(pos, 1, erlaube_doppelt=True, bemerkung="Kunde hat Key verloren")  # bewusste Entscheidung
        key = ESDKey.objects.get()
        self.assertEqual((key.vergeben, key.frei), (2, 0))
        b3 = self.beleg(self.k2)  # Volumenkey ist erschöpft
        with self.assertRaises(ValueError):
            b3.abschliessen()

    def test_lieferschein_dann_rechnung_vergibt_nicht_doppelt(self):
        services.importieren(self.m, self.win, "KEY-A\nKEY-B")
        buche_manuell(self.m, self.win, Bewegungsart.WARENEINGANG, Decimal("5"))
        ls = self.beleg(art=Belegart.LIEFERSCHEIN)
        ls.abschliessen()
        self.assertEqual(ls.esd_vergaben.count(), 1)
        re = ls.folgebeleg_erzeugen()
        re.abschliessen()
        self.assertEqual(re.esd_vergaben.count(), 0)  # Key hängt am Lieferschein
        self.assertEqual(services.bestand(self.m, self.win)["frei"], 1)
        text = " ".join(PdfReader(io.BytesIO(beleg_pdf(re))).pages[0].extract_text().split())
        self.assertIn("Lizenzschlüssel: KEY-A", text)  # steht trotzdem auf der Rechnung

    def test_retoure_freigeben_oder_sperren(self):
        services.importieren(self.m, self.win, "KEY-A\nKEY-B")
        b = self.beleg(menge="2")
        b.abschliessen()
        v1, v2 = list(b.esd_vergaben.order_by("id"))
        services.rueckgabe(v1, "freigeben")
        services.rueckgabe(v2, "sperren")
        self.assertEqual((v1.status, v2.status), (VergabeStatus.FREIGEGEBEN, VergabeStatus.GESPERRT))
        self.assertEqual(services.bestand(self.m, self.win), {"keys": 2, "frei": 1, "gesperrt": 1, "vergeben": 1})
        with self.assertRaises(ValueError):
            services.rueckgabe(v1, "freigeben")  # nur einmal
        b2 = self.beleg(self.k2)
        b2.abschliessen()
        self.assertEqual(b2.esd_vergaben.get().key, v1.key)  # freigegebener Key ist wieder vergebbar
        self.assertEqual(ESDKey.objects.get(pk=v2.key_id).status, KeyStatus.GESPERRT)

    def test_masken_und_mail(self):
        self.client.force_login(self.user)
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()
        r = self.client.post(reverse("esd:artikel", args=[self.win.pk]), {"keys": "K1\nK2", "max_vergaben": "1"})
        self.assertEqual(ESDKey.objects.count(), 2)
        self.assertContains(self.client.get(reverse("esd:uebersicht")), "WIN11")
        self.assertContains(self.client.get(reverse("esd:artikel", args=[self.win.pk])), "K1")
        b = self.beleg()
        self.client.post(reverse("esd:zuordnen", args=[b.pk]), {"position": b.positionen.get().pk, "anzahl": "1"})
        self.assertEqual(b.esd_vergaben.count(), 1)
        self.assertContains(self.client.get(reverse("esd:beleg", args=[b.pk])), "Key freigeben")
        self.assertContains(self.client.get(reverse("esd:mail", args=[b.pk])), "K1")
        b.abschliessen()  # bereits zugeordnete Position bleibt unverändert
        self.assertEqual(b.esd_vergaben.count(), 1)
        self.assertContains(self.client.get(reverse("verkauf:beleg", args=[b.pk])), "ESD-Keys")
        key = ESDKey.objects.get(key="K2")
        self.client.post(reverse("esd:key_status", args=[key.pk]))
        key.refresh_from_db()
        self.assertEqual(key.status, KeyStatus.GESPERRT)


@override_settings(SECURE_SSL_REDIRECT=False)
class EsdSucheTest(TestCase):
    def test_suchfeld_liefert_nur_software(self):
        from apps.kern.models import Mandant
        from apps.stammdaten.models import Artikel, Kategorie, Steuersatz
        m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        st = Steuersatz.objects.create(bezeichnung="19", prozent=Decimal("19"))
        sw = Kategorie.objects.create(name="Software")
        hw = Kategorie.objects.create(name="Hardware")
        Artikel.objects.create(nummer="S1", bezeichnung="Windows 11 Pro", steuersatz=st, kategorie=sw)
        Artikel.objects.create(nummer="H1", bezeichnung="Windows Tastatur", steuersatz=st, kategorie=hw)
        self.client.force_login(get_user_model().objects.create_user("t", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = m.pk
        s.save()
        url = reverse("esd:suche")
        self.assertEqual([t["nummer"] for t in self.client.get(url, {"q": "windows"}).json()["treffer"]], ["S1"])
        self.assertEqual([t["nummer"] for t in self.client.get(url, {"q": "*"}).json()["treffer"]], ["S1"])
        self.assertEqual(self.client.get(url, {"q": "w"}).json()["treffer"], [])


@override_settings(SECURE_SSL_REDIRECT=False)
class CsvImportTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        st = Steuersatz.objects.create(bezeichnung="19", prozent=Decimal("19"))
        self.a = Artikel.objects.create(nummer="21882", bezeichnung="Office", steuersatz=st)
        self.user = get_user_model().objects.create_user("t", password="x" * 14)
        self.client.force_login(self.user)
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    def test_import_mit_mandant_artikel_key_anzahl(self):
        daten = "OKAY;21882;AAA-1;1\nOKAY;21882;AAA-2;5\n".encode()
        self.assertEqual(services.csv_importieren(daten), (2, 0, 1))
        self.assertEqual(services.csv_importieren(daten), (0, 2, 1))
        self.a.refresh_from_db()
        self.assertTrue(self.a.esd_key)
        self.assertEqual(ESDKey.objects.get(key="AAA-2").max_vergaben, 5)

    def test_fehler_bricht_alles_ab(self):
        daten = "OKAY;21882;AAA-1;1\nXXX;21882;AAA-2;1\nOKAY;999;AAA-3;1\nOKAY;21882;AAA-4;0\n".encode()
        with self.assertRaises(services.CsvFehler) as ctx:
            services.csv_importieren(daten)
        self.assertEqual(len(ctx.exception.fehler), 3)
        self.assertEqual(ESDKey.objects.count(), 0)

    def test_kopfzeile_und_upload(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        datei = SimpleUploadedFile("k.csv", "Mandant;Artikel;Key;Anzahl\nOKAY;21882;K-1;2\n".encode("cp1252"))
        r = self.client.post(reverse("esd:csv_import"), {"datei": datei}, follow=True)
        self.assertContains(r, "1 Key(s) für 1 Artikel importiert")
