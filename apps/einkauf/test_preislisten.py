import io
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.kern.models import Mandant
from apps.stammdaten.models import Artikel, ArtikelLieferant, Hersteller, Lieferant, Steuersatz

from . import preislisten
from .models import Preislistenprofil

CSV = (
    "Best.-Nr.;Herst.-Nr.;EAN;Bezeichnung;Hersteller;EK;Bestand;Lieferzeit\n"
    "TD-1;MX-100;4012345678901;Maus Logitech MX;Logitech;12,50;40;2\n"      # vorhandener Artikel (Herstellernummer)
    "TD-2;;4099999999999;Tastatur Cherry;Cherry;1.234,56;5;3\n"             # neu (Typ P), Tausendertrenner
    "TD-3;XX-3;;Kabel;;abc;1;1\n"                                           # Fehler: EK
    ";;;ohne Bestellnummer;;1,00;1;1\n"                                     # Fehler: Bestellnummer fehlt
    "TD-5;;4012345678901;Maus per EAN;;11,90;;\n"                           # vorhandener Artikel über EAN
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
        self.lf = Lieferant.objects.create(nummer="L1", name1="Techdata GmbH")
        st = Steuersatz.objects.get(nummer=1)  # per Migration vorbelegt (19 %)
        Hersteller.objects.create(kennzeichen="LOGI", name="Logitech")
        self.maus = Artikel.objects.create(nummer="M1", bezeichnung="Maus", herstellernummer="MX-100",
                                           ean="4012345678901", steuersatz=st)

    def test_vorschau_schreibt_nichts(self):
        p = profil(self.lf)
        v = preislisten.vorschau(p, CSV.encode())
        self.assertEqual((v["gesamt"], v["zeilen"][0]["artikel"], v["zeilen"][1]["ergebnis"]), (5, self.maus, "wird angelegt"))
        self.assertEqual(Artikel.objects.count(), 1)
        self.assertEqual(ArtikelLieferant.objects.count(), 0)

    def test_import_abgleich_neu_fehler_und_wiederholung(self):
        p = profil(self.lf)
        prot = preislisten.importieren(p, CSV.encode(), "liste.csv")
        self.assertEqual((prot.gelesen, prot.neu, prot.fehler), (5, 3, 2))
        self.assertEqual({f["zeile"] for f in prot.fehlerzeilen}, {4, 5})
        al = ArtikelLieferant.objects.get(bestellnummer="TD-1")
        self.assertEqual((al.artikel, al.ek, al.bestand, al.lieferzeit_tage), (self.maus, Decimal("12.5000"), 40, 2))
        self.assertEqual(ArtikelLieferant.objects.get(bestellnummer="TD-5").artikel, self.maus)  # Abgleich über EAN
        neu = Artikel.objects.get(nummer="PL1-TD-2")
        self.assertEqual((neu.typ, neu.bezeichnung, neu.ean), ("P", "Tastatur Cherry", "4099999999999"))
        self.assertEqual(ArtikelLieferant.objects.get(bestellnummer="TD-2").ek, Decimal("1234.5600"))
        # zweiter Lauf mit geändertem Preis: nichts doppelt, nur geändert/unverändert
        geaendert = CSV.replace("12,50", "13,00")
        prot2 = preislisten.importieren(p, geaendert.encode(), "liste.csv")
        self.assertEqual((prot2.neu, prot2.geaendert, prot2.unveraendert, prot2.fehler), (0, 1, 2, 2))
        self.assertEqual(ArtikelLieferant.objects.get(bestellnummer="TD-1").ek, Decimal("13.0000"))
        self.assertEqual(Artikel.objects.count(), 2)
        p.refresh_from_db()
        self.assertIsNotNone(p.letzter_lauf)

    def test_ohne_neue_artikel_nur_aktualisieren(self):
        p = profil(self.lf, neue_artikel=False)
        prot = preislisten.importieren(p, CSV.encode())
        self.assertEqual((prot.neu, prot.uebersprungen), (2, 1))
        self.assertFalse(Artikel.objects.filter(typ="P").exists())

    def test_spaltennummern_ohne_kopfzeile_und_punkt_dezimal_ansi(self):
        text = "TD-9|MX-100|9.99|7\n"
        p = profil(self.lf, trennzeichen="|", zeichensatz="cp1252", dezimaltrennzeichen=".", kopfzeile=False, erste_datenzeile=1,
                   sp_bestellnummer="1", sp_herstellernummer="2", sp_ean="", sp_bezeichnung="", sp_hersteller="", sp_ek="3",
                   sp_bestand="4", sp_lieferzeit="")
        prot = preislisten.importieren(p, text.encode("cp1252"))
        self.assertEqual((prot.neu, prot.fehler), (1, 0))
        self.assertEqual(ArtikelLieferant.objects.get().ek, Decimal("9.9900"))

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
        self.assertEqual(ArtikelLieferant.objects.get(bestellnummer="X-1").ek, Decimal("11.5000"))
        xml = b"""<?xml version="1.0"?><liste><artikel><nr>Y-1</nr><hnr>MX-100</hnr><preis>8,40</preis><menge>2</menge></artikel>
                  <artikel><nr>Y-2</nr><preis>x</preis></artikel></liste>"""
        px = profil(self.lf, name="XML", dateityp="xml", xml_satz="artikel", sp_bestellnummer="nr", sp_herstellernummer="hnr",
                    sp_ean="", sp_bezeichnung="", sp_hersteller="", sp_ek="preis", sp_bestand="menge", sp_lieferzeit="")
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
    def test_vorschau_import_und_protokoll(self):
        m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        lf = Lieferant.objects.create(nummer="L1", name1="Techdata GmbH")
        p = profil(lf)
        self.client.force_login(get_user_model().objects.create_user("t", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = m.pk
        s.save()
        self.assertContains(self.client.get(reverse("einkauf:preislisten")), "Hauptliste")
        r = self.client.post(reverse("einkauf:preisliste", args=[p.pk]), {
            "aktion": "vorschau", "datei": SimpleUploadedFile("liste.csv", CSV.encode())})
        self.assertContains(r, "Vorschau: 5 Zeilen")
        self.assertEqual(ArtikelLieferant.objects.count(), 0)
        token = r.context["token"]
        r2 = self.client.post(reverse("einkauf:preisliste", args=[p.pk]), {"aktion": "importieren", "token": token})
        protokoll = p.importe.get()
        self.assertRedirects(r2, reverse("einkauf:preislistenimport", args=[protokoll.pk]))
        self.assertContains(self.client.get(reverse("einkauf:preislistenimport", args=[protokoll.pk])), "ohne Bestellnummer")
        r3 = self.client.post(reverse("einkauf:preisliste", args=[p.pk]), {"aktion": "importieren", "token": token})
        self.assertContains(r3, "abgelaufen")  # Datei wurde nach dem Import entfernt
