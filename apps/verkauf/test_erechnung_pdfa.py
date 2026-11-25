"""E-Rechnung als PDF/A-3B, eingebettete Schrift, Archivierung nach GoBD (Auftrag des Nutzers 25.11.2026).

Die Probe-Rechnungen wurden zusätzlich mit dem offiziellen Validator Mustang 2.26 (veraPDF, EN 16931, XRechnung 3.0)
geprüft: gültig, 0 Fehler. Diese Tests sichern die Struktur gegen Rückschritte ab."""
import os
import xml.etree.ElementTree as ET
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from reportlab.pdfbase import pdfmetrics

from apps.kern import pdfa
from apps.kern.models import Mandant
from apps.stammdaten.models import Kunde, Zahlungsbedingung
from apps.verkauf.pdf import beleg_pdf

from .models import Beleg, Belegart, ERechnungArchiv, Position
from .zugferd import beleg_pdf_mit_xml, xml_erzeugen, xrechnung_archiviert

RAM = "{urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100}"
UDT = "{urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100}"


@override_settings(SECURE_SSL_REDIRECT=False)
class ERechnungPdfaTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer", strasse="Weg 1", plz="42651", ort="Solingen",
                                        ust_id="DE123456789", iban="DE02120300000000202051", email="info@okay.test", telefon="0212 1")
        self.k = Kunde.objects.create(nummer="1", name1="Muster GmbH", strasse="Str. 2", plz="10115", ort="Berlin",
                                      leitweg_id="04011000-12345-34")
        self.zb = Zahlungsbedingung.objects.create(bezeichnung="14 Tage", tage_netto=14, druck_text="Zahlbar in 14 Tagen")

    def rechnung(self):
        b = Beleg.objects.create(mandant=self.m, art=Belegart.RECHNUNG, kunde=self.k, zahlungsbedingung=self.zb)
        Position.objects.create(beleg=b, bezeichnung="Netzteil ATX 750W", menge=3, einzelpreis=Decimal("60.50"))
        Position.objects.create(beleg=b, bezeichnung="Fachbuch", menge=1, einzelpreis=Decimal("19.90"), steuer_prozent=Decimal("7"))
        b.abschliessen()
        b.refresh_from_db()
        return b

    # ---- Schrift und PDF/A ----------------------------------------------------------------------
    def test_helvetica_ist_eingebettete_liberation_sans(self):
        schrift = pdfmetrics.getFont("Helvetica-Bold")
        self.assertIn("LiberationSans-Bold.ttf", schrift.face.filename)
        self.assertTrue(os.path.exists(os.path.join(pdfa.SCHRIFTORDNER, "LICENSE-Liberation.txt")))
        self.assertAlmostEqual(pdfmetrics.stringWidth("123.456,78 €", "Helvetica", 9), 52.55, delta=0.05)  # = Helvetica-AFM, Layout bleibt

    def test_jedes_pdf_bettet_seine_schriften_ein(self):
        angebot = Beleg.objects.create(mandant=self.m, art=Belegart.ANGEBOT, kunde=self.k)
        Position.objects.create(beleg=angebot, bezeichnung="X", menge=1, einzelpreis=Decimal("1"))
        fehler = pdfa.pruefen(beleg_pdf(angebot))
        self.assertFalse([f for f in fehler if "Schrift" in f], fehler)

    def test_rechnung_und_gutschrift_sind_pdfa3_mit_factur_x(self):
        b = self.rechnung()
        g = b.gutschrift_erzeugen()
        g.abschliessen()
        for beleg in (b, Beleg.objects.get(pk=g.pk)):
            with self.subTest(beleg.art):
                pdf, hinweis = beleg_pdf_mit_xml(beleg)
                self.assertEqual(hinweis, "")
                self.assertEqual(pdfa.pruefen(pdf), [])
                self.assertTrue(pdf.startswith(b"%PDF-1.7"))
                self.assertEqual(pdfa.anhang(pdf), xml_erzeugen(beleg))
                self.assertIn(b"Factur-X PDFA Extension Schema", pdf)

    def test_pruefen_erkennt_fehlendes_pdfa(self):
        fehler = pdfa.pruefen(beleg_pdf(self.rechnung()))  # reines reportlab-PDF
        self.assertIn("kein Farbprofil (OutputIntent)", fehler)
        self.assertIn("keine PDF/A-3-Kennung im XMP", fehler)

    def test_lieferdatum_und_geschaeftsprozess_in_der_xml(self):
        b = self.rechnung()
        w = ET.fromstring(xml_erzeugen(b))
        datum = w.find(f".//{RAM}ActualDeliverySupplyChainEvent/{RAM}OccurrenceDateTime/{UDT}DateTimeString")
        self.assertEqual(datum.text, b.belegdatum.strftime("%Y%m%d"))
        self.assertNotIn(b"BusinessProcessSpecifiedDocumentContextParameter", xml_erzeugen(b))
        self.assertIn(b"urn:fdc:peppol.eu:2017:poacc:billing:01:1.0", xml_erzeugen(b, xrechnung=True))
        g = b.gutschrift_erzeugen()
        g.abschliessen()
        self.assertIn(b.belegdatum.strftime("%Y%m%d").encode(), xml_erzeugen(Beleg.objects.get(pk=g.pk)))

    def test_mandant_luecken(self):
        from .zugferd import mandant_luecken
        self.assertEqual(mandant_luecken(self.m), ([], []))
        leer = Mandant(kurzname="X", firmenname="X")
        self.assertEqual(mandant_luecken(leer), (["USt-ID oder Steuernummer", "Straße", "PLZ", "Ort", "IBAN"], ["Telefon", "E-Mail"]))

    # ---- Archiv (GoBD) --------------------------------------------------------------------------
    def test_erste_e_rechnung_wird_eingefroren(self):
        b = self.rechnung()
        erstes, _ = beleg_pdf_mit_xml(b)
        Mandant.objects.filter(pk=self.m.pk).update(firmenname="Neuer Name GmbH", iban="DE89370400440532013000")
        zweites, _ = beleg_pdf_mit_xml(Beleg.objects.get(pk=b.pk))
        self.assertEqual(erstes, zweites)
        self.assertNotIn(b"Neuer Name", zweites)
        archiv = ERechnungArchiv.objects.get(beleg=b)
        import hashlib
        self.assertEqual(archiv.pdf_sha256, hashlib.sha256(erstes).hexdigest())

    def test_archiv_unveraenderlich_und_nicht_loeschbar(self):
        b = self.rechnung()
        beleg_pdf_mit_xml(b)
        archiv = ERechnungArchiv.objects.get(beleg=b)
        archiv.pdf, archiv.pdf_sha256 = b"%PDF-falsch", "0" * 64
        with self.assertRaisesMessage(ValueError, "nicht ändern"):
            archiv.save()
        with self.assertRaisesMessage(ValueError, "nicht löschen"):
            ERechnungArchiv.objects.get(beleg=b).delete()

    def test_ohne_e_rechnung_wird_nichts_archiviert(self):
        Mandant.objects.filter(pk=self.m.pk).update(ust_id="", steuernummer="")
        b = self.rechnung()
        pdf, hinweis = beleg_pdf_mit_xml(Beleg.objects.get(pk=b.pk))
        self.assertIn("USt-ID", hinweis)
        self.assertFalse(ERechnungArchiv.objects.exists())

    def test_xrechnung_wird_beim_ersten_download_festgeschrieben(self):
        b = self.rechnung()
        erste = xrechnung_archiviert(b)
        Kunde.objects.filter(pk=self.k.pk).update(leitweg_id="99999999-99999-99")
        self.assertEqual(xrechnung_archiviert(Beleg.objects.get(pk=b.pk)), erste)
        archiv = ERechnungArchiv.objects.get(beleg=b)
        self.assertEqual(bytes(archiv.xrechnung), erste)
        self.assertIsNotNone(archiv.xrechnung_am)
        self.assertEqual(pdfa.anhang(bytes(archiv.pdf)), xml_erzeugen(b))  # PDF gleich mit festgeschrieben

    def test_seiten_liefern_das_archiv_und_zeigen_den_hinweis(self):
        get_user_model().objects.create_superuser("chef", password="x" * 14)
        self.client.login(username="chef", password="x" * 14)
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()
        b = self.rechnung()
        pdf = self.client.get(reverse("verkauf:beleg_pdf", args=[b.pk])).content
        self.assertEqual(pdf, bytes(ERechnungArchiv.objects.get(beleg=b).pdf))
        xml = self.client.get(reverse("verkauf:beleg_xrechnung", args=[b.pk])).content
        self.assertIn(b"xrechnung_3.0", xml)
        self.assertContains(self.client.get(reverse("verkauf:beleg", args=[b.pk])), "E-Rechnung (PDF/A-3) archiviert am")
