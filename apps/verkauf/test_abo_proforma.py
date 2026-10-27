from datetime import date
from decimal import Decimal
from io import StringIO

from django.core.management import call_command
from django.test import TestCase, override_settings

from apps.kern.models import Mandant
from apps.stammdaten.models import Artikel, Kunde, Steuersatz

from .models import Abo, AboPosition, Beleg, Belegart, Position, monate_addieren
from .pdf import beleg_pdf


class AboProformaTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.kunde = Kunde.objects.create(nummer="1", name1="Kunde GmbH")
        self.st = Steuersatz.objects.create(bezeichnung="19", prozent=Decimal("19"))

    def test_monate_addieren(self):
        self.assertEqual(monate_addieren(date(2026, 1, 31), 1), date(2026, 2, 28))
        self.assertEqual(monate_addieren(date(2026, 11, 15), 3), date(2027, 2, 15))

    def test_proforma_aus_angebot(self):
        an = Beleg.objects.create(mandant=self.m, art=Belegart.ANGEBOT, kunde=self.kunde)
        Position.objects.create(beleg=an, typ="L", bezeichnung="Beratung", menge=2, einzelpreis=Decimal("50"))
        pf = an.kopieren_als(Belegart.PROFORMA)
        self.assertTrue(pf.nummer.startswith("PF"))  # eigener Nummernkreis
        self.assertEqual((pf.art, pf.positionen.count()), (Belegart.PROFORMA, 1))
        self.assertIsNone(pf.loeschhindernis())
        self.assertTrue(beleg_pdf(pf).startswith(b"%PDF"))

    def test_wiederkehrende_rechnung(self):
        abo = Abo.objects.create(mandant=self.m, kunde=self.kunde, bezeichnung="Wartung", monate=3,
                                 naechster_termin=date(2026, 10, 1), ende=date(2027, 4, 1))
        AboPosition.objects.create(abo=abo, bezeichnung="Wartungspauschale", menge=1, einzelpreis=Decimal("100"))
        ausgabe = StringIO()
        call_command("wiederkehrende_rechnungen", stdout=ausgabe)
        abo.refresh_from_db()
        re = Beleg.objects.get(art=Belegart.RECHNUNG)
        self.assertFalse(re.abgeschlossen)  # nur Entwurf
        self.assertEqual((re.summe_netto, re.positionen.count()), (Decimal("100.00"), 1))
        self.assertEqual(abo.naechster_termin, date(2027, 1, 1))
        call_command("wiederkehrende_rechnungen", stdout=StringIO())
        self.assertEqual(Beleg.objects.filter(art=Belegart.RECHNUNG).count(), 1)  # noch nicht fällig
        abo.naechster_termin = date(2027, 4, 1)
        abo.save()
        abo.rechnung_erzeugen(date(2027, 4, 1))
        self.assertFalse(abo.aktiv)  # Ende erreicht


@override_settings(SECURE_SSL_REDIRECT=False)
class VersandTest(TestCase):
    def test_versand_aufkleber_und_tracking(self):
        from decimal import Decimal as D
        from django.contrib.auth import get_user_model
        from django.urls import reverse
        from .models import Versand
        from .versand import aufkleber_pdf, versandmail_text
        m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer", strasse="Weg 1", plz="42651", ort="Solingen")
        kunde = Kunde.objects.create(nummer="1", name1="Kunde GmbH", strasse="Str. 2", plz="10115", ort="Berlin")
        ls = Beleg.objects.create(mandant=m, art=Belegart.LIEFERSCHEIN, kunde=kunde)
        v = Versand.objects.create(beleg=ls, sendungsnummer="00340434161094042557", gewicht_kg=D("1.5"))
        self.assertTrue(aufkleber_pdf(v).startswith(b"%PDF"))
        self.assertIn("00340434161094042557", v.tracking_url)
        betreff, text = versandmail_text(v)
        self.assertIn(v.tracking_url, text)
        self.client.force_login(get_user_model().objects.create_user("t", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = m.pk
        s.save()
        self.assertContains(self.client.get(reverse("verkauf:beleg_versand", args=[ls.pk])), "Sendung verfolgen")
        r = self.client.post(reverse("verkauf:beleg_versand", args=[ls.pk]), {"aktion": "neu", "sendungsnummer": "123 456", "gewicht": "2,5"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(ls.versendungen.count(), 2)
        self.assertEqual(self.client.get(reverse("verkauf:versand_aufkleber", args=[ls.pk, v.pk]))["Content-Type"], "application/pdf")
