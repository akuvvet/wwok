from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core import mail as djmail
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.kern.models import Mandant
from apps.lager.models import Bewegungsart, bestand
from apps.lager.services import buche_manuell
from apps.stammdaten.models import Artikel, Kunde, Steuersatz
from apps.verkauf.models import Belegart

from . import services
from .models import Reparatur, ReparaturPosition, Status
from .pdf import schein_pdf
from apps.kern.testhilfe import mit_logo


@override_settings(SECURE_SSL_REDIRECT=False)
class WerkstattTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.kunde = Kunde.objects.create(nummer="1", name1="Kunde GmbH", email="k@example.test")
        st = Steuersatz.objects.create(bezeichnung="19", prozent=Decimal("19"))
        self.ssd = Artikel.objects.create(nummer="SSD1", bezeichnung="SSD 500GB", steuersatz=st, vk1=Decimal("60"))
        buche_manuell(self.m, self.ssd, Bewegungsart.WARENEINGANG, Decimal("5"))
        self.user = get_user_model().objects.create_user("t", password="x" * 14)

    def annehmen(self, **kw):
        return services.annehmen(self.m, self.kunde, "Notebook", "startet nicht", seriennummer="SN123", **kw)

    def test_nummer_status_und_statusmail(self):
        r = self.annehmen()
        r2 = self.annehmen()
        self.assertTrue(r.nummer.startswith("WS") and r2.nummer.endswith("00002"))
        # SMTP nicht eingerichtet -> Meldung statt Fehler, Status wird trotzdem gesetzt
        meldung = services.status_setzen(r, Status.FERTIG)
        self.assertIn("nicht gesendet", meldung)
        self.assertEqual((r.status, r.fertig_am), (Status.FERTIG, date.today()))
        self.assertIsNone(services.status_setzen(r, Status.WARTET))  # keine Mail für diesen Status

    def test_rechnung_aus_teilen_und_arbeitszeit(self):
        r = self.annehmen()
        with self.assertRaises(ValueError):
            services.rechnung_erzeugen(r)
        services.position_hinzufuegen(r, "T", "SSD 500GB", Decimal("1"), Decimal("60"), self.ssd)
        services.position_hinzufuegen(r, "A", "Arbeitszeit", Decimal("1.5"), Decimal("50"))
        beleg = services.rechnung_erzeugen(r)
        self.assertEqual((beleg.art, beleg.summe_netto, beleg.summe_brutto), (Belegart.RECHNUNG, Decimal("135.00"), Decimal("160.65")))
        with self.assertRaises(ValueError):
            services.rechnung_erzeugen(r)  # nur einmal
        beleg.abschliessen()
        self.assertEqual(bestand(self.m, self.ssd), Decimal("4"))  # Teil wird über die Rechnung abgebucht

    def test_garantie_bucht_teile_einmal_und_keine_rechnung(self):
        r = self.annehmen(garantie=True)
        services.position_hinzufuegen(r, "T", "SSD 500GB", Decimal("1"), Decimal("0"), self.ssd)
        with self.assertRaises(ValueError):
            services.rechnung_erzeugen(r)
        self.assertEqual(services.garantieteile_buchen(r), 1)
        self.assertEqual(bestand(self.m, self.ssd), Decimal("4"))
        with self.assertRaises(ValueError):
            services.garantieteile_buchen(r)

    def test_masken_und_pdf(self):
        self.client.force_login(self.user)
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()
        r = self.client.post(reverse("werkstatt:annahme"), {
            "kundennummer": "1", "geraet": "Drucker HP", "fehler": "Papierstau", "seriennummer": "ABC9"})
        rep = Reparatur.objects.get()
        self.assertRedirects(r, reverse("werkstatt:detail", args=[rep.pk]))
        self.client.post(reverse("werkstatt:position", args=[rep.pk]), {"art": "T", "artikel": "SSD1", "menge": "1"})
        self.assertEqual(rep.positionen.get().einzelpreis, Decimal("60.00"))
        self.assertContains(self.client.get(reverse("werkstatt:detail", args=[rep.pk])), "Drucker HP")
        self.assertContains(self.client.get(reverse("werkstatt:liste"), {"q": "abc9"}), rep.nummer)
        for art in ("annahme", "abhol"):
            self.assertEqual(self.client.get(reverse("werkstatt:schein", args=[rep.pk, art]))["Content-Type"], "application/pdf")
        self.assertTrue(schein_pdf(rep, True).startswith(b"%PDF"))
        ohne = schein_pdf(rep, True)
        with mit_logo(self.m):
            self.assertGreater(len(schein_pdf(type(rep).objects.get(pk=rep.pk), True)), len(ohne))  # Logo eingebettet
        self.assertContains(self.client.get(reverse("lager:seriennummern"), {"q": "ABC9"}), rep.nummer)
        self.client.post(reverse("werkstatt:rechnung", args=[rep.pk]))
        rep.refresh_from_db()
        self.assertIsNotNone(rep.rechnung)
