"""Preislistenartikel (Typ P) als neuen Lagerartikel (Typ A) kopieren (Issue 24)."""
from decimal import Decimal as D

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.kern.models import Mandant

from . import kopie
from .models import (Artikel, ArtikelLieferant, Hersteller, Lieferant, PreisVorgabe, Preislistenartikel, Stammnummer, Steuersatz)
from .preise import stufenpreis

NEU = "stammdaten:artikel_neu"


@override_settings(SECURE_SSL_REDIRECT=False)
class PreislisteKopieTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        # Standard-Steuersatz (nummer=1) kommt aus der Migration 0005 – genau den wählt die Kopie-Logik.
        self.st = Steuersatz.objects.filter(nummer=1).first() or Steuersatz.objects.create(bezeichnung="19", prozent=D("19"))
        self.hs = Hersteller.objects.create(name="Delock")
        self.api = Lieferant.objects.create(nummer="70272", name1="API GmbH", kurzname="API")
        self.kosa = Lieferant.objects.create(nummer="70300", name1="Kosatec GmbH", kurzname="Kosatec")
        self.p1 = Preislistenartikel.objects.create(
            lieferant=self.api, bestellnummer="100012", herstellernummer="65051", ean="4043619650517", bezeichnung="Delock DVI Adapter",
            hersteller="Delock", ek=D("7.90"), bestand=5, lieferzeit_tage=2, gewicht_kg=D("0.120"), preisliste="Api")
        self.p2 = Preislistenartikel.objects.create(
            lieferant=self.kosa, bestellnummer="K-65051", herstellernummer="65051", bezeichnung="Delock DVI Adapter",
            hersteller="Delock", ek=D("7.20"), lieferzeit_tage=3, preisliste="Kosatec")
        PreisVorgabe.objects.create(aufschlag_vk1=D("50"))
        Stammnummer.setzen(Stammnummer.Art.ARTIKEL, 5000)
        self.client.force_login(get_user_model().objects.create_superuser("chef", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()
        self.url = reverse(NEU) + f"?kopie_von_pl={self.p1.pk}"

    def daten(self, **extra):
        d = {"nummer": "5001", "typ": "A", "bezeichnung": "Delock DVI Adapter", "mengeneinheit": "Stück", "steuersatz": self.st.pk,
             "hersteller": self.hs.pk, "herstellernummer": "65051", "ean": "4043619650517", "gewicht_kg": "0.120",
             "kopie_von_pl": self.p1.pk, "md-lagerplatz": ""}
        d.update(extra)
        return d

    def test_vorbelegung_ohne_kopiezusatz_mit_ean_und_herstellernummer(self):
        neu = kopie.neuer_artikel_aus_preisliste(self.p1)
        self.assertIsNone(neu.pk)
        self.assertEqual((neu.typ, neu.bezeichnung, neu.herstellernummer, neu.ean, neu.hersteller, neu.gewicht_kg, neu.steuersatz),
                         ("A", "Delock DVI Adapter", "65051", "4043619650517", self.hs, D("0.120"), self.st))
        self.assertEqual(neu.nummer, Stammnummer.vorschlag("A"))
        self.assertFalse(neu.online)
        self.assertEqual((neu.durchschnitts_ek, neu.ek_herkunft), (D("7.20"), "import"))  # günstigster EK aller Lieferanten

    def test_maske_oeffnet_vorausgefuellt_und_legt_nichts_an(self):
        r = self.client.get(self.url)
        self.assertContains(r, 'name="kopie_von_pl"')
        self.assertContains(r, "Delock DVI Adapter")
        self.assertContains(r, "65051")
        self.assertEqual((Artikel.objects.count(), ArtikelLieferant.objects.count()), (0, 0))
        self.p1.refresh_from_db()
        self.assertIsNone(self.p1.artikel)

    def test_speichern_verknuepft_alle_lieferanten_und_setzt_ek_und_vk(self):
        r = self.client.post(reverse(NEU), self.daten())
        self.assertEqual(r.status_code, 200, r.content.decode()[:500])
        a = Artikel.objects.get(nummer="5001")
        self.assertEqual((a.typ, a.herstellernummer, a.ean, a.online), ("A", "65051", "4043619650517", False))
        self.assertEqual((a.durchschnitts_ek, a.ek_herkunft), (D("7.20"), "import"))
        self.assertEqual(stufenpreis(a, 1), D("10.80"))  # 7,20 + 50 % Aufschlag
        zeilen = {(z.lieferant, z.bestellnummer): z for z in a.lieferanten.all()}
        self.assertEqual(set(zeilen), {(self.api, "100012"), (self.kosa, "K-65051")})
        z = zeilen[(self.api, "100012")]
        self.assertEqual((z.ek, z.lieferzeit_tage, z.preisliste), (D("7.90"), 2, "Api"))
        for p in (self.p1, self.p2):
            p.refresh_from_db()
            self.assertEqual(p.artikel, a)
        md = a.mandantendaten.get(mandant=self.m)  # Bestand 0: keine Bestandsbuchung, kein EK-Wert aus dem Lager
        self.assertIsNone(md.letzter_ek)

    def test_p_artikel_bleibt_unveraendert(self):
        vorher = list(Preislistenartikel.objects.order_by("pk").values("ek", "bezeichnung", "herstellernummer", "bestand"))
        self.client.post(reverse(NEU), self.daten())
        self.assertEqual(list(Preislistenartikel.objects.order_by("pk").values("ek", "bezeichnung", "herstellernummer", "bestand")), vorher)

    def test_fehlender_hersteller_wird_beim_speichern_angelegt(self):
        self.hs.delete()
        r = self.client.get(self.url)
        self.assertEqual(Hersteller.objects.count(), 0)  # Anzeigen legt nichts an
        self.assertEqual(r.status_code, 200)
        self.client.post(reverse(NEU), self.daten(hersteller=""))
        self.assertEqual(Artikel.objects.get(nummer="5001").hersteller.name, "Delock")

    def test_dublette_per_herstellernummer_zeigt_hinweis_mit_link(self):
        vorhanden = Artikel.objects.create(nummer="4000", typ="A", bezeichnung="Alt", herstellernummer="65051", steuersatz=self.st)
        r = self.client.get(self.url)
        self.assertContains(r, "gibt es schon")
        self.assertContains(r, reverse("lager:artikel", args=[vorhanden.pk]))
        self.assertNotContains(r, 'name="kopie_von_pl"')
        weiter = self.client.get(self.url + "&trotzdem=1")  # Abbruch ist möglich, bewusstes Weitermachen ebenso
        self.assertContains(weiter, 'name="kopie_von_pl"')
        self.assertEqual(Artikel.objects.count(), 1)

    def test_dublette_per_ean(self):
        Artikel.objects.create(nummer="4001", typ="A", bezeichnung="Alt", ean="4043619650517", steuersatz=self.st)
        self.assertContains(self.client.get(self.url), "gibt es schon")

    def test_unbekannter_eintrag_404(self):
        self.assertEqual(self.client.get(reverse(NEU) + "?kopie_von_pl=99999").status_code, 404)

    def test_liste_zeigt_knopf_nur_ohne_eigenen_artikel(self):
        r = self.client.get(reverse("einkauf:preislistenartikel"))
        self.assertContains(r, f"?kopie_von_pl={self.p1.pk}")
        a = Artikel.objects.create(nummer="4002", typ="A", bezeichnung="X", steuersatz=self.st)
        Preislistenartikel.objects.filter(pk=self.p1.pk).update(artikel=a)
        r = self.client.get(reverse("einkauf:preislistenartikel"))
        self.assertNotContains(r, f"?kopie_von_pl={self.p1.pk}")
        self.assertContains(r, f"?kopie_von_pl={self.p2.pk}")

    def test_a_artikel_behalten_das_normale_kopieren(self):
        a = Artikel.objects.create(nummer="4003", typ="A", bezeichnung="Normal", steuersatz=self.st)
        r = self.client.get(reverse("stammdaten:artikel_bearbeiten", args=[a.pk]))
        self.assertContains(r, "data-kopieren=")
        self.assertNotContains(r, "kopie_von_pl")
