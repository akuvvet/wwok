import io
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.kern.models import Mandant
from apps.stammdaten.models import Artikel, ArtikelLieferant, Lieferant, Preislistenartikel, Steuersatz

from . import assistent
from .models import Preislistenprofil

CSV_ANSI = (
    "Artikelnummer;MPN;EAN;Artikelbezeichnung;Marke;Händlerpreis;Lagerbestand\n"
    "TD-1;MX-100;4012345678901;Maus Logitech;Logitech;12,50;40\n"
    "TD-2;;4099999999999;Tastatur Cherry;Cherry;1.234,56;5\n").encode("cp1252")


@override_settings(SECURE_SSL_REDIRECT=False)
class AssistentTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.lf = Lieferant.objects.create(nummer="L1", name1="Techdata GmbH")
        self.st = Steuersatz.objects.get(nummer=1)
        self.maus = Artikel.objects.create(nummer="M1", bezeichnung="Maus", herstellernummer="MX-100", steuersatz=self.st,
                                           vk1=Decimal("20"), durchschnitts_ek=Decimal("10"))
        self.client.force_login(get_user_model().objects.create_user("t", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    def test_kosatec_liste_mit_bom_und_kuerzeln(self):
        kopf = "artnr;herstnr;artname;hersteller;hersturl;ean;hek;vkbrutto;verfuegbar;menge;eta;indate;gewicht;eol;kat1"
        zeilen = [
            '382;58005;"LX-6200 (Edu Bundle. Bulk 10)";LANCOM;;4044144580055;3423,84;4217,99;A;6;2026-10-28;1993-10-11;0;0;Netzwerk',
            '30470;GS108GE;"Netgear GS108GE";NETGEAR;;0606449025187;19,99;24,62;V;200;;2003-10-29;0,795;0;Netzwerk']
        kosatec = ("\ufeff" + "\n".join([kopf] + zeilen) + "\n").encode("utf-8")
        erkannt = assistent.format_erkennen("preisliste.csv", kosatec)
        p = assistent.muster_profil(self.lf, erkannt)
        namen, bsp = assistent.spalten_und_beispiele(p, kosatec)
        self.assertEqual(namen[0], "artnr")  # kein BOM im Spaltennamen
        vorschlag = assistent.spalten_raten(namen)
        self.assertEqual((vorschlag["herstellernummer"], vorschlag["bezeichnung"], vorschlag["ek"], vorschlag["bestand"]),
                         ("herstnr", "artname", "hek", "menge"))  # Bestand = Stückzahl, nicht der Status A/V/B

    def test_format_und_spalten_erkennen(self):
        erkannt = assistent.format_erkennen("liste.csv", CSV_ANSI)
        self.assertEqual((erkannt["dateityp"], erkannt["zeichensatz"], erkannt["trennzeichen"]), ("csv", "cp1252", ";"))
        tab = assistent.format_erkennen("x.txt", "a\tb\tc\n1\t2\t3".encode())
        self.assertEqual(tab["trennzeichen"], "\\t")
        self.assertEqual(assistent.format_erkennen("x.xlsx", b"")["dateityp"], "xlsx")
        xml = assistent.format_erkennen("p.xml", b"<?xml version='1.0'?><liste><artikel><nr>1</nr></artikel><artikel><nr>2</nr></artikel></liste>")
        self.assertEqual((xml["dateityp"], xml["xml_satz"]), ("xml", "artikel"))
        profil = assistent.muster_profil(self.lf, erkannt)
        namen, beispiele = assistent.spalten_und_beispiele(profil, CSV_ANSI)
        self.assertEqual(namen[:2], ["Artikelnummer", "MPN"])
        self.assertEqual(beispiele[0][5], "12,50")
        vorschlag = assistent.spalten_raten(namen)
        self.assertEqual((vorschlag["bestellnummer"], vorschlag["herstellernummer"], vorschlag["ean"], vorschlag["ek"], vorschlag["bestand"]),
                         ("Artikelnummer", "MPN", "EAN", "Händlerpreis", "Lagerbestand"))

    def test_assistent_von_der_datei_bis_zum_import(self):
        r = self.client.post(reverse("einkauf:preislisten_neu"), {
            "aktion": "datei", "lieferant": self.lf.pk, "name": "Hauptliste", "datei": SimpleUploadedFile("preise.csv", CSV_ANSI)})
        self.assertContains(r, "Spalten zuordnen")
        self.assertContains(r, "Händlerpreis")
        self.assertEqual(r.context["auswahl"]["ek"], "Händlerpreis")  # automatisch vorgeschlagen
        token = r.context["token"]
        r2 = self.client.post(reverse("einkauf:preislisten_neu"), {
            "aktion": "speichern", "token": token, "lieferant": self.lf.pk, "name": "Hauptliste", "kopfzeile": "1",
            "dezimaltrennzeichen": ",", "zeichensatz": "cp1252", "trennzeichen": ";", "neue_artikel": "1",
            "sp_bestellnummer": "Artikelnummer", "sp_herstellernummer": "MPN", "sp_ean": "EAN", "sp_bezeichnung": "Artikelbezeichnung",
            "sp_hersteller": "Marke", "sp_ek": "Händlerpreis", "sp_bestand": "Lagerbestand"})
        profil = Preislistenprofil.objects.get()
        self.assertRedirects(r2, f"{reverse('einkauf:preisliste', args=[profil.pk])}?token={token}", fetch_redirect_response=False)
        self.assertEqual((profil.sp_ek, profil.zeichensatz, profil.dateityp, profil.erste_datenzeile), ("Händlerpreis", "cp1252", "csv", 2))
        vorschau = self.client.get(f"{reverse('einkauf:preisliste', args=[profil.pk])}?token={token}")
        self.assertContains(vorschau, "Vorschau: 2 Zeilen")
        self.assertContains(vorschau, "Tastatur Cherry")
        imp = self.client.post(reverse("einkauf:preisliste", args=[profil.pk]), {"aktion": "importieren", "token": token}, follow=True)
        self.assertContains(imp, "2 neu")
        self.assertEqual(ArtikelLieferant.objects.get(bestellnummer="TD-1").artikel, self.maus)  # über Herstellernummer verknüpft
        self.assertEqual(Preislistenartikel.objects.get(bestellnummer="TD-2").ek, Decimal("1234.5600"))  # nur Preisliste

    def test_pflichtfelder_und_zuordnung_aendern(self):
        r = self.client.post(reverse("einkauf:preislisten_neu"), {
            "aktion": "datei", "lieferant": self.lf.pk, "datei": SimpleUploadedFile("preise.csv", CSV_ANSI)})
        r = self.client.post(reverse("einkauf:preislisten_neu"), {
            "aktion": "speichern", "token": r.context["token"], "lieferant": self.lf.pk, "kopfzeile": "1",
            "sp_bestellnummer": "Artikelnummer"}, follow=True)  # EK fehlt
        self.assertContains(r, "müssen zugeordnet werden")
        self.assertEqual(Preislistenprofil.objects.count(), 0)
        profil = Preislistenprofil.objects.create(lieferant=self.lf, name="Alt", zeichensatz="cp1252", sp_bestellnummer="Artikelnummer", sp_ek="Händlerpreis")
        r = self.client.post(reverse("einkauf:preislisten_zuordnung", args=[profil.pk]), {
            "aktion": "datei", "datei": SimpleUploadedFile("neu.csv", CSV_ANSI)})
        self.assertEqual(r.context["auswahl"]["ek"], "Händlerpreis")  # bestehende Zuordnung vorbelegt
        self.client.post(reverse("einkauf:preislisten_zuordnung", args=[profil.pk]), {
            "aktion": "speichern", "token": r.context["token"], "kopfzeile": "1", "dezimaltrennzeichen": ",",
            "sp_bestellnummer": "MPN", "sp_ek": "Händlerpreis", "name": "Neu benannt"})
        profil.refresh_from_db()
        self.assertEqual((profil.sp_bestellnummer, profil.name), ("MPN", "Neu benannt"))

    def test_artikelpreise_vorschau_und_anwenden(self):
        text = ("Art-Nr;EK;Preis brutto\nMX-100;8,40;29,99\nXXX;1;2\n;;\nMX-100;abc;1\n").encode()
        profil = assistent.muster_profil(None, {"trennzeichen": ";"})
        vorschau = assistent.artikelpreise(profil, text, "herstellernummer", "Art-Nr", {"ek": "EK", "vk1": "Preis brutto"}, brutto=True)
        self.assertEqual((vorschau["gelesen"], vorschau["geaendert"], vorschau["nicht_gefunden"], vorschau["fehler"]), (3, 1, 1, 1))
        self.maus.refresh_from_db()
        self.assertEqual(self.maus.durchschnitts_ek, Decimal("10.0000"))  # Vorschau schreibt nichts
        assistent.artikelpreise(profil, text, "herstellernummer", "Art-Nr", {"ek": "EK", "vk1": "Preis brutto"}, brutto=True, anwenden=True)
        self.maus.refresh_from_db()
        self.assertEqual((self.maus.durchschnitts_ek, self.maus.vk1), (Decimal("8.4000"), Decimal("25.20")))  # 29,99 brutto → 25,20 netto
        again = assistent.artikelpreise(profil, text, "herstellernummer", "Art-Nr", {"ek": "EK"}, anwenden=True)
        self.assertEqual(again["geaendert"], 0)

    def test_artikelpreise_maske(self):
        text = "Artikelnummer;EK;VK1\nM1;7,00;19,90\n".encode()
        r = self.client.post(reverse("einkauf:artikelpreise"), {"aktion": "datei", "datei": SimpleUploadedFile("p.csv", text)})
        self.assertContains(r, "Artikelpreise zuordnen")
        token = r.context["token"]
        daten = {"token": token, "schluessel_art": "nummer", "schluessel_spalte": "Artikelnummer", "sp_ek": "EK", "sp_vk1": "VK1",
                 "kopfzeile": "1", "dezimaltrennzeichen": ",", "trennzeichen": ";", "zeichensatz": "utf-8"}
        v = self.client.post(reverse("einkauf:artikelpreise"), {**daten, "aktion": "vorschau"})
        self.assertContains(v, "1 Änderungen übernehmen")
        self.maus.refresh_from_db()
        self.assertEqual(self.maus.vk1, Decimal("20.00"))
        a = self.client.post(reverse("einkauf:artikelpreise"), {**daten, "aktion": "anwenden"})
        self.assertContains(a, "Ergebnis")
        self.maus.refresh_from_db()
        self.assertEqual((self.maus.durchschnitts_ek, self.maus.vk1), (Decimal("7.0000"), Decimal("19.90")))
        xml = self.client.post(reverse("einkauf:artikelpreise"), {"aktion": "datei", "datei": SimpleUploadedFile("p.xml", b"<?xml version='1.0'?><a/>")}, follow=True)
        self.assertContains(xml, "XML wird")
