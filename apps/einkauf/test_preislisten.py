import io
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.kern.models import Mandant
from apps.stammdaten.models import Artikel, ArtikelLieferant, Lieferant, Preislistenartikel, Steuersatz

from . import preislisten
from .models import Preislistenprofil

CSV = (
    "Best.-Nr.;Herst.-Nr.;EAN;Bezeichnung;Hersteller;EK;Bestand;Lieferzeit\n"
    "TD-1;MX-100;4012345678901;Maus Logitech MX;Logitech;12,50;40;2\n"      # passt zu eigenem Artikel (Herstellernummer)
    "TD-2;;4099999999999;Tastatur Cherry;Cherry;1.234,56;5;3\n"             # nur Preisliste, Tausendertrenner
    "TD-3;XX-3;;Kabel;;abc;1;1\n"                                           # Fehler: EK
    ";;;ohne Bestellnummer;;1,00;1;1\n"                                     # Fehler: Bestellnummer fehlt
    "TD-5;;4012345678901;Maus per EAN;;11,90;;\n"                           # passt zu eigenem Artikel über die EAN
)


def profil(lieferant, **kw):
    daten = dict(lieferant=lieferant, name="Hauptliste", dateityp="csv", zeichensatz="utf-8", trennzeichen=";",
                 dezimaltrennzeichen=",", sp_bestellnummer="Best.-Nr.", sp_herstellernummer="Herst.-Nr.", sp_ean="EAN",
                 sp_bezeichnung="Bezeichnung", sp_hersteller="Hersteller", sp_ek="EK", sp_bestand="Bestand",
                 sp_lieferzeit="Lieferzeit")
    daten.update(kw)
    return Preislistenprofil.objects.create(**daten)


class PreislistenTest(TestCase):
    def setUp(self):
        self.lf = Lieferant.objects.create(nummer="L1", name1="Techdata GmbH", kurzname="Techdata")
        st = Steuersatz.objects.get(nummer=1)  # per Migration vorbelegt (19 %)
        self.maus = Artikel.objects.create(nummer="M1", bezeichnung="Maus", herstellernummer="MX-100",
                                           ean="4012345678901", steuersatz=st)

    def test_vorschau_schreibt_nichts(self):
        p = profil(self.lf)
        v = preislisten.vorschau(p, CSV.encode())
        self.assertEqual((v["gesamt"], v["zeilen"][0]["artikel"], v["zeilen"][1]["ergebnis"]), (5, self.maus, "neu in der Preisliste"))
        self.assertEqual(Preislistenartikel.objects.count(), 0)
        self.assertEqual(ArtikelLieferant.objects.count(), 0)

    def test_import_in_eigene_tabelle_mit_abgleich_und_fehlern(self):
        p = profil(self.lf)
        prot = preislisten.importieren(p, CSV.encode(), "liste.csv")
        self.assertEqual((prot.gelesen, prot.neu, prot.fehler), (5, 3, 2))
        self.assertEqual({f["zeile"] for f in prot.fehlerzeilen}, {4, 5})
        self.assertEqual(Artikel.objects.count(), 1)  # Preislistenartikel werden NICHT zu eigenen Artikeln
        self.assertEqual(Preislistenartikel.objects.count(), 3)
        e1 = Preislistenartikel.objects.get(bestellnummer="TD-1")
        self.assertEqual((e1.artikel, e1.ek, e1.bestand, e1.lieferzeit_tage, e1.hersteller, e1.lieferant), (self.maus, Decimal("12.5000"), 40, 2, "Logitech", self.lf))
        self.assertEqual(Preislistenartikel.objects.get(bestellnummer="TD-5").artikel, self.maus)  # Abgleich über EAN
        e2 = Preislistenartikel.objects.get(bestellnummer="TD-2")
        self.assertEqual((e2.artikel, e2.ek, e2.bezeichnung, e2.nummer), (None, Decimal("1234.5600"), "Tastatur Cherry", "TD-2"))
        # zum eigenen Artikel wird der Lieferantenpreis mitgeführt, zu reinen Preislistenartikeln nicht
        self.assertEqual(sorted(ArtikelLieferant.objects.values_list("bestellnummer", flat=True)), ["TD-1", "TD-5"])
        # zweiter Lauf mit geändertem Preis: nichts doppelt, nur geändert/unverändert
        prot2 = preislisten.importieren(p, CSV.replace("12,50", "13,00").encode(), "liste.csv")
        self.assertEqual((prot2.neu, prot2.geaendert, prot2.unveraendert, prot2.fehler), (0, 1, 2, 2))
        self.assertEqual(Preislistenartikel.objects.get(bestellnummer="TD-1").ek, Decimal("13.0000"))
        self.assertEqual(ArtikelLieferant.objects.get(bestellnummer="TD-1").ek, Decimal("13.0000"))
        self.assertEqual(Preislistenartikel.objects.count(), 3)
        p.refresh_from_db()
        self.assertIsNotNone(p.letzter_lauf)

    def test_ohne_neue_artikel_nur_passende_uebernehmen(self):
        p = profil(self.lf, neue_artikel=False)
        prot = preislisten.importieren(p, CSV.encode())
        self.assertEqual((prot.neu, prot.uebersprungen), (2, 1))
        self.assertFalse(Preislistenartikel.objects.filter(bestellnummer="TD-2").exists())

    def test_spaltennummern_ohne_kopfzeile_und_punkt_dezimal_ansi(self):
        text = "TD-9|MX-100|9.99|7|Maus neu\n"
        p = profil(self.lf, trennzeichen="|", zeichensatz="cp1252", dezimaltrennzeichen=".", kopfzeile=False, erste_datenzeile=1,
                   sp_bestellnummer="1", sp_herstellernummer="2", sp_ean="", sp_bezeichnung="5", sp_hersteller="", sp_ek="3",
                   sp_bestand="4", sp_lieferzeit="")
        prot = preislisten.importieren(p, text.encode("cp1252"))
        self.assertEqual((prot.neu, prot.fehler), (1, 0))
        e = Preislistenartikel.objects.get()
        self.assertEqual((e.ek, e.artikel, e.bezeichnung), (Decimal("9.9900"), self.maus, "Maus neu"))

    def test_excel_und_xml(self):
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.append(["Best.-Nr.", "Herst.-Nr.", "EAN", "Bezeichnung", "Hersteller", "EK", "Bestand", "Lieferzeit"])
        ws.append(["X-1", "MX-100", "", "Maus", "", 11.5, 3, 1])
        puffer = io.BytesIO()
        wb.save(puffer)
        p = profil(self.lf, dateityp="xlsx")
        self.assertEqual(preislisten.importieren(p, puffer.getvalue()).neu, 1)
        self.assertEqual(Preislistenartikel.objects.get(bestellnummer="X-1").ek, Decimal("11.5000"))
        xml = b"""<?xml version="1.0"?><liste><artikel><nr>Y-1</nr><hnr>MX-100</hnr><titel>Maus XML</titel><preis>8,40</preis><menge>2</menge></artikel>
                  <artikel><nr>Y-2</nr><titel>Defekt</titel><preis>x</preis></artikel></liste>"""
        px = profil(self.lf, name="XML", dateityp="xml", xml_satz="artikel", sp_bestellnummer="nr", sp_herstellernummer="hnr",
                    sp_ean="", sp_bezeichnung="titel", sp_hersteller="", sp_ek="preis", sp_bestand="menge", sp_lieferzeit="")
        prot = preislisten.importieren(px, xml)
        self.assertEqual((prot.neu, prot.fehler), (1, 1))

    def test_falsche_spalte_und_zeichensatz_geben_klare_fehler(self):
        p = profil(self.lf, sp_ek="Preis")
        with self.assertRaises(preislisten.PreislistenFehler):
            preislisten.importieren(p, CSV.encode())
        p2 = profil(self.lf, name="ANSI")
        with self.assertRaises(preislisten.PreislistenFehler):
            preislisten.importieren(p2, "Best.-Nr.;EK\nä;1".encode("cp1252"))

    def test_faellige_profile_und_abruf(self):
        p = profil(self.lf, name="FTP", quelle="ftp", adresse="ftp.example.test", pfad="/preise/liste.csv",
                   benutzer="u", intervall_stunden=6)
        self.assertIn(p, list(preislisten.faellige_profile()))
        p.letzter_lauf = timezone.now()
        p.save()
        self.assertNotIn(p, list(preislisten.faellige_profile()))
        with mock.patch("apps.einkauf.preislisten.abrufen", return_value=("liste.csv", CSV.encode())):
            from django.core.management import call_command
            ausgabe = io.StringIO()
            call_command("preislisten_abrufen", "--alle", stdout=ausgabe)
        self.assertIn("3 neu", ausgabe.getvalue())


@override_settings(SECURE_SSL_REDIRECT=False)
class PreislistenMaskeTest(TestCase):
    def test_vorschau_import_protokoll_und_suche(self):
        m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        lf = Lieferant.objects.create(nummer="L1", name1="Techdata GmbH", kurzname="Techdata")
        p = profil(lf)
        self.client.force_login(get_user_model().objects.create_user("t", password="x" * 14, is_staff=True))
        s = self.client.session
        s["mandant_id"] = m.pk
        s.save()
        self.assertContains(self.client.get(reverse("einkauf:preislisten")), "Hauptliste")
        r = self.client.post(reverse("einkauf:preisliste", args=[p.pk]), {
            "aktion": "vorschau", "datei": SimpleUploadedFile("liste.csv", CSV.encode())})
        self.assertContains(r, "Vorschau: 5 Zeilen")
        self.assertEqual(Preislistenartikel.objects.count(), 0)
        token = r.context["token"]
        r2 = self.client.post(reverse("einkauf:preisliste", args=[p.pk]), {"aktion": "importieren", "token": token})
        protokoll = p.importe.get()
        self.assertRedirects(r2, reverse("einkauf:preislistenimport", args=[protokoll.pk]))
        self.assertContains(self.client.get(reverse("einkauf:preislistenimport", args=[protokoll.pk])), "ohne Bestellnummer")
        r3 = self.client.post(reverse("einkauf:preisliste", args=[p.pk]), {"aktion": "importieren", "token": token})
        self.assertContains(r3, "abgelaufen")  # Datei wurde nach dem Import entfernt
        # Ansicht der Preislistenartikel: Suche mit mehreren Wörtern, Lieferant als Kurzname
        seite = self.client.get(reverse("einkauf:preislistenartikel"), {"q": "cherry tast"})
        self.assertContains(seite, "Tastatur Cherry")
        self.assertContains(seite, "Techdata")
        self.assertNotContains(seite, "Maus Logitech")


class BestpreisplanTest(TestCase):
    def test_faelligkeit(self):
        import datetime as dt
        from django.utils import timezone
        from apps.einkauf.models import Bestpreisplan
        plan = Bestpreisplan.holen()
        jetzt = timezone.make_aware(dt.datetime(2026, 10, 28, 12, 0))  # Mittwoch
        self.assertFalse(plan.faellig(jetzt))  # aus
        plan.rhythmus, plan.uhrzeit = "taeglich", dt.time(3, 0)
        self.assertTrue(plan.faellig(jetzt))  # nie gelaufen
        plan.letzter_lauf = timezone.make_aware(dt.datetime(2026, 10, 28, 3, 5))
        self.assertFalse(plan.faellig(jetzt))  # heute schon gelaufen
        self.assertTrue(plan.faellig(timezone.make_aware(dt.datetime(2026, 10, 29, 3, 1))))  # nächster Tag
        plan.rhythmus, plan.wochentag = "woechentlich", 0  # montags
        plan.letzter_lauf = timezone.make_aware(dt.datetime(2026, 10, 26, 3, 5))  # Montag gelaufen
        self.assertFalse(plan.faellig(jetzt))
        self.assertTrue(plan.faellig(timezone.make_aware(dt.datetime(2026, 11, 2, 3, 1))))
        plan.rhythmus = "nach_lauf"
        self.assertFalse(plan.faellig(jetzt))
        self.assertTrue(plan.faellig(jetzt, preislisten_importiert=True))

    def test_taeglicher_abruf_um_zwei_uhr(self):
        import datetime as dt
        lf = Lieferant.objects.create(nummer="9", name1="Lief")
        p = profil(lf, name="URL", quelle="url", adresse="https://x.test/a.csv", zeitplan="taeglich", abruf_uhrzeit=dt.time(2, 0))
        f = lambda *a: timezone.make_aware(dt.datetime(*a))
        self.assertTrue(p.faellig(f(2026, 10, 28, 1, 0)))  # nie gelaufen
        p.letzter_lauf = f(2026, 10, 28, 2, 7)
        self.assertFalse(p.faellig(f(2026, 10, 28, 14, 0)))  # heute schon gelaufen
        self.assertFalse(p.faellig(f(2026, 10, 29, 1, 59)))  # vor dem nächsten Termin
        self.assertTrue(p.faellig(f(2026, 10, 29, 2, 7)))
