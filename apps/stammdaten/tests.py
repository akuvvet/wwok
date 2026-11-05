from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse


class LiveSucheTests(TestCase):
    """Live-Suche: zentrales Skript in der Verwaltung, Suche bleibt mit Sortierung/Filter kombinierbar."""

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_superuser("admin-live", "a@example.com", "pw")

    def setUp(self):
        self.client.force_login(self.user)

    def test_skript_in_listen_eingebunden(self):
        for name in ["stammdaten_preislistenartikel", "stammdaten_kunde", "stammdaten_lieferant", "stammdaten_artikel"]:
            with self.subTest(name=name):
                r = self.client.get(reverse(f"admin:{name}_changelist"))
                self.assertContains(r, "data-wwok-livesuche")
                self.assertContains(r, 'id="searchbar"')
                self.assertContains(r, "AbortController")

    def test_suche_mit_sortierung_behaelt_parameter(self):
        r = self.client.get(reverse("admin:stammdaten_preislistenartikel_changelist"), {"q": "ab", "o": "1"})
        self.assertEqual(r.status_code, 200)
        # Sortierung steckt als verstecktes Feld im Suchformular und wird von der Live-Suche übernommen
        self.assertContains(r, 'name="o" value="1"')
        self.assertContains(r, 'value="ab"')
