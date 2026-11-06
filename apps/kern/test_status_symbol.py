"""Issue 62: Status als Symbol (Text als Tooltip/aria-label, überfällig, Skonto, Legende)."""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.template import Context, Template
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.kern.models import Mandant
from apps.kern.templatetags.status import STATUS, status_daten
from apps.stammdaten.models import Lieferant


def rendern(text, **kontext):
    return Template(text).render(Context(kontext))


class StatusSymbolTest(SimpleTestCase):
    def test_symbol_mit_tooltip_und_aria(self):
        html = rendern('{% status_symbol "lieferantenrechnung" "exportiert" "zur Zahlung exportiert" %}')
        self.assertIn('aria-label="zur Zahlung exportiert"', html)
        self.assertIn('data-tip="zur Zahlung exportiert"', html)
        self.assertIn("#i-s-export", html)
        self.assertIn("ton-info", html)
        self.assertNotIn("mit-text", html)

    def test_mit_text_ohne_tooltip(self):
        html = rendern('{% status_symbol "lieferantenrechnung" "bezahlt" "bezahlt" mit_text=True %}')
        self.assertIn("mit-text", html)
        self.assertNotIn("data-tip", html)
        self.assertIn('<span class="status-text">bezahlt</span>', html)

    def test_unbekannter_status_bleibt_text(self):
        self.assertEqual(rendern('{% status_symbol "lieferantenrechnung" "xyz" t %}', t="irgendwas <b>"), "irgendwas &lt;b&gt;")

    def test_text_wird_maskiert(self):
        self.assertIn("&lt;script&gt;", rendern('{% status_symbol "werkstatt" "fertig" t %}', t="<script>"))

    def test_ueberfaellig_und_skonto(self):
        heute = timezone.localdate()
        icon, ton, text, zusatz = status_daten("lieferantenrechnung", "offen", "offen", faellig=heute - timedelta(days=3), heute=heute)
        self.assertEqual((icon, ton), ("alarm", "warn"))
        self.assertIn("überfällig seit", text)
        icon, ton, text, zusatz = status_daten("lieferantenrechnung", "offen", "offen", faellig=heute + timedelta(days=10),
                                               skonto_bis=heute + timedelta(days=1), heute=heute)
        self.assertEqual((icon, zusatz), ("uhr", "%"))
        self.assertIn("Skonto nur noch bis", text)
        # bezahlt: weder überfällig noch Skonto
        self.assertEqual(status_daten("lieferantenrechnung", "bezahlt", "", faellig=heute - timedelta(days=3),
                                      skonto_bis=heute, heute=heute)[:2], ("haken", "ok"))
        # Skonto weit weg: kein Hinweis
        self.assertEqual(status_daten("lieferantenrechnung", "offen", "", skonto_bis=heute + timedelta(days=9), heute=heute)[3], "")

    def test_gleiche_bedeutung_gleiches_symbol(self):
        for code in ("storniert", "bezahlt"):
            symbole = {werte[code][0] for werte in STATUS.values() if code in werte}
            self.assertEqual(len(symbole), 1, code)
        self.assertTrue(all(ton in {"ok", "hinweis", "warn", "info", "neutral"} for b in STATUS.values() for _, ton, _ in b.values()))

    def test_alle_symbole_im_vorrat(self):
        from pathlib import Path
        from django.conf import settings
        base = (Path(settings.BASE_DIR) / "templates" / "base.html").read_text(encoding="utf-8")
        for icon in {i for b in STATUS.values() for i, _, _ in b.values()} | {"alarm"}:
            self.assertIn(f'id="i-s-{icon}"', base, icon)

    def test_legende(self):
        html = rendern('{% status_legende "lieferantenrechnung" "ueberfaellig" "skonto" %}')
        self.assertIn("status-legende", html)
        for text in ("offen", "zur Zahlung exportiert", "bezahlt", "überfällig", "Skontofrist"):
            self.assertIn(text, html)


@override_settings(SECURE_SSL_REDIRECT=False)
class StatusSeitenTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.lf = Lieferant.objects.create(nummer="L1", name1="Techdata GmbH")
        user = get_user_model().objects.create_superuser("t", password="x" * 14)
        self.client.force_login(user)
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    def test_lieferantenrechnungen_als_symbol(self):
        from apps.einkauf.models import Lieferantenrechnung
        heute = timezone.localdate()
        r = Lieferantenrechnung.objects.create(mandant=self.m, lieferant=self.lf, rechnungsnummer="R1", datum=heute,
                                               faellig=heute - timedelta(days=1), netto=Decimal("10"), brutto=Decimal("11.90"))
        Lieferantenrechnung.objects.create(mandant=self.m, lieferant=self.lf, rechnungsnummer="R2", datum=heute, faellig=heute,
                                           netto=Decimal("10"), brutto=Decimal("11.90"), status="exportiert")
        seite = self.client.get(reverse("einkauf:rechnungen") + "?status=alle")
        self.assertContains(seite, "#i-s-alarm")  # R1 überfällig
        self.assertContains(seite, 'aria-label="zur Zahlung exportiert"')
        self.assertContains(seite, "status-legende")
        detail = self.client.get(reverse("einkauf:rechnung", args=[r.pk]))
        self.assertContains(detail, "status-symbol ton-warn mit-text")

    def test_bestellungen_als_symbol(self):
        from apps.einkauf import services
        b = services.bestellung_anlegen(self.m, self.lf)
        seite = self.client.get(reverse("einkauf:bestellungen"))
        self.assertContains(seite, 'aria-label="Entwurf"')
        self.assertContains(self.client.get(reverse("einkauf:bestellung", args=[b.pk])), "#i-s-stift")
