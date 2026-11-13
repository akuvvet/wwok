"""Kategorie per Suchfeld mit Sofortfilter (Issue 58): Artikelmaske und Verwaltung."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.kern.models import Mandant

from .forms import ArtikelForm
from .models import Artikel, Kategorie, Steuersatz


@override_settings(SECURE_SSL_REDIRECT=False)
class KategorieSuchauswahlTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.hw = Kategorie.objects.create(name="Hardware", nummer="10", online=True)
        self.nb = Kategorie.objects.create(name="Notebooks", nummer="1010", eltern=self.hw, online=True)
        self.alt = Kategorie.objects.create(name="Altgeräte", nummer="99")  # offline
        st = Steuersatz.objects.create(bezeichnung="19", prozent=Decimal("19"))
        self.a = Artikel.objects.create(nummer="A1", bezeichnung="Laptop", steuersatz=st, kategorie=self.nb)
        self.client.force_login(get_user_model().objects.create_user("t", password="x" * 14, is_staff=True, is_superuser=True))
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    def test_formular_rendert_suchauswahl_mit_pfad_nummer_und_offline(self):
        html = str(ArtikelForm(instance=self.a)["kategorie"])
        self.assertIn('data-suchauswahl="kategorie"', html)
        self.assertIn(f'value="{self.nb.pk}" selected data-suche="1010">Hardware › Notebooks', html)
        self.assertIn(f'value="{self.alt.pk}" data-suche="99" data-offline="1">', html)

    def test_artikelmaske_und_verwaltung_binden_den_baustein_ein(self):
        maske = self.client.get(reverse("stammdaten:artikel_bearbeiten", args=[self.a.pk]))
        self.assertContains(maske, "data-suchauswahl")
        seite = self.client.get(reverse("lager:uebersicht"))
        self.assertContains(seite, "window.wwokSuchauswahl")  # Dialog lädt die Maske nach, base.html bringt den Baustein mit
        verwaltung = self.client.get(reverse("admin:stammdaten_artikel_change", args=[self.a.pk]))
        self.assertContains(verwaltung, 'data-suchauswahl="kategorie"')
        self.assertContains(verwaltung, "window.wwokSuchauswahl")
        self.assertContains(verwaltung, "related-widget-wrapper")  # Symbole „+“/Bearbeiten bleiben

    def test_speichern_mit_gewaehlter_kategorie_und_ungueltige_id_abgelehnt(self):
        form = ArtikelForm(instance=self.a)
        daten = {name: feld.value() for name, feld in ((n, form[n]) for n in form.fields) if feld.value() is not None}
        daten["kategorie"] = self.hw.pk
        f = ArtikelForm(daten, instance=self.a)
        f.is_valid()
        self.assertNotIn("kategorie", f.errors)
        daten["kategorie"] = 999999
        self.assertIn("kategorie", ArtikelForm(daten, instance=self.a).errors)


@override_settings(SECURE_SSL_REDIRECT=False)
class NachfolgerSuchauswahlTest(TestCase):
    """Nachfolger als Suchfeld; ähnliche Bezeichnungen schlägt das Skript aus dem Feld „bezeichnung“ vor."""

    def setUp(self):
        Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        st = Steuersatz.objects.create(bezeichnung="19", prozent=Decimal("19"))
        self.a = Artikel.objects.create(nummer="A1", bezeichnung="VGA 8GB RTX 5060", steuersatz=st)
        self.b = Artikel.objects.create(nummer="A2", bezeichnung="VGA 8GB RTX 5060 Ti", steuersatz=st, herstellernummer="GV-N506", ean="4719331")
        self.alt = Artikel.objects.create(nummer="A3", bezeichnung="VGA alt", steuersatz=st, nicht_aktiv=True)
        self.user = get_user_model().objects.create_user("t", password="x" * 14, is_staff=True, is_superuser=True)

    def test_maske_rendert_suchfeld_mit_vergleichsfeld_und_ohne_sich_selbst(self):
        html = str(ArtikelForm(instance=self.a)["nachfolger"])
        self.assertIn('data-suchauswahl="nachfolger"', html)
        self.assertIn('data-aehnlich-feld="bezeichnung"', html)
        self.assertIn('data-marke="nicht aktiv"', html)
        self.assertIn(f'value="{self.b.pk}" data-suche="GV-N506 4719331">A2 – VGA 8GB RTX 5060 Ti', html)
        self.assertIn(f'value="{self.alt.pk}" data-offline="1">', html)
        self.assertNotIn(f'value="{self.a.pk}"', html)

    def test_neuer_artikel_sieht_alle(self):
        html = str(ArtikelForm()["nachfolger"])
        self.assertIn(f'value="{self.a.pk}"', html)

    def test_speichern_mit_nachfolger_und_nicht_sich_selbst(self):
        form = ArtikelForm(instance=self.a)
        daten = {name: feld.value() for name, feld in ((n, form[n]) for n in form.fields) if feld.value() is not None}
        daten["nachfolger"] = self.b.pk
        f = ArtikelForm(daten, instance=self.a)
        f.is_valid()
        self.assertNotIn("nachfolger", f.errors)
        daten["nachfolger"] = self.a.pk
        self.assertIn("nachfolger", ArtikelForm(daten, instance=self.a).errors)

    def test_artikelmaske_nutzt_das_suchfeld(self):
        self.client.force_login(self.user)
        r = self.client.get(reverse("stammdaten:artikel_bearbeiten", args=[self.a.pk]))
        self.assertContains(r, 'data-suchauswahl="nachfolger"')
        self.assertContains(r, 'data-aehnlich-feld="bezeichnung"')
