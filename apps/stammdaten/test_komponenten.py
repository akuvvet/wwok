"""Gruppenartikel: Komponenten in der Artikelmaske pflegen, mit EK, VK und Marge."""
from decimal import Decimal as D

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.kern.models import Mandant

from .models import Artikel, GruppenKomponente, Steuersatz
from .preise import gruppen_kalkulation, marge


@override_settings(SECURE_SSL_REDIRECT=False)
class KomponentenTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.st = Steuersatz.objects.create(bezeichnung="19", prozent=D("19"))
        self.ram = Artikel.objects.create(nummer="22699", bezeichnung="DDR4 8GB", steuersatz=self.st, durchschnitts_ek=D("55.17"), vk1=D("70"))
        self.ssd = Artikel.objects.create(nummer="23560", bezeichnung="SSD 500GB", steuersatz=self.st, durchschnitts_ek=D("41.04"), vk1=D("55"))
        self.montage = Artikel.objects.create(nummer="40098", typ="L", bezeichnung="Montage", steuersatz=self.st, vk1=D("35"))
        self.pc = Artikel.objects.create(nummer="23282", typ="G", bezeichnung="PC Gaming", steuersatz=self.st, vk1=D("200"))
        self.user = get_user_model().objects.create_user("t", password="x" * 14, is_staff=True)
        self.client.force_login(self.user)
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()
        self.url = reverse("stammdaten:artikel_bearbeiten", args=[self.pc.pk])

    def daten(self, artikel, **mehr):
        return {"nummer": artikel.nummer, "typ": artikel.typ, "bezeichnung": artikel.bezeichnung, "steuersatz": self.st.pk, "mengeneinheit": "Stück",
                "vk1": "200", "komp_geladen": "1", **mehr}

    def test_marge_wie_in_mention(self):
        self.assertEqual(marge(D("842.92"), D("859")), D("1.87"))
        self.assertIsNone(marge(None, D("10")))
        self.assertIsNone(marge(D("10"), D("0")))

    def test_suche_ohne_gruppen_mit_ek_und_vk(self):
        url = reverse("stammdaten:komponenten_suche")
        t = self.client.get(url, {"q": "ddr4"}).json()["treffer"][0]
        self.assertEqual((t["nummer"], D(t["ek"]), D(t["vk"])), ("22699", D("55.17"), D("70")))
        self.assertEqual(self.client.get(url, {"q": "montage"}).json()["treffer"][0]["ek"], "")
        self.assertEqual(self.client.get(url, {"q": "gaming"}).json()["treffer"], [])
        self.assertEqual(self.client.get(url, {"q": "x"}).json()["treffer"], [])

    def test_komponenten_speichern_aendern_entfernen(self):
        r = self.client.post(self.url, self.daten(self.pc, komp_id=[self.ram.pk, self.ssd.pk], komp_menge=["2", "1,5"]))
        self.assertEqual(r.status_code, 200, r.content[:300])
        self.assertEqual(sorted(self.pc.komponenten.values_list("komponente__nummer", "menge")), [("22699", D("2.000")), ("23560", D("1.500"))])
        self.pc.refresh_from_db()
        self.assertEqual(self.pc.ek_errechnet, D("171.90"))  # 2 × 55,17 + 1,5 × 41,04
        self.client.post(self.url, self.daten(self.pc, komp_id=[self.ssd.pk], komp_menge=["1"]))
        self.assertEqual(self.pc.komponenten.get().komponente, self.ssd)
        self.client.post(self.url, self.daten(self.pc))
        self.assertFalse(self.pc.komponenten.exists())

    def test_neue_gruppe_direkt_mit_komponenten(self):
        daten = self.daten(Artikel(nummer="500", typ="G", bezeichnung="Bundle"), komp_id=[self.ram.pk], komp_menge=["1"])
        r = self.client.post(reverse("stammdaten:artikel_neu"), daten)
        self.assertEqual(r.status_code, 200, r.content[:300])
        self.assertEqual(Artikel.objects.get(nummer="500").komponenten.get().komponente, self.ram)

    def test_ungueltige_komponenten_speichern_nichts(self):
        faelle = (([self.pc.pk], ["1"], "keine weitere Gruppe"), ([self.ram.pk], ["0"], "größer als 0"), ([self.ram.pk], ["abc"], "größer als 0"),
                  ([self.ram.pk], ["NaN"], "größer als 0"), (["999999"], ["1"], "nicht gefunden"))
        for ids, mengen, text in faelle:
            r = self.client.post(self.url, self.daten(self.pc, bezeichnung="Geändert", komp_id=ids, komp_menge=mengen))
            self.assertContains(r, text, status_code=422)
        self.pc.refresh_from_db()
        self.assertEqual(self.pc.bezeichnung, "PC Gaming")
        self.assertFalse(self.pc.komponenten.exists())

    def test_gruppenpreis_wird_mit_vk1_bis_vk3_gespeichert(self):
        self.client.post(self.url, self.daten(self.pc, vk1="499.00", vk2="499.00", vk3="499.00", komp_id=[self.ram.pk], komp_menge=["1"]))
        self.pc.refresh_from_db()
        self.assertEqual((self.pc.vk1, self.pc.vk2, self.pc.vk3), (D("499.00"), D("499.00"), D("499.00")))

    def test_gleicher_artikel_wird_zusammengefasst(self):
        self.client.post(self.url, self.daten(self.pc, komp_id=[self.ram.pk, self.ram.pk], komp_menge=["1", "2"]))
        self.assertEqual(self.pc.komponenten.get().menge, D("3.000"))

    def test_anderer_typ_ignoriert_komponenten(self):
        url = reverse("stammdaten:artikel_bearbeiten", args=[self.ram.pk])
        self.client.post(url, self.daten(self.ram, komp_id=[self.ssd.pk], komp_menge=["1"]))
        self.assertFalse(GruppenKomponente.objects.exists())

    def test_kalkulation_und_artikelseite(self):
        GruppenKomponente.objects.create(gruppe=self.pc, komponente=self.ram, menge=D("2"))
        GruppenKomponente.objects.create(gruppe=self.pc, komponente=self.montage, menge=D("1"))
        k = gruppen_kalkulation(self.pc)
        self.assertEqual((k["ek"], k["vk"], k["vollstaendig"]), (D("110.34"), D("175.00"), True))  # Leistung zählt mit 0 € EK
        self.assertEqual(k["preis_marge"], D("44.83"))
        r = self.client.get(reverse("lager:artikel", args=[self.pc.pk]))
        self.assertContains(r, "<h2>Komponenten</h2>")
        self.assertContains(r, "Gruppenpreis (VK1)")
        self.assertContains(r, "44,83 %")

    def test_leistung_blockiert_gruppen_ek_nicht(self):
        GruppenKomponente.objects.create(gruppe=self.pc, komponente=self.ram, menge=D("2"))
        GruppenKomponente.objects.create(gruppe=self.pc, komponente=self.montage, menge=D("1"))
        self.assertEqual(self.pc.ek_errechnet, D("110.34"))

    def test_fehlender_ek_ist_markiert_und_leere_gruppe_hat_keine_marge(self):
        k = gruppen_kalkulation(self.pc)
        self.assertEqual((k["marge"], k["preis_marge"]), (None, None))
        ohne = Artikel.objects.create(nummer="99", bezeichnung="Neu", steuersatz=self.st, vk1=D("10"))
        GruppenKomponente.objects.create(gruppe=self.pc, komponente=ohne, menge=D("1"))
        self.assertFalse(gruppen_kalkulation(self.pc)["vollstaendig"])
        self.assertContains(self.client.get(reverse("lager:artikel", args=[self.pc.pk])), "fehlt EK oder VK")

    def test_ungueltige_zeile_bleibt_in_der_maske(self):
        GruppenKomponente.objects.create(gruppe=self.pc, komponente=self.ram, menge=D("2"))
        GruppenKomponente.objects.create(gruppe=self.pc, komponente=self.ssd, menge=D("1"))
        r = self.client.post(self.url, self.daten(self.pc, komp_id=[self.ram.pk, self.ssd.pk], komp_menge=["2", "abc"]))
        self.assertContains(r, '"nummer": "23560"', status_code=422)  # sonst würde das nächste Speichern sie löschen
        self.assertContains(r, '"menge": "abc"', status_code=422)
        self.assertEqual(self.pc.komponenten.count(), 2)

    def test_ohne_geladenen_reiter_bleiben_komponenten_unberuehrt(self):
        GruppenKomponente.objects.create(gruppe=self.pc, komponente=self.ram, menge=D("2"))
        daten = self.daten(self.pc)
        del daten["komp_geladen"]
        self.assertEqual(self.client.post(self.url, daten).status_code, 200)
        self.assertEqual(self.pc.komponenten.count(), 1)

    def test_tausenderpunkt_und_protokoll_nur_bei_aenderung(self):
        k = GruppenKomponente.objects.create(gruppe=self.pc, komponente=self.ram, menge=D("2"))
        self.client.post(self.url, self.daten(self.pc, komp_id=[self.ram.pk], komp_menge=["1.234,5"]))
        k2 = self.pc.komponenten.get()
        self.assertEqual((k2.pk, k2.menge), (k.pk, D("1234.500")))  # Zeile bleibt erhalten, nur die Menge ändert sich

    def test_genauer_ek_in_der_kalkulation(self):
        klein = Artikel.objects.create(nummer="77", bezeichnung="Schraube", steuersatz=self.st, durchschnitts_ek=D("0.0449"), vk1=D("0.05"))
        GruppenKomponente.objects.create(gruppe=self.pc, komponente=klein, menge=D("100"))
        self.assertEqual(gruppen_kalkulation(self.pc)["ek"], D("4.4900"))

    def test_maske_hat_reiter_mit_vorhandenen_komponenten(self):
        GruppenKomponente.objects.create(gruppe=self.pc, komponente=self.ram, menge=D("2"))
        r = self.client.get(self.url)
        self.assertContains(r, 'data-modaltab="komponenten"')
        self.assertContains(r, "data-preis-uebernehmen")  # Gruppenpreis oder Aufschlag nach VK1–VK3
        self.assertContains(r, "data-aufschlag-eingabe")
        self.assertContains(r, '"nummer": "22699"')
        self.assertEqual(self.client.get(reverse("stammdaten:artikel_neu")).status_code, 200)
