"""EK-Preis netto beim Anlegen: Basis, solange es weder Preisliste noch Wareneingang gibt; der Preisimport überschreibt ihn."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.einkauf import preislisten
from apps.einkauf.bestpreis import bestpreise
from apps.einkauf.services import ek_vorschlag
from apps.einkauf.test_preislisten import profil
from apps.kern.models import Mandant
from apps.lager.models import Bewegungsart, Lagerbewegung

from .models import Artikel, ArtikelLieferant, EkHerkunft, Lieferant, Steuersatz, _ek_quelle
from .preise import stufenpreis

D = Decimal


@override_settings(SECURE_SSL_REDIRECT=False)
class EkManuellTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.st = Steuersatz.objects.get(nummer=1)
        self.lf = Lieferant.objects.create(nummer="L1", name1="Techdata GmbH", kurzname="Techdata")
        self.client.force_login(get_user_model().objects.create_user("t", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    def daten(self, **extra):
        d = {"nummer": "5000", "typ": "A", "bezeichnung": "Maus", "mengeneinheit": "Stück", "steuersatz": self.st.pk,
             "aufschlag_vk1": "20"}
        d.update(extra)
        return d

    def anlegen(self, **extra):
        return self.client.post(reverse("stammdaten:artikel_neu"), self.daten(**extra))

    def test_anlegen_mit_komma_speichert_dezimal_und_herkunft_manuell(self):
        self.assertEqual(self.anlegen(ek_manuell="12,50").status_code, 200)
        a = Artikel.objects.get(nummer="5000")
        self.assertEqual(a.ek_manuell, D("12.50"))
        self.assertIsInstance(a.ek_manuell, Decimal)
        self.assertEqual(a.ek_herkunft, EkHerkunft.MANUELL)
        self.assertIsNotNone(a.ek_geaendert_am)
        self.assertEqual(_ek_quelle(a), (D("12.50"), "manuell"))
        self.assertEqual(stufenpreis(a, 1), D("15.00"))  # VK-Kalkulation: 12,50 + 20 %
        self.assertEqual(ek_vorschlag(self.m, self.lf, a)[0], D("12.50"))  # Bestellung: manueller EK als Vorschlag

    def test_falsche_und_negative_eingaben_werden_abgelehnt(self):
        for wert, text in (("abc", "Bitte einen Betrag wie 12,50 eingeben."), ("-1,00", "Der EK-Preis darf nicht negativ sein.")):
            r = self.anlegen(ek_manuell=wert)
            self.assertEqual(r.status_code, 422)
            self.assertContains(r, text, status_code=422)
        self.assertFalse(Artikel.objects.exists())

    def test_ohne_ek_bleibt_leer_und_zeigt_ek_fehlt(self):
        self.assertEqual(self.anlegen().status_code, 200)
        a = Artikel.objects.get(nummer="5000")
        self.assertIsNone(a.ek_manuell)
        self.assertEqual(a.ek_herkunft, "")
        self.assertContains(self.client.get(reverse("lager:artikel", args=[a.pk])), "EK fehlt")
        self.assertContains(self.client.get(reverse("lager:uebersicht")), "EK fehlt")
        self.assertContains(self.client.get(reverse("stammdaten:artikel_bearbeiten", args=[a.pk])), "EK fehlt – bitte EK-Preis eintragen.")

    def test_bearbeiten_aendert_ek_und_anzeige_in_liste_und_artikel(self):
        a = Artikel.objects.create(nummer="5000", bezeichnung="Maus", steuersatz=self.st)
        r = self.client.post(reverse("stammdaten:artikel_bearbeiten", args=[a.pk]), self.daten(ek_manuell="7,25"))
        self.assertEqual(r.status_code, 200)
        a.refresh_from_db()
        self.assertEqual((a.ek_manuell, a.ek_herkunft), (D("7.25"), EkHerkunft.MANUELL))
        self.assertContains(self.client.get(reverse("lager:uebersicht")), '<span title="manuell">7,25</span>')
        self.assertContains(self.client.get(reverse("lager:artikel", args=[a.pk])), "<strong>7,25 €</strong>")
        self.assertContains(self.client.get(reverse("stammdaten:artikel_bearbeiten", args=[a.pk])), 'value="7,25"')

    def test_preisimport_hat_vorrang_und_setzt_herkunft_import(self):
        self.anlegen(ek_manuell="12,50", herstellernummer="MX-100")
        a = Artikel.objects.get(nummer="5000")
        preislisten.importieren(profil(self.lf), "Best.-Nr.;Herst.-Nr.;EAN;Bezeichnung;Hersteller;EK;Bestand;Lieferzeit\n"
                                                 "TD-1;MX-100;;Maus;Logitech;9,80;40;2\n".encode(), "liste.csv")
        a.refresh_from_db()
        self.assertEqual(a.ek_herkunft, EkHerkunft.IMPORT)
        self.assertEqual(_ek_quelle(a), (D("9.8000"), "Preisliste Techdata GmbH"))
        # Zeitplan „Günstigster EK“ schreibt den Import-EK fest in den Artikel
        bestpreise(anwenden=True)
        a.refresh_from_db()
        self.assertEqual((a.durchschnitts_ek, a.ek_herkunft), (D("9.8000"), EkHerkunft.IMPORT))
        self.assertEqual(_ek_quelle(a), (D("9.8000"), "Preisimport"))

    def test_bestpreis_ueberschreibt_manuellen_ek(self):
        a = Artikel.objects.create(nummer="5000", bezeichnung="Maus", steuersatz=self.st, ek_manuell=D("12.50"), ek_herkunft="manuell")
        ArtikelLieferant.objects.create(artikel=a, lieferant=self.lf, bestellnummer="TD-1", ek=D("11"))
        bestpreise(anwenden=True)
        a.refresh_from_db()
        self.assertEqual((a.durchschnitts_ek, a.ek_herkunft), (D("11.0000"), EkHerkunft.IMPORT))

    def test_nach_wareneingang_nur_anzeige(self):
        a = Artikel.objects.create(nummer="5000", bezeichnung="Maus", steuersatz=self.st, ek_manuell=D("12.50"),
                                   durchschnitts_ek=D("10"), ek_herkunft="wareneingang")
        Lagerbewegung.objects.create(mandant=self.m, artikel=a, art=Bewegungsart.WARENEINGANG, menge=D("1"))
        r = self.client.get(reverse("stammdaten:artikel_bearbeiten", args=[a.pk]))
        self.assertContains(r, "nach dem Wareneingang gilt der Ø-EK, das Feld ist gesperrt.")
        self.client.post(reverse("stammdaten:artikel_bearbeiten", args=[a.pk]), self.daten(ek_manuell="99"))
        a.refresh_from_db()
        self.assertEqual((a.ek_manuell, a.ek_herkunft), (D("12.50"), "wareneingang"))  # gesperrtes Feld bleibt unverändert
        self.assertEqual(_ek_quelle(a), (D("10"), "Wareneingang"))

    def test_manuelles_speichern_nach_import_laesst_quelle_preisimport(self):
        a = Artikel.objects.create(nummer="5000", bezeichnung="Maus", steuersatz=self.st, durchschnitts_ek=D("9.80"), ek_herkunft="import")
        self.client.post(reverse("stammdaten:artikel_bearbeiten", args=[a.pk]), self.daten(ek_manuell="12,50"))
        a.refresh_from_db()
        self.assertEqual((a.ek_manuell, a.ek_herkunft), (D("12.50"), EkHerkunft.IMPORT))  # Import hat Vorrang und behält die Herkunft
        self.assertEqual(_ek_quelle(a), (D("9.8000"), "Preisimport"))
        # Ø-EK ohne bekannte Herkunft (aus Mention übernommen) heißt neutral „Durchschnitts-EK“, ohne Datum
        alt = Artikel.objects.create(nummer="5001", bezeichnung="Alt", steuersatz=self.st, durchschnitts_ek=D("5"))
        self.assertEqual(_ek_quelle(alt), (D("5"), "Durchschnitts-EK"))
        self.assertIsNone(alt.ek_datum("Durchschnitts-EK"))

    def test_preisimport_aendert_herkunft_nicht_nach_altem_wareneingang(self):
        a = Artikel.objects.create(nummer="5000", bezeichnung="Maus", steuersatz=self.st, herstellernummer="MX-100", durchschnitts_ek=D("10"))
        Lagerbewegung.objects.create(mandant=self.m, artikel=a, art=Bewegungsart.WARENEINGANG, menge=D("1"))
        preislisten.importieren(profil(self.lf), "Best.-Nr.;Herst.-Nr.;EAN;Bezeichnung;Hersteller;EK;Bestand;Lieferzeit\n"
                                                 "TD-1;MX-100;;Maus;Logitech;9,80;40;2\n".encode(), "liste.csv")
        a.refresh_from_db()
        self.assertEqual(a.ek_herkunft, "")

    def test_maske_liefert_ek_fuer_live_umrechnung_vk_und_aufschlag(self):
        a = Artikel.objects.create(nummer="5000", bezeichnung="Maus", steuersatz=self.st, ek_manuell=D("12.50"), ek_herkunft="manuell")
        html = self.client.get(reverse("stammdaten:artikel_bearbeiten", args=[a.pk])).content.decode()
        for stufe in (1, 2, 3):
            self.assertRegex(html, rf'name="vk{stufe}"[^>]*data-ek="12.50" data-ek-quelle="manuell"')
        neu = self.client.get(reverse("stammdaten:artikel_neu")).content.decode()
        self.assertIn('data-ek="" data-ek-quelle=""', neu)  # neuer Artikel: die Maske rechnet mit dem eingetippten EK
        self.assertIn("if (dlg.querySelector('[name=vk1][data-ek]')) preisKopplung();", self.client.get(reverse("lager:uebersicht")).content.decode())

    def test_nur_fuer_lagerartikel(self):
        self.assertEqual(self.anlegen(typ="L", ek_manuell="5", nummer="L1").status_code, 200)
        self.assertIsNone(Artikel.objects.get(nummer="L1").ek_manuell)
