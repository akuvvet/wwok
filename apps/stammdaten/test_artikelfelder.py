"""Artikelstamm (Entscheidung 19.11.2026): Kennungen, Preisfelder, Grundpreis, weitere Kategorien, Steuersatz-Standard, Historie."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.einkauf.services import artikel_per_code
from apps.kern.models import Mandant
from apps.lager import views as lager_views

from .forms import ARTIKEL_TABS, ArtikelForm
from .historie import eingabe, historie
from .models import Artikel, Kategorie, Steuersatz, isbn_gueltig, isbn_normalisieren

D = Decimal


@override_settings(SECURE_SSL_REDIRECT=False)
class ArtikelfelderTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.st = Steuersatz.objects.filter(prozent=D("19")).order_by("pk").first() or Steuersatz.objects.create(bezeichnung="19x", prozent=D("19"))
        self.haupt = Kategorie.objects.create(name="Hardware")
        self.zweit = Kategorie.objects.create(name="Angebote")
        self.a = Artikel.objects.create(
            nummer="F1", bezeichnung="Funkmaus", steuersatz=self.st, kategorie=self.haupt, vk1=D("10.00"),
            amazon_asin="B00ABC1234", amazon_sku="OKAY-MAUS-01", amazon_fnsku="X001ABCDEF", isbn="9783161484100", upc="036000291452", ebay_id="123456789012")
        self.a.weitere_kategorien.add(self.zweit)
        self.andere = Artikel.objects.create(nummer="F2", bezeichnung="Tastatur", steuersatz=self.st)
        self.user = get_user_model().objects.create_superuser("chef", password="x" * 14)
        self.client.force_login(self.user)
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    # --- Validierung ---
    def test_isbn_pruefziffer(self):
        self.assertTrue(isbn_gueltig("9783161484100"))
        self.assertTrue(isbn_gueltig("0306406152"))
        self.assertTrue(isbn_gueltig("080442957X"))
        self.assertFalse(isbn_gueltig("9783161484101"))
        self.assertFalse(isbn_gueltig("12345"))
        self.assertEqual(isbn_normalisieren("978-3-16-148410-0"), "9783161484100")

    def test_isbn_mit_bindestrichen_wird_normalisiert_gespeichert(self):
        a = Artikel(nummer="F3", bezeichnung="Buch", steuersatz=self.st, isbn="978-3-16-148410-0")
        a.full_clean()
        self.assertEqual(a.isbn, "9783161484100")

    def test_ungueltige_isbn_wird_abgelehnt(self):
        a = Artikel(nummer="F3", bezeichnung="Buch", steuersatz=self.st, isbn="978-3-16-148410-1")
        with self.assertRaises(ValidationError) as e:
            a.full_clean()
        self.assertIn("isbn", e.exception.message_dict)

    def test_upc_nur_ziffern(self):
        a = Artikel(nummer="F3", bezeichnung="x", steuersatz=self.st, upc="03600029145A")
        with self.assertRaises(ValidationError) as e:
            a.full_clean()
        self.assertIn("upc", e.exception.message_dict)

    def test_preisgrenzen_min_nicht_ueber_max(self):
        a = Artikel(nummer="F3", bezeichnung="x", steuersatz=self.st, vk_min=D("20"), vk_max=D("10"))
        with self.assertRaises(ValidationError) as e:
            a.full_clean()
        self.assertIn("vk_max", e.exception.message_dict)
        Artikel(nummer="F3", bezeichnung="x", steuersatz=self.st, vk_min=D("10"), vk_max=D("10")).full_clean()  # gleich ist erlaubt
        Artikel(nummer="F3", bezeichnung="x", steuersatz=self.st, vk_min=D("10")).full_clean()  # nur eine Grenze ist erlaubt

    def test_sonder_ek_zeitraum_und_preiseinheit(self):
        from datetime import date
        a = Artikel(nummer="F3", bezeichnung="x", steuersatz=self.st, sonder_ek=D("1"), sonder_ek_von=date(2026, 5, 1), sonder_ek_bis=date(2026, 4, 1))
        with self.assertRaises(ValidationError) as e:
            a.full_clean()
        self.assertIn("sonder_ek_bis", e.exception.message_dict)
        a = Artikel(nummer="F3", bezeichnung="x", steuersatz=self.st, preiseinheit=0)
        with self.assertRaises(ValidationError) as e:
            a.full_clean()
        self.assertIn("preiseinheit", e.exception.message_dict)
        self.assertEqual(Artikel(nummer="F4").preiseinheit, 1)

    def test_formular_meldet_fehler_bei_grenzen(self):
        form = ArtikelForm({"nummer": "F9", "typ": "A", "bezeichnung": "x", "steuersatz": self.st.pk, "mengeneinheit": "Stück",
                            "vk_min": "20", "vk_max": "10", "preiseinheit": "1"})
        self.assertFalse(form.is_valid())
        self.assertIn("vk_max", form.errors)

    def test_formular_preiseinheit_leer_wird_1_und_null_abgelehnt(self):
        basis = {"nummer": "F9", "typ": "A", "bezeichnung": "x", "steuersatz": self.st.pk, "mengeneinheit": "Stück",
                 "fix_vk1": "aufschlag", "fix_vk2": "aufschlag", "fix_vk3": "aufschlag"}
        form = ArtikelForm({**basis, "preiseinheit": ""})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save().preiseinheit, 1)
        self.assertIn("preiseinheit", ArtikelForm({**basis, "nummer": "F10", "preiseinheit": "0"}).errors)

    # --- Grundpreis ---
    def test_grundpreis(self):
        a = Artikel(grundpreis_faktor=D("10"), grundpreis_einheit="kg")
        self.assertEqual(a.grundpreis(D("1.25")), D("12.50"))
        self.assertEqual(a.grundpreis_text(D("1.25")), "12,50 € / kg")
        self.assertEqual(Artikel(grundpreis_faktor=D("0.333333"), grundpreis_einheit="l").grundpreis(D("1.00")), D("0.33"))
        self.assertEqual(Artikel(grundpreis_faktor=D("1.5")).grundpreis(D("0.01")), D("0.02"))  # kaufmännisch gerundet (0,015 -> 0,02)
        self.assertEqual(Artikel(grundpreis_faktor=D("1000"), grundpreis_einheit="m").grundpreis_text(D("1234.5")), "1.234.500,00 € / m")

    def test_grundpreis_ohne_faktor_oder_preis(self):
        self.assertIsNone(Artikel().grundpreis(D("5")))
        self.assertIsNone(Artikel(grundpreis_faktor=D("2")).grundpreis(None))
        self.assertEqual(Artikel(grundpreis_faktor=D("2")).grundpreis_text(D("5")), "")  # ohne Einheit kein Text

    # --- Suche ---
    def test_lager_suche_findet_ueber_alle_kennungen(self):
        for wort in ("B00ABC1234", "OKAY-MAUS-01", "X001ABCDEF", "9783161484100", "978-3-16-148410-0", "036000291452", "123456789012"):
            self.assertEqual(list(lager_views.suche(wort)), [self.a], wort)
        self.assertEqual(list(lager_views.suche("gibtesnicht")), [])

    def test_schnellsuche_findet_ueber_kennungen(self):
        for wort in ("B00ABC1234", "9783161484100", "036000291452", "123456789012"):
            treffer = self.client.get(reverse("schnellsuche"), {"q": wort}).json()["treffer"]
            artikel = [t for t in treffer if t["gruppe"] == "Artikel"]
            self.assertEqual([t["titel"] for t in artikel], ["F1 – Funkmaus"], wort)
        zusatz = self.client.get(reverse("schnellsuche"), {"q": "B00ABC1234"}).json()["treffer"][0]["zusatz"]
        self.assertEqual(zusatz, "ASIN B00ABC1234")

    def test_belegmaske_suche_findet_ueber_kennungen(self):
        for wort in ("B00ABC1234", "9783161484100", "036000291452", "123456789012"):
            treffer = self.client.get(reverse("verkauf:api_artikel"), {"q": wort}).json()["treffer"]
            self.assertEqual([t["nummer"] for t in treffer], ["F1"], wort)

    def test_tabellensuche_und_komponenten_suche(self):
        r = self.client.get(reverse("stammdaten:artikel_tabelle"), {"q": "B00ABC1234"}).json()
        self.assertEqual([t["nummer"] for t in r["treffer"]], ["F1"])

    def test_wareneingang_scan_findet_ueber_kennungen(self):
        for code in ("B00ABC1234", "OKAY-MAUS-01", "X001ABCDEF", "9783161484100", "978-3-16-148410-0", "036000291452", "123456789012"):
            self.assertEqual(artikel_per_code(code), self.a, code)
        self.assertIsNone(artikel_per_code("unbekannt"))
        self.assertIsNone(artikel_per_code("---"))  # nur Bindestriche darf nicht jeden Artikel ohne ISBN treffen

    # --- weitere Kategorien ---
    def test_kategoriefilter_findet_ueber_weitere_kategorien(self):
        alle = Artikel.objects.all()
        self.assertEqual(sorted(lager_views.nach_kategorie(alle, str(self.zweit.pk)).values_list("nummer", flat=True)), ["F1"])
        self.assertEqual(list(lager_views.nach_kategorie(alle, str(self.haupt.pk)).values_list("nummer", flat=True)), ["F1"])
        self.assertEqual(alle.count(), lager_views.nach_kategorie(alle, "").count())  # ohne Kategorie kein Filter

    def test_kategoriefilter_ohne_doppelte_treffer(self):
        unter = Kategorie.objects.create(name="Mäuse", eltern=self.haupt)
        self.a.weitere_kategorien.add(unter)  # Haupt- und weitere Kategorie liegen beide im Baum der Hardware
        self.assertEqual(lager_views.nach_kategorie(Artikel.objects.all(), str(self.haupt.pk)).count(), 1)

    def test_artikelliste_filtert_ueber_weitere_kategorie(self):
        r = self.client.get(reverse("lager:uebersicht"), {"kategorie": self.zweit.pk})
        self.assertEqual([a.nummer for a in r.context["seite"].object_list], ["F1"])

    def test_formular_speichert_weitere_kategorien(self):
        daten = {"nummer": "F9", "typ": "A", "bezeichnung": "Neu", "steuersatz": self.st.pk, "mengeneinheit": "Stück", "preiseinheit": "1",
                 "kategorie": self.haupt.pk, "weitere_kategorien": [self.zweit.pk], "fix_vk1": "aufschlag", "fix_vk2": "aufschlag", "fix_vk3": "aufschlag"}
        form = ArtikelForm(daten)
        self.assertTrue(form.is_valid(), form.errors)
        neu = form.save()
        self.assertEqual(list(neu.weitere_kategorien.all()), [self.zweit])

    # --- Formular ---
    def test_steuersatz_im_neuen_formular_mit_19_prozent_vorbelegt(self):
        form = ArtikelForm()
        self.assertEqual(form["steuersatz"].value(), self.st.pk)

    def test_steuersatz_vorbelegung_nur_fuer_neue_artikel(self):
        sieben = Steuersatz.objects.create(bezeichnung="7x", prozent=D("7"))
        a = Artikel.objects.create(nummer="F7", bezeichnung="Buch", steuersatz=sieben)
        self.assertEqual(ArtikelForm(instance=a)["steuersatz"].value(), sieben.pk)
        self.assertEqual(ArtikelForm(instance=Artikel(steuersatz=sieben))["steuersatz"].value(), sieben.pk)  # Kopie behält ihren Satz

    def test_steuersatz_ohne_19_prozent_bleibt_leer(self):
        Artikel.objects.all().delete()
        Steuersatz.objects.filter(prozent=D("19")).delete()
        self.assertIsNone(ArtikelForm()["steuersatz"].value())

    def test_felder_stehen_in_den_reitern(self):
        tabs = {k: f for k, _, f in ARTIKEL_TABS}
        self.assertEqual(tabs["kennungen"], ["amazon_asin", "amazon_sku", "amazon_fnsku", "isbn", "upc", "ebay_id"])
        for feld in ("uvp", "ek3", "sonder_ek", "sonder_ek_von", "sonder_ek_bis", "vk_min", "vk_max", "preiseinheit", "grundpreis_faktor", "grundpreis_einheit"):
            self.assertIn(feld, tabs["preise"])
        self.assertEqual(tabs["grund"][tabs["grund"].index("kategorie") + 1], "weitere_kategorien")

    # --- Anzeige und Historie ---
    def test_historie_zeigt_preisaenderung_alt_neu(self):
        self.a.refresh_from_db()
        self.a.vk1 = D("12.50")
        self.a.save()
        eintraege = historie(self.a, self.m)
        self.assertEqual(eintraege[0]["aktion"], "geändert")
        zeile = [z for z in eintraege[0]["aenderungen"] if z[0] == "VK1 netto"]
        self.assertEqual(len(zeile), 1)
        self.assertTrue(zeile[0][1].startswith("10"))
        self.assertEqual(zeile[0][2], "12.50")
        self.assertEqual(len(eintraege[0]["aenderungen"]), 1)  # nur das geänderte Feld, nicht Zeitstempel oder Gleiches
        self.assertEqual(eintraege[-1]["aktion"], "angelegt")

    def test_historie_enthaelt_nur_den_eigenen_artikel_und_aktiven_mandanten(self):
        from .models import ArtikelMandant
        m2 = Mandant.objects.create(kurzname="ZONE", firmenname="ZONE Handel")
        ArtikelMandant.objects.create(artikel=self.a, mandant=self.m, lagerplatz="A1")
        ArtikelMandant.objects.create(artikel=self.a, mandant=m2, lagerplatz="B2")
        self.andere.bezeichnung = "Tastatur neu"
        self.andere.save()
        eintraege = historie(self.a, self.m)
        bereiche = [e["bereich"] for e in eintraege]
        self.assertEqual(bereiche.count("Mandantendaten"), 1)  # nur der aktive Mandant
        self.assertEqual(sum(1 for e in historie(self.a, m2) if e["bereich"] == "Mandantendaten"), 1)
        self.assertFalse(any("Tastatur" in str(z) for e in eintraege for z in e["aenderungen"]))

    def test_historie_begrenzt(self):
        for i in range(5):
            self.a.vk1 = D(i + 20)
            self.a.save()
        self.assertEqual(len(historie(self.a, self.m, grenze=3)), 3)

    def test_eingabe_zeigt_zeitpunkt(self):
        am, von = eingabe(self.a)
        self.assertEqual(am, self.a.angelegt_am)
        self.assertIsNone(von)  # im Test ohne angemeldeten Benutzer protokolliert

    def test_artikelseite_zeigt_kennungen_historie_und_grundpreis(self):
        self.a.grundpreis_faktor, self.a.grundpreis_einheit = D("10"), "kg"
        self.a.uvp, self.a.vk_min, self.a.vk_max = D("15.00"), D("8.00"), D("14.00")
        self.a.save()
        r = self.client.get(reverse("lager:artikel", args=[self.a.pk]))
        self.assertEqual(r.status_code, 200)
        for text in ("B00ABC1234", "OKAY-MAUS-01", "X001ABCDEF", "9783161484100", "036000291452", "123456789012", "Eingabedatum", "Letzte Änderung",
                     "100,00 € / kg", "Historie", "Angebote"):
            self.assertContains(r, text)
        self.assertContains(r, "119,00 € / kg")  # Grundpreis brutto aus VK1 10,00 netto + 19 %
