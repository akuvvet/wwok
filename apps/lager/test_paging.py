from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.kern.models import Mandant
from apps.stammdaten.models import Artikel, Kategorie, Steuersatz


@override_settings(SECURE_SSL_REDIRECT=False)
class LagerPagingTest(TestCase):
    def test_seiten_und_filter(self):
        m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        st = Steuersatz.objects.create(bezeichnung="19", prozent=Decimal("19"))
        kat = Kategorie.objects.create(name="Maeuse", nummer="1")
        for i in range(120):
            Artikel.objects.create(nummer=f"A{i:03d}", bezeichnung=f"Artikel {i}", steuersatz=st, kategorie=kat if i % 2 else None)
        self.client.force_login(get_user_model().objects.create_user("t", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = m.pk
        s.save()
        url = reverse("lager:uebersicht")
        r = self.client.get(url, {"pro_seite": 50})
        self.assertEqual((len(r.context["zeilen"]), r.context["seite"].paginator.num_pages), (50, 3))
        self.assertContains(r, "Seite 1 von 3")
        r2 = self.client.get(url, {"pro_seite": 50, "seite": 3})
        self.assertEqual(len(r2.context["zeilen"]), 20)
        r3 = self.client.get(url, {"kategorie": kat.pk, "pro_seite": 25})
        self.assertEqual(r3.context["seite"].paginator.count, 60)
        r4 = self.client.get(url, {"q": "artikel 7", "seite": 99})  # ungültige Seite -> letzte
        self.assertGreaterEqual(len(r4.context["zeilen"]), 1)
        self.assertContains(self.client.get(url), 'class="knopf')  # Menü als Buttons
