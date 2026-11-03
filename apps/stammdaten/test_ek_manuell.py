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
        seite = self.client.get(reverse("lager:uebersicht")).content.decode()
        self.assertIn("window.wwokPreisKopplung = function (wurzel)", seite)  # gemeinsame Logik aus _preiskopplung.html
        self.assertIn("if (dlg.querySelector('[name=vk1][data-ek]')) wwokPreisKopplung(dlg);", seite)

    def test_aufschlag_vk1_fuer_alle_und_preisraster(self):
        html = self.client.get(reverse("stammdaten:artikel_neu")).content.decode()
        self.assertIn('class="mraster preisraster"', html)
        # Reihenfolge im Raster: EK, beide Häkchen, VK1–VK3, Aufschlag VK1–VK3
        namen = ["ek_manuell", "vk1_fuer_alle", "aufschlag_vk1_fuer_alle", "vk1", "vk2", "vk3", "aufschlag_vk1", "aufschlag_vk2", "aufschlag_vk3"]
        stellen = [html.index(f'name="{n}"') for n in namen]
        self.assertEqual(stellen, sorted(stellen))
        self.assertEqual(self.anlegen(ek_manuell="10", aufschlag_vk1="25", aufschlag_vk1_fuer_alle="on").status_code, 200)
        a = Artikel.objects.get(nummer="5000")
        self.assertEqual((a.aufschlag_vk1, a.aufschlag_vk2, a.aufschlag_vk3), (D("25"), D("25"), D("25")))
        self.assertEqual(stufenpreis(a, 3), D("12.50"))
        r = self.anlegen(nummer="5001", aufschlag_vk1="", aufschlag_vk1_fuer_alle="on")
        self.assertContains(r, "Bitte zuerst Aufschlag VK1 eintragen.", status_code=422)

    def test_fix_vk_oder_aufschlag_je_stufe(self):
        a = Artikel.objects.create(nummer="5000", bezeichnung="Maus", steuersatz=self.st, ek_manuell=D("10"), ek_herkunft="manuell",
                                   vk1=D("39.90"), aufschlag_vk2=D("20"))
        url = reverse("stammdaten:artikel_bearbeiten", args=[a.pk])
        html = self.client.get(url).content.decode()
        # Vorbelegung: VK1 ist fest, VK2/VK3 folgen dem Aufschlag
        self.assertIn('<input type="radio" name="fix_vk1" value="vk" aria-label="VK1 netto fixieren" checked> fix', html)
        self.assertIn('<input type="radio" name="fix_vk1" value="aufschlag" aria-label="Aufschlag VK1 % fixieren"> fix', html)
        self.assertIn('<input type="radio" name="fix_vk2" value="aufschlag" aria-label="Aufschlag VK2 % fixieren" checked> fix', html)
        # VK1 auf Aufschlag umstellen: kein fester VK mehr, der VK folgt dem EK
        r = self.client.post(url, self.daten(ek_manuell="10", vk1="13.00", aufschlag_vk1="30", fix_vk1="aufschlag",
                                             vk2="15.00", aufschlag_vk2="50", fix_vk2="vk"))
        self.assertEqual(r.status_code, 200, r.content[:300])
        a.refresh_from_db()
        self.assertEqual((a.vk1, a.aufschlag_vk1, a.vk2, a.aufschlag_vk2), (None, D("30"), D("15"), D("50")))
        self.assertEqual(stufenpreis(a, 1), D("13.00"))
        a.ek_manuell = D("20")
        a.save()
        self.assertEqual((stufenpreis(a, 1), stufenpreis(a, 2)), (D("26.00"), D("15")))  # Aufschlag fix folgt dem EK, VK fix bleibt
        # VK fixieren ohne VK: klare Meldung
        r = self.client.post(url, self.daten(fix_vk3="vk", vk3=""))
        self.assertContains(r, "Bitte VK3 eintragen oder den Aufschlag fixieren.", status_code=422)

    def test_aufschlag_fix_ohne_ek_oder_aufschlag_behaelt_den_vk(self):
        a = Artikel.objects.create(nummer="5000", bezeichnung="Maus", steuersatz=self.st, vk1=D("39.90"))  # kein EK
        url = reverse("stammdaten:artikel_bearbeiten", args=[a.pk])
        r = self.client.post(url, self.daten(aufschlag_vk1="", vk1="39.90", fix_vk1="aufschlag"))
        self.assertContains(r, "Ohne EK und Aufschlag ergibt sich kein VK1 – bitte den VK fixieren.", status_code=422)
        a.refresh_from_db()
        self.assertEqual(a.vk1, D("39.90"))
        # mit eingetipptem EK (neuer manueller EK) geht es
        r = self.client.post(url, self.daten(ek_manuell="10", aufschlag_vk1="20", vk1="39.90", fix_vk1="aufschlag"))
        self.assertEqual(r.status_code, 200, r.content[:300])
        a.refresh_from_db()
        self.assertEqual((a.vk1, stufenpreis(a, 1)), (None, D("12.00")))

    def test_ungueltiger_aufschlag_mit_fuer_alle_ohne_zusatzmeldung(self):
        r = self.anlegen(aufschlag_vk1="abc", aufschlag_vk1_fuer_alle="on")
        self.assertEqual(r.status_code, 422)
        self.assertNotContains(r, "Bitte zuerst Aufschlag VK1 eintragen.", status_code=422)

    def test_verwaltung_preise_mit_fix_und_umrechnung(self):
        admin = get_user_model().objects.create_superuser("admin", "a@a.de", "x" * 14)
        self.client.force_login(admin)
        a = Artikel.objects.create(nummer="5000", bezeichnung="Maus", steuersatz=self.st, ek_manuell=D("10"), ek_herkunft="manuell", vk1=D("20"))
        url = reverse("admin:stammdaten_artikel_change", args=[a.pk])
        html = self.client.get(url).content.decode()
        for name in ("ek_manuell", "aufschlag_vk1_fuer_alle", "vk3", "aufschlag_vk3"):
            self.assertIn(f'name="{name}"', html)
        self.assertRegex(html, r'name="vk1"[^>]*data-fix="vk"')
        self.assertIn("wwokPreisKopplung(formular)", html)
        daten = {"nummer": "5000", "bezeichnung": "Maus", "steuersatz": self.st.pk, "mengeneinheit": "Stück",
                 "ek_manuell": "10,00", "vk1": "20.00", "aufschlag_vk1": "50", "fix_vk1": "aufschlag",
                 **{f"{p}-{k}": v for p in ("mandantendaten", "lieferanten", "komponenten", "bilder")
                    for k, v in (("TOTAL_FORMS", "0"), ("INITIAL_FORMS", "0"), ("MIN_NUM_FORMS", "0"), ("MAX_NUM_FORMS", "1000"))}}
        r = self.client.post(url, daten)
        self.assertEqual(r.status_code, 302, r.content.decode()[:2000])
        a.refresh_from_db()
        self.assertEqual((a.vk1, a.aufschlag_vk1, stufenpreis(a, 1)), (None, D("50"), D("15.00")))

    def test_fix_wirkt_nicht_bei_leistungen(self):
        l = Artikel.objects.create(nummer="L1", typ="L", bezeichnung="Einrichtung", steuersatz=self.st, vk1=D("50"))
        r = self.client.post(reverse("stammdaten:artikel_bearbeiten", args=[l.pk]),
                             self.daten(nummer="L1", typ="L", vk1="60", fix_vk1="aufschlag"))
        self.assertEqual(r.status_code, 200, r.content[:300])
        l.refresh_from_db()
        self.assertEqual(l.vk1, D("60"))

    def test_nur_fuer_lagerartikel(self):
        self.assertEqual(self.anlegen(typ="L", ek_manuell="5", nummer="L1").status_code, 200)
        self.assertIsNone(Artikel.objects.get(nummer="L1").ek_manuell)
