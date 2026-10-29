"""Regressionstests zur Projektprüfung: Folgen im Belegfluss, Sperren, Abos, Eingabeprüfung."""
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
from .models import (Abo, AboPosition, Beleg, BelegGesperrt, Belegart, Mailvorlage, Position, faellige_abos,
                     rechnung_aus_lieferscheinen)


@override_settings(SECURE_SSL_REDIRECT=False)
class AuditVerkaufTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.kunde = Kunde.objects.create(nummer="1", name1="Kunde GmbH")
        st = Steuersatz.objects.create(bezeichnung="19", prozent=Decimal("19"))
        self.a = Artikel.objects.create(nummer="A1", bezeichnung="Maus", steuersatz=st, vk1=Decimal("10"))
        self.b = Artikel.objects.create(nummer="B1", bezeichnung="Tastatur", steuersatz=st, vk1=Decimal("20"))
        buche_manuell(self.m, self.a, Bewegungsart.WARENEINGANG, Decimal("10"))
        buche_manuell(self.m, self.b, Bewegungsart.WARENEINGANG, Decimal("1"))
        self.client.force_login(get_user_model().objects.create_user("t", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    def beleg(self, art, **felder):
        return Beleg.objects.create(mandant=self.m, art=art, kunde=self.kunde, **felder)

    def pos(self, beleg, artikel=None, menge="1", preis="10"):
        return Position.objects.create(beleg=beleg, artikel=artikel, bezeichnung=artikel.bezeichnung if artikel else "Dienst",
                                       menge=Decimal(menge), einzelpreis=Decimal(preis))

    def lieferschein(self, **felder):
        ls = self.beleg(Belegart.LIEFERSCHEIN, **felder)
        self.pos(ls, self.a, "2")
        ls.abschliessen()
        return ls

    # Teillieferung
    def test_teillieferung_uebernimmt_rabatt_und_nur_fuer_entwuerfe(self):
        ls = self.beleg(Belegart.LIEFERSCHEIN, rabatt_prozent=Decimal("10"))
        self.pos(ls, self.a, "2", "100")
        self.pos(ls, self.b, "5", "100")  # nur 1 auf Lager
        teil = ls.teillieferung_abschliessen()
        self.assertEqual(teil.rabatt_prozent, Decimal("10.00"))
        self.assertEqual(teil.summe_netto, Decimal("180.00"))
        with self.assertRaises(ValueError):
            teil.teillieferung_abschliessen()  # abgeschlossen: keine zweite Teilung (keine Doppelbuchung)
        self.assertEqual(bestand(self.m, self.a), Decimal("8"))

    # Sperren
    def test_abgeschlossener_lieferschein_hat_feste_positionen(self):
        ls = self.lieferschein()
        p = ls.positionen.get()
        p.menge = Decimal("1")
        with self.assertRaises(BelegGesperrt):
            p.save()
        with self.assertRaises(BelegGesperrt):
            p.delete()
        with self.assertRaises(BelegGesperrt):
            self.pos(ls, self.a, "1")
        self.assertEqual(bestand(self.m, self.a), Decimal("8"))

    # Doppelte Folgebelege
    def test_lieferschein_nur_einmal_abrechenbar(self):
        ls = self.lieferschein()
        ls.folgebeleg_erzeugen()
        with self.assertRaises(ValueError):
            ls.folgebeleg_erzeugen()
        with self.assertRaises(ValueError):
            ls.kopieren_als(Belegart.RECHNUNG)
        self.assertEqual(Beleg.objects.filter(art=Belegart.RECHNUNG).count(), 1)

    def test_kopie_als_proforma_blockiert_abrechnung_nicht(self):
        ls = self.lieferschein()
        ls.kopieren_als(Belegart.PROFORMA)
        self.assertTrue(ls.abrechenbar)
        rechnung_aus_lieferscheinen([ls])

    def test_auftrag_nur_einmal_in_lieferschein_und_nicht_parallel_in_rechnung(self):
        ab = self.beleg(Belegart.AUFTRAG)
        self.pos(ab, self.a, "1")
        ab.folgebeleg_erzeugen()
        with self.assertRaises(ValueError):
            ab.folgebeleg_erzeugen()
        with self.assertRaises(ValueError):
            ab.kopieren_als(Belegart.RECHNUNG)

    def test_gutschriften_ueberschreiten_rechnung_nicht(self):
        re = self.beleg(Belegart.RECHNUNG)
        self.pos(re, None, "1", "100")
        re.abschliessen()
        gs = re.gutschrift_erzeugen()
        with self.assertRaises(ValueError):
            re.gutschrift_erzeugen()  # volle Gutschrift existiert schon
        gs.abschliessen()
        with self.assertRaises(ValueError):
            re.gutschrift_erzeugen()

    # Abos
    def abo(self, **felder):
        abo = Abo.objects.create(mandant=self.m, kunde=self.kunde, bezeichnung="Wartung", naechster_termin=date(2026, 1, 31),
                                 starttag=31, **felder)
        AboPosition.objects.create(abo=abo, bezeichnung="Wartung", einzelpreis=Decimal("100"))
        return abo

    def test_abo_uebernimmt_rabatt_und_nebenkosten(self):
        abo = self.abo(rabatt_prozent=Decimal("10"), versandkosten=Decimal("5"))
        rechnung = abo.rechnung_erzeugen(date(2026, 1, 31))
        self.assertEqual(rechnung.summe_netto, Decimal("95.00"))

    def test_abo_monatsende_driftet_nicht(self):
        abo = self.abo()
        termine = []
        for _ in range(4):
            abo.rechnung_erzeugen(abo.naechster_termin)
            termine.append(abo.naechster_termin)
        self.assertEqual(termine, [date(2026, 2, 28), date(2026, 3, 31), date(2026, 4, 30), date(2026, 5, 31)])

    def test_abo_nach_ende_nicht_mehr_faellig(self):
        abo = self.abo(ende=date(2026, 1, 15))
        self.assertEqual(list(faellige_abos(date(2026, 2, 1))), [])
        abo.ende = date(2026, 3, 1)
        abo.save()
        self.assertEqual(list(faellige_abos(date(2026, 2, 1))), [abo])

    def test_abo_rhythmus_wird_geprueft(self):
        re = self.beleg(Belegart.RECHNUNG)
        self.pos(re, None, "1", "100")
        re.abschliessen()
        for monate in ("0", "7", "-1", "x"):
            r = self.client.post(reverse("verkauf:abo_aus_rechnung", args=[re.pk]), {"monate": monate, "start": "2026-02-01"})
            self.assertEqual(r.status_code, 302, monate)
        self.assertEqual(Abo.objects.count(), 0)
        self.client.post(reverse("verkauf:abo_aus_rechnung", args=[re.pk]), {"monate": "3", "start": "2026-02-01"})
        self.assertEqual(Abo.objects.get().monate, 3)

    # Eingaben
    def test_position_neu_prueft_menge_rabatt_und_zahlen(self):
        b = self.beleg(Belegart.ANGEBOT)
        url = reverse("verkauf:position_neu", args=[b.pk])
        for daten in ({"artikel": "A1", "menge": "-3"}, {"artikel": "A1", "menge": "0"}, {"artikel": "A1", "rabatt": "150"},
                      {"artikel": "A1", "menge": "NaN"}, {"artikel": "A1", "preis": "Infinity"}):
            self.assertEqual(self.client.post(url, daten).status_code, 302, daten)
        self.assertEqual(b.positionen.count(), 0)

    def test_kopf_lehnt_nan_und_negative_nebenkosten_ab(self):
        b = self.beleg(Belegart.ANGEBOT)
        self.pos(b, self.a, "1")
        url = reverse("verkauf:beleg_kopf", args=[b.pk])
        self.assertEqual(self.client.post(url, {"rabatt_prozent": "NaN"}).status_code, 302)
        self.client.post(url, {"versandkosten": "-500"})
        b.refresh_from_db()
        self.assertEqual(b.versandkosten, Decimal("0"))

    def test_versand_mit_ungueltigem_gewicht_ist_kein_500(self):
        ls = self.lieferschein()
        r = self.client.post(reverse("verkauf:beleg_versand", args=[ls.pk]), {"aktion": "neu", "gewicht": "abc"})
        self.assertEqual(r.status_code, 302)

    # Versand nur bei erlaubtem Druck
    def test_mail_nur_wenn_druck_erlaubt(self):
        ls = self.beleg(Belegart.LIEFERSCHEIN)
        self.pos(ls, self.b, "5")  # Bestand reicht nicht
        Beleg.objects.filter(pk=ls.pk).update(nummer="LS-X")  # nummeriert, aber nicht abgeschlossen
        ls.refresh_from_db()
        with self.assertRaises(MailFehler):
            beleg_senden(ls, "a@b.de", "x", "y")
        self.assertEqual(self.client.get(reverse("verkauf:beleg_mail", args=[ls.pk])).status_code, 302)

    def test_fehlerhafte_mailvorlage_gibt_keinen_500(self):
        re = self.beleg(Belegart.RECHNUNG)
        self.pos(re, None, "1", "100")
        re.abschliessen()
        Mailvorlage.objects.create(mandant=self.m, art=Belegart.RECHNUNG, betreff="Rechnung {", text="Hallo {0} {}")
        self.assertEqual(self.client.get(reverse("verkauf:beleg_mail", args=[re.pk])).status_code, 200)
