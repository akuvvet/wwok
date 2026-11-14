"""Prüfbericht 14.11.2026, Abschnitt C (Einkauf): Wareneingang löschen, Positionen, E-Rechnung, Vorschlag, Scan, Mail,
Serverprüfungen, RMA, Admin, Preislistenimport."""
from datetime import date
from decimal import Decimal
from unittest import mock

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse

from apps.kern.models import Mandant
from apps.stammdaten.models import Artikel, ArtikelLieferant, Kunde, Lieferant, Preislistenartikel, Steuersatz
from apps.werkstatt import services as werkstatt

from . import preislisten, services
from .models import BestellStatus, Bestellung, Lieferantenrechnung, Rma, RmaStatus, Wareneingang
from .test_preislisten import CSV, profil

D = Decimal

CII = """<?xml version="1.0"?>
<rsm:CrossIndustryInvoice xmlns:rsm="urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100"
 xmlns:ram="urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100"
 xmlns:udt="urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100">
<rsm:ExchangedDocument><ram:ID>RE-77</ram:ID><ram:IssueDateTime><udt:DateTimeString format="102">20261015</udt:DateTimeString></ram:IssueDateTime></rsm:ExchangedDocument>
<rsm:SupplyChainTradeTransaction><ram:ApplicableHeaderTradeAgreement><ram:SellerTradeParty><ram:Name>{verkaeufer}</ram:Name>
<ram:SpecifiedTaxRegistration><ram:ID schemeID="VA">{verkaeufer_ust}</ram:ID></ram:SpecifiedTaxRegistration></ram:SellerTradeParty>
<ram:BuyerTradeParty><ram:Name>{kaeufer}</ram:Name><ram:SpecifiedTaxRegistration><ram:ID schemeID="VA">{kaeufer_ust}</ram:ID></ram:SpecifiedTaxRegistration></ram:BuyerTradeParty>
</ram:ApplicableHeaderTradeAgreement>
<ram:ApplicableHeaderTradeSettlement>
<ram:SpecifiedTradeSettlementHeaderMonetarySummation><ram:TaxBasisTotalAmount>100.00</ram:TaxBasisTotalAmount><ram:TaxTotalAmount currencyID="EUR">19.00</ram:TaxTotalAmount><ram:GrandTotalAmount>119.00</ram:GrandTotalAmount></ram:SpecifiedTradeSettlementHeaderMonetarySummation>
</ram:ApplicableHeaderTradeSettlement></rsm:SupplyChainTradeTransaction></rsm:CrossIndustryInvoice>"""


def cii(verkaeufer="Techdata GmbH", verkaeufer_ust="DE123456789", kaeufer="OKAY Computer", kaeufer_ust="DE999999999"):
    return CII.format(verkaeufer=verkaeufer, verkaeufer_ust=verkaeufer_ust, kaeufer=kaeufer, kaeufer_ust=kaeufer_ust).encode()


@override_settings(SECURE_SSL_REDIRECT=False)
class BasisTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer", ust_id="DE 999 999 999")
        self.lf = Lieferant.objects.create(nummer="L1", name1="Techdata GmbH", ust_id="DE123456789")
        st = Steuersatz.objects.create(bezeichnung="19", prozent=D("19"))
        self.maus = Artikel.objects.create(nummer="M1", bezeichnung="Maus", steuersatz=st, vk1=D("15"), ean="4012345678901")
        self.tast = Artikel.objects.create(nummer="T1", bezeichnung="Tastatur", steuersatz=st, vk1=D("25"))
        ArtikelLieferant.objects.create(artikel=self.maus, lieferant=self.lf, bestellnummer="TD-1", ek=D("9"))
        ArtikelLieferant.objects.create(artikel=self.tast, lieferant=self.lf, bestellnummer="TD-2", ek=D("18"))
        user = get_user_model().objects.create_superuser("t", password="x" * 14)
        self.client.force_login(user)
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    def bestellt(self, *positionen):
        b = services.bestellung_anlegen(self.m, self.lf)
        for artikel, menge, *ek in positionen:
            services.bestellposition(b, artikel, D(menge), D(ek[0]) if ek else None)
        services.bestellen(b)
        return b


class WareneingangLoeschenTest(BasisTest):
    def test_ungebucht_loeschen_gibt_bestellung_frei(self):
        b = self.bestellt((self.maus, "5"))
        we = services.wareneingang_anlegen(self.m, self.lf, b)
        services.wareneingang_aus_bestellung(we)
        self.client.post(reverse("einkauf:bestellung_loeschen", args=[b.pk]))
        self.assertTrue(Bestellung.objects.filter(pk=b.pk).exists())  # offener Wareneingang blockiert
        r = self.client.post(reverse("einkauf:wareneingang_loeschen", args=[we.pk]))
        self.assertRedirects(r, reverse("einkauf:bestellung", args=[b.pk]))
        self.assertFalse(Wareneingang.objects.exists())
        self.assertEqual(b.positionen.get().geliefert, D("0"))
        self.client.post(reverse("einkauf:bestellung_stornieren", args=[b.pk]))
        b.refresh_from_db()
        self.assertEqual(b.status, BestellStatus.STORNIERT)

    def test_gebuchter_wareneingang_nicht_loeschbar(self):
        b = self.bestellt((self.maus, "5"))
        we = services.wareneingang_anlegen(self.m, self.lf, b)
        services.wareneingang_aus_bestellung(we)
        services.wareneingang_buchen(we, bestaetigt=True)
        self.client.post(reverse("einkauf:wareneingang_loeschen", args=[we.pk]))
        self.assertTrue(Wareneingang.objects.filter(pk=we.pk).exists())
        with self.assertRaisesMessage(ValueError, "bereits gebucht"):
            services.wareneingang_loeschen(we)

    def test_zugeordnet_zu_rechnung_nicht_loeschbar(self):
        we = services.wareneingang_anlegen(self.m, self.lf)
        r = services.rechnung_erfassen(self.m, self.lf, "R1", date.today(), D("100"), D("119"))
        r.wareneingaenge.add(we)
        with self.assertRaisesMessage(ValueError, "Lieferantenrechnung"):
            services.wareneingang_loeschen(we)

    def test_button_nur_beim_ungebuchten(self):
        we = services.wareneingang_anlegen(self.m, self.lf)
        self.assertContains(self.client.get(reverse("einkauf:wareneingang", args=[we.pk])), "Wareneingang löschen")


class PositionLoeschenTest(BasisTest):
    def loeschen(self, b, pos):
        return self.client.post(reverse("einkauf:bestellung_position_loeschen", args=[b.pk, pos.pk]), follow=True)

    def test_normalfall_entwurf_und_mehrere_positionen(self):
        b = services.bestellung_anlegen(self.m, self.lf)
        services.bestellposition(b, self.maus, D("1"))
        self.loeschen(b, b.positionen.get())
        self.assertEqual(b.positionen.count(), 0)  # Entwurf darf leer werden
        b2 = self.bestellt((self.maus, "1"), (self.tast, "1"))
        self.loeschen(b2, b2.positionen.get(artikel=self.tast))
        self.assertEqual(b2.positionen.count(), 1)

    def test_offener_wareneingang_blockiert(self):
        b = self.bestellt((self.maus, "1"), (self.tast, "1"))
        we = services.wareneingang_anlegen(self.m, self.lf, b)
        services.wareneingang_aus_bestellung(we)
        r = self.loeschen(b, b.positionen.get(artikel=self.tast))
        self.assertEqual(b.positionen.count(), 2)
        self.assertContains(r, "offenen Wareneingang")

    def test_letzte_position_bestellter_bestellung(self):
        b = self.bestellt((self.maus, "1"))
        r = self.loeschen(b, b.positionen.get())
        self.assertEqual(b.positionen.count(), 1)
        self.assertContains(r, "letzte Position")

    def test_stornierte_bestellung(self):
        b = self.bestellt((self.maus, "1"), (self.tast, "1"))
        self.client.post(reverse("einkauf:bestellung_stornieren", args=[b.pk]))
        self.loeschen(b, b.positionen.first())
        self.assertEqual(b.positionen.count(), 2)


class ERechnungTest(BasisTest):
    def hochladen(self, inhalt):
        return self.client.post(reverse("einkauf:rechnung_erechnung"), {"datei": SimpleUploadedFile("re.xml", inhalt)}, follow=True)

    def test_normalfall(self):
        r = self.hochladen(cii())
        self.assertEqual(Lieferantenrechnung.objects.get().lieferant, self.lf)
        self.assertContains(r, "übernommen")

    def test_falscher_empfaenger_wird_abgelehnt(self):
        r = self.hochladen(cii(kaeufer="Andere Firma", kaeufer_ust="DE111111111"))
        self.assertFalse(Lieferantenrechnung.objects.exists())
        self.assertContains(r, "andere USt-ID")

    def test_lieferant_mit_abweichender_ust_id_wird_abgelehnt(self):
        r = self.hochladen(cii(verkaeufer_ust="DE555555555"))  # Name passt, USt-ID gehört einem anderen
        self.assertFalse(Lieferantenrechnung.objects.exists())
        self.assertContains(r, "passt nicht")

    def test_namensanfang_nur_bei_eindeutigem_treffer(self):
        Lieferant.objects.create(nummer="L2", name1="Techdata Distribution AG")  # zweiter Treffer für „Techdata“
        r = self.hochladen(cii(verkaeufer="Techdata International Holding", verkaeufer_ust=""))
        self.assertFalse(Lieferantenrechnung.objects.exists())
        self.assertContains(r, "nicht gefunden")


class VorschlagTest(BasisTest):
    def abschicken(self, menge):
        return self.client.post(reverse("einkauf:vorschlag"), {"auswahl": [self.maus.pk], f"menge_{self.maus.pk}": menge}, follow=True)

    def test_ungueltige_mengen(self):
        for menge in ("abc", "0", "-3", "1e400"):
            with self.subTest(menge=menge):
                r = self.abschicken(menge)
                self.assertEqual(r.status_code, 200)
                self.assertFalse(Bestellung.objects.exists())

    def test_normalfall(self):
        self.abschicken("4")
        self.assertEqual(Bestellung.objects.get().positionen.get().menge, D("4"))

    def test_unbekannter_artikel_und_kein_text_als_id(self):
        r = self.client.post(reverse("einkauf:vorschlag"), {"auswahl": ["x"]})
        self.assertEqual(r.status_code, 200)
        r = self.client.post(reverse("einkauf:vorschlag"), {"auswahl": ["99999"]})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(Bestellung.objects.exists())


class ScanTest(BasisTest):
    def test_zwei_positionen_desselben_artikels(self):
        b = self.bestellt((self.maus, "2", "9"), (self.maus, "3", "10"))
        p1, p2 = b.positionen.order_by("id")
        we = services.wareneingang_anlegen(self.m, self.lf, b)
        for _ in range(4):
            services.wareneingang_erfassen(we, self.maus)
        je_bp = {z.bestellposition_id: z.menge for z in we.positionen.all()}
        self.assertEqual(je_bp, {p1.pk: D("2"), p2.pk: D("2")})  # erste Position voll, dann die zweite

    def test_ueberlieferung_bleibt_bei_erster_position(self):
        b = self.bestellt((self.maus, "1", "9"), (self.maus, "1", "10"))
        we = services.wareneingang_anlegen(self.m, self.lf, b)
        for _ in range(3):
            services.wareneingang_erfassen(we, self.maus)
        self.assertEqual(sum(z.menge for z in we.positionen.all()), D("3"))
        self.assertEqual(we.positionen.get(bestellposition=b.positionen.order_by("id").first()).menge, D("2"))

    def test_eine_position_wie_bisher(self):
        b = self.bestellt((self.maus, "10"))
        we = services.wareneingang_anlegen(self.m, self.lf, b)
        services.wareneingang_erfassen(we, self.maus)
        services.wareneingang_erfassen(we, self.maus)
        self.assertEqual(we.positionen.get().menge, D("2"))


class MailTest(BasisTest):
    def test_keine_mail_ohne_positionen(self):
        b = services.bestellung_anlegen(self.m, self.lf)
        with mock.patch("apps.einkauf.views.senden_an") as senden:
            r = self.client.post(reverse("einkauf:bestellung_mail", args=[b.pk]), {"an": "a@b.de", "betreff": "x", "text": "y"})
        senden.assert_not_called()
        self.assertContains(r, "ohne Positionen")
        b.refresh_from_db()
        self.assertEqual(b.status, BestellStatus.ENTWURF)

    def test_keine_mail_fuer_stornierte_bestellung(self):
        b = self.bestellt((self.maus, "1"))
        b.status = BestellStatus.STORNIERT
        b.save()
        with mock.patch("apps.einkauf.views.senden_an") as senden:
            self.client.post(reverse("einkauf:bestellung_mail", args=[b.pk]), {"an": "a@b.de", "betreff": "x", "text": "y"})
        senden.assert_not_called()

    def test_normalfall_sendet_und_markiert_bestellt(self):
        b = services.bestellung_anlegen(self.m, self.lf)
        services.bestellposition(b, self.maus, D("1"))
        with mock.patch("apps.einkauf.views.senden_an") as senden:
            self.client.post(reverse("einkauf:bestellung_mail", args=[b.pk]), {"an": "a@b.de", "betreff": "x", "text": "y"})
        senden.assert_called_once()
        b.refresh_from_db()
        self.assertEqual(b.status, BestellStatus.BESTELLT)


class ServerpruefungTest(BasisTest):
    def test_wareneingang_zu_stornierter_bestellung(self):
        b = self.bestellt((self.maus, "1"))
        b.status = BestellStatus.STORNIERT
        b.save()
        r = self.client.post(reverse("einkauf:wareneingang_neu"), {"bestellung": b.pk}, follow=True)
        self.assertFalse(Wareneingang.objects.exists())
        self.assertContains(r, "storniert")

    def test_buchen_zu_stornierter_bestellung(self):
        b = self.bestellt((self.maus, "1"))
        we = services.wareneingang_anlegen(self.m, self.lf, b)
        services.wareneingang_aus_bestellung(we)
        b.status = BestellStatus.STORNIERT
        b.save()
        with self.assertRaisesMessage(ValueError, "storniert"):
            services.wareneingang_buchen(we, bestaetigt=True)

    def test_negativer_ek_in_bestellung(self):
        b = services.bestellung_anlegen(self.m, self.lf)
        with self.assertRaisesMessage(ValueError, "negativ"):
            services.bestellposition(b, self.maus, D("1"), D("-5"))
        self.client.post(reverse("einkauf:bestellung_position", args=[b.pk]), {"artikel": "M1", "menge": "1", "ek": "-5"})
        self.assertFalse(b.positionen.exists())

    def test_menge_null_in_bestellung(self):
        b = services.bestellung_anlegen(self.m, self.lf)
        with self.assertRaises(ValueError):
            services.bestellposition(b, self.maus, D("0"))

    def test_negativer_ek_im_wareneingang(self):
        we = services.wareneingang_anlegen(self.m, self.lf)
        with self.assertRaisesMessage(ValueError, "negativ"):
            services.wareneingang_erfassen(we, self.maus, D("1"), D("-1"))

    def test_gesperrter_lieferant(self):
        self.lf.gesperrt = True
        self.lf.save()
        r = self.client.post(reverse("einkauf:bestellung_neu"), {"lieferant": self.lf.pk}, follow=True)
        self.assertFalse(Bestellung.objects.exists())
        self.assertContains(r, "gesperrt")
        r = self.client.post(reverse("einkauf:wareneingang_neu"), {"lieferant": self.lf.pk}, follow=True)
        self.assertFalse(Wareneingang.objects.exists())
        self.assertContains(r, "gesperrt")


class RmaTest(BasisTest):
    def anlegen(self):
        self.client.post(reverse("einkauf:rma_neu"), {"lieferant": self.lf.pk, "bezeichnung": "Maus", "fehler": "kaputt"})

    def test_ungueltiger_status(self):
        self.anlegen()
        rma = Rma.objects.get()
        self.client.post(reverse("einkauf:rma", args=[rma.pk]), {"status": "quatsch"})
        rma.refresh_from_db()
        self.assertEqual(rma.status, RmaStatus.ANGELEGT)
        self.client.post(reverse("einkauf:rma", args=[rma.pk]), {"status": "eingesendet", "gutschrift_betrag": "-5"})
        rma.refresh_from_db()
        self.assertEqual(rma.status, RmaStatus.ANGELEGT)  # negativer Betrag: nichts gespeichert
        self.client.post(reverse("einkauf:rma", args=[rma.pk]), {"status": "eingesendet"})
        rma.refresh_from_db()
        self.assertEqual((rma.status, rma.eingesendet_am), (RmaStatus.EINGESENDET, date.today()))

    def test_doppelklick_neue_rma(self):
        self.anlegen()
        self.anlegen()
        self.assertEqual(Rma.objects.count(), 1)

    def test_doppelklick_rma_aus_reparatur(self):
        kunde = Kunde.objects.create(nummer="1", name1="K")
        rep = werkstatt.annehmen(self.m, kunde, "Notebook", "defekt", seriennummer="SN1", artikel=self.maus)
        url = reverse("einkauf:rma_aus_reparatur", args=[rep.pk])
        self.client.post(url, {"lieferant": self.lf.pk})
        r = self.client.post(url, {"lieferant": self.lf.pk}, follow=True)
        self.assertEqual(Rma.objects.count(), 1)
        self.assertContains(r, "offene RMA")

    def test_nach_abschluss_neue_rma_zum_auftrag_moeglich(self):
        kunde = Kunde.objects.create(nummer="1", name1="K")
        rep = werkstatt.annehmen(self.m, kunde, "Notebook", "defekt", seriennummer="SN1", artikel=self.maus)
        rma = services.rma_anlegen(self.m, self.lf, "Notebook", "defekt", reparatur=rep)
        rma.status = RmaStatus.ABGELEHNT
        rma.save()
        services.rma_anlegen(self.m, self.lf, "Notebook", "defekt", reparatur=rep)
        self.assertEqual(Rma.objects.count(), 2)


class AdminGebuchtTest(BasisTest):
    def test_gebuchter_wareneingang_nur_lesbar(self):
        b = self.bestellt((self.maus, "1"))
        we = services.wareneingang_anlegen(self.m, self.lf, b)
        services.wareneingang_aus_bestellung(we)
        offen_ = Wareneingang.objects.get(pk=we.pk)
        services.wareneingang_buchen(we, bestaetigt=True)
        gebucht = Wareneingang.objects.get(pk=we.pk)
        request = RequestFactory().get("/")
        request.user = get_user_model().objects.get(username="t")
        ma = admin.site._registry[Wareneingang]
        inline = ma.get_inline_instances(request, gebucht)[0]
        self.assertTrue(ma.has_change_permission(request, offen_))
        self.assertFalse(ma.has_change_permission(request, gebucht))
        self.assertFalse(ma.has_delete_permission(request, gebucht))
        self.assertFalse(inline.has_change_permission(request, gebucht))
        self.assertFalse(inline.has_add_permission(request, gebucht))
        self.assertFalse(inline.has_delete_permission(request, gebucht))
        self.assertTrue(inline.has_change_permission(request, Wareneingang(gebucht=False)))


class ImportTransaktionTest(TestCase):
    def test_abbruch_laesst_nichts_zurueck(self):
        lf = Lieferant.objects.create(nummer="L1", name1="Techdata GmbH", kurzname="Techdata")
        p = profil(lf)
        with mock.patch.object(preislisten, "_masse_in_artikel", side_effect=RuntimeError("Abbruch")):
            with self.assertRaises(RuntimeError):
                preislisten.importieren(p, CSV.encode(), "liste.csv")
        self.assertEqual(Preislistenartikel.objects.count(), 0)
        self.assertEqual(ArtikelLieferant.objects.count(), 0)
        p.refresh_from_db()
        self.assertIsNone(p.laeuft_seit)  # Sperre ist wieder frei

    def test_normalfall_schreibt(self):
        lf = Lieferant.objects.create(nummer="L1", name1="Techdata GmbH", kurzname="Techdata")
        preislisten.importieren(profil(lf), CSV.encode(), "liste.csv")
        self.assertEqual(Preislistenartikel.objects.count(), 3)
