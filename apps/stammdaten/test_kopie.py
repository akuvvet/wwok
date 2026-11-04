"""Artikel kopieren (Issue 15): Knopf im Artikel-Modal und in Verwaltung → Artikel, eine gemeinsame Kopierlogik."""
import io
import json
import tempfile
from decimal import Decimal as D

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from PIL import Image

from apps.kern.models import Mandant

from . import kopie
from .models import (Artikel, ArtikelBild, ArtikelLieferant, ArtikelMandant, GruppenKomponente, Hersteller, Kategorie, Lieferant, Stammnummer,
                     Steuersatz)

NEU = "stammdaten:artikel_neu"


def png(farbe="red"):
    puffer = io.BytesIO()
    Image.new("RGB", (4, 4), farbe).save(puffer, "PNG")
    return SimpleUploadedFile("a.png", puffer.getvalue(), content_type="image/png")


@override_settings(SECURE_SSL_REDIRECT=False)
class KopieBasis(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.st = Steuersatz.objects.create(bezeichnung="19", prozent=D("19"))
        self.kat = Kategorie.objects.create(name="Mäuse")
        self.hs = Hersteller.objects.create(name="Logitech")
        self.vorlage = Artikel.objects.create(
            nummer="5000", typ="A", bezeichnung="Maus M185", bezeichnung_erweitert="Kabellos", kategorie=self.kat, hersteller=self.hs,
            herstellernummer="910-002", ean="4006381333931", garantie_monate=24, laenge_cm=D("10"), breite_cm=D("6"), hoehe_cm=D("4"),
            gewicht_kg=D("0.095"), steuersatz=self.st, ek_manuell=D("8.50"), vk1=D("15.00"), aufschlag_vk2=D("30"), online=True,
            kurzbeschreibung="Kurz", keywords="maus", meta_titel="Titel", meta_description="Beschr", nicht_aktiv=True,
            durchschnitts_ek=D("7.00"))
        self.md = ArtikelMandant.objects.create(artikel=self.vorlage, mandant=self.m, lagerplatz="R1", mindestbestand=2, maximalbestand=9,
                                                ek2=D("8.10"), ek2_fix=True, vk_fix1=True, letzter_ek=D("7.50"), ek_avg=D("7.40"))
        self.lf = Lieferant.objects.create(nummer="L1", name1="Techdata GmbH", kurzname="Techdata")
        ArtikelLieferant.objects.create(artikel=self.vorlage, lieferant=self.lf, bestellnummer="X1", ek=D("7"))
        Stammnummer.setzen(Stammnummer.Art.ARTIKEL, 5000)  # nächste Nummer des Nummernkreises: 5001
        self.user = get_user_model().objects.create_superuser("chef", password="x" * 14)
        self.client.force_login(self.user)
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    def stand(self, a):
        """Vollständiger Stand eines Artikels samt Mandantendaten, um „unverändert“ zu prüfen."""
        a = Artikel.objects.get(pk=a.pk)
        felder = {f.attname: getattr(a, f.attname) for f in Artikel._meta.concrete_fields}
        md = ArtikelMandant.objects.filter(artikel=a).values().first()
        return felder, md, a.bilder.count(), a.lieferanten.count(), a.komponenten.count()


class KopierLogikTest(KopieBasis):
    def test_uebernommene_und_nicht_uebernommene_felder(self):
        neu = kopie.neuer_artikel(self.vorlage)
        self.assertIsNone(neu.pk)
        self.assertEqual(neu.bezeichnung, "Maus M185 (Kopie)")
        self.assertNotEqual(neu.nummer, "5000")
        for feld in ("typ", "kategorie_id", "hersteller_id", "bezeichnung_erweitert", "garantie_monate", "laenge_cm", "breite_cm", "hoehe_cm",
                     "gewicht_kg", "steuersatz_id", "ek_manuell", "vk1", "aufschlag_vk2", "online", "kurzbeschreibung", "keywords",
                     "meta_titel", "meta_description", "nicht_aktiv"):
            self.assertEqual(getattr(neu, feld), getattr(self.vorlage, feld), feld)
        self.assertEqual((neu.ean, neu.herstellernummer), ("", ""))
        self.assertIsNone(neu.durchschnitts_ek)
        self.assertEqual(neu.ek_herkunft, "")

    def test_bezeichnung_wird_auf_feldlaenge_gekuerzt(self):
        self.vorlage.bezeichnung = "x" * 255
        self.assertEqual(len(kopie.kopie_bezeichnung(self.vorlage.bezeichnung)), 255)
        self.assertTrue(kopie.kopie_bezeichnung(self.vorlage.bezeichnung).endswith(" (Kopie)"))

    def test_mandantwerte_nur_ek2_und_festlegungen(self):
        self.assertEqual(kopie.mandant_werte(self.vorlage, self.m),
                         {"ek2": D("8.10"), "ek2_fix": True, "vk_fix1": True, "vk_fix2": False, "vk_fix3": False})

    def test_p_wird_zum_lagerartikel(self):
        self.vorlage.typ = "P"
        self.assertEqual(kopie.neuer_artikel(self.vorlage).typ, "A")


class KopierenMaskeTest(KopieBasis):
    def kopie_url(self, bilder=False):
        return reverse(NEU) + f"?kopie_von={self.vorlage.pk}" + ("&kopie_bilder=1" if bilder else "")

    def daten(self, **extra):
        d = {"nummer": "5001", "typ": "A", "bezeichnung": "Maus M185 (Kopie)", "mengeneinheit": "Stück", "steuersatz": self.st.pk,
             "kategorie": self.kat.pk, "hersteller": self.hs.pk, "vk1": "15.00", "kopie_von": self.vorlage.pk, "md-lagerplatz": ""}
        d.update(extra)
        return d

    def test_knopf_nur_im_gespeicherten_artikel(self):
        bearbeiten = self.client.get(reverse("stammdaten:artikel_bearbeiten", args=[self.vorlage.pk]))
        self.assertContains(bearbeiten, "data-kopieren=")
        self.assertContains(bearbeiten, f"?kopie_von={self.vorlage.pk}")
        self.assertNotContains(self.client.get(reverse(NEU)), "data-kopieren=")
        self.assertNotContains(self.client.get(self.kopie_url()), "data-kopieren=")  # auch nicht in der Kopie selbst
        self.assertNotContains(self.client.get(reverse("lager:uebersicht")), "data-kopieren=")  # nicht in Listen

    def test_formular_ist_vorausgefuellt_und_legt_nichts_an(self):
        vorher = self.stand(self.vorlage)
        r = self.client.get(self.kopie_url())
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Neuer Artikel")
        self.assertContains(r, "Maus M185 (Kopie)")
        self.assertContains(r, "Kabellos")
        self.assertEqual(r.context["form"].instance.nummer, "5001")  # nächste freie Nummer, nicht die der Vorlage
        self.assertNotContains(r, "910-002")  # Herstellernummer
        self.assertNotContains(r, "4006381333931")  # EAN
        self.assertEqual(r.context["tabs"][-1]["extra"].initial.get("lagerplatz", ""), "")  # Lagerplatz gehört nicht zur Vorlage
        self.assertContains(r, f'name="kopie_von" value="{self.vorlage.pk}"')
        self.assertEqual(Artikel.objects.count(), 1)  # Abbrechen = nichts gespeichert
        self.assertEqual(self.stand(self.vorlage), vorher)

    def test_speichern_legt_artikel_nach_tabelle_an_und_laesst_vorlage_unveraendert(self):
        vorher = self.stand(self.vorlage)
        r = self.client.post(reverse(NEU), self.daten(**{"garantie_monate": "24", "aufschlag_vk2": "30", "ek_manuell": "8,50"}))
        self.assertEqual(r.status_code, 200, r.content[:300])
        neu = Artikel.objects.get(nummer="5001")
        self.assertEqual(json.loads(r.content)["ziel"], reverse("lager:artikel", args=[neu.pk]))
        self.assertEqual((neu.ean, neu.herstellernummer), ("", ""))
        self.assertIsNone(neu.durchschnitts_ek)
        self.assertEqual(neu.ek_manuell, D("8.50"))
        self.assertEqual(neu.ek_herkunft, "manuell")
        md = ArtikelMandant.objects.get(artikel=neu, mandant=self.m)
        self.assertEqual((md.ek2, md.ek2_fix, md.vk_fix1), (D("8.10"), True, True))
        self.assertEqual((md.lagerplatz, md.mindestbestand, md.maximalbestand), ("", None, None))
        self.assertIsNone(md.letzter_ek)
        self.assertIsNone(md.ek_avg)
        self.assertEqual(neu.lieferanten.count(), 0)
        self.assertEqual(self.stand(self.vorlage), vorher)

    def test_unbekannte_vorlage_ergibt_404(self):
        self.assertEqual(self.client.get(reverse(NEU) + "?kopie_von=99999").status_code, 404)

    def test_login_noetig(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.kopie_url()).status_code, 302)

    def test_gruppe_uebernimmt_komponenten(self):
        ram = Artikel.objects.create(nummer="6001", bezeichnung="RAM", steuersatz=self.st, vk1=D("50"))
        gruppe = Artikel.objects.create(nummer="6000", typ="G", bezeichnung="PC", steuersatz=self.st, vk1=D("500"))
        GruppenKomponente.objects.create(gruppe=gruppe, komponente=ram, menge=D("2"))
        r = self.client.get(reverse(NEU) + f"?kopie_von={gruppe.pk}")
        self.assertContains(r, "6001")
        self.assertContains(r, '<option value="G" selected>')
        r = self.client.post(reverse(NEU), self.daten(nummer="6002", typ="G", bezeichnung="PC (Kopie)", kopie_von=gruppe.pk, vk1="500",
                                                      komp_geladen="1", komp_id=[ram.pk], komp_menge=["2"]))
        self.assertEqual(r.status_code, 200, r.content[:300])
        neu = Artikel.objects.get(nummer="6002")
        self.assertEqual([(k.komponente_id, k.menge) for k in neu.komponenten.all()], [(ram.pk, D("2"))])
        self.assertEqual(gruppe.komponenten.count(), 1)

    def test_leistung_wird_kopiert(self):
        leistung = Artikel.objects.create(nummer="7000", typ="L", bezeichnung="Montage", steuersatz=self.st, kosten=D("20"), vk1=D("35"))
        r = self.client.get(reverse(NEU) + f"?kopie_von={leistung.pk}")
        self.assertContains(r, "Montage (Kopie)")
        self.assertContains(r, '<option value="L" selected>')

    def test_bilder_nur_mit_haken_und_als_eigene_dateien(self):
        with tempfile.TemporaryDirectory() as ordner, override_settings(MEDIA_ROOT=ordner):
            bild = ArtikelBild(artikel=self.vorlage, position=1, alt_text="Vorn")
            bild.bild.save("a.png", png(), save=True)
            ohne = self.client.get(self.kopie_url())
            self.assertContains(ohne, "(0/10)")
            mit = self.client.get(self.kopie_url(bilder=True))
            self.assertContains(mit, "(1/10)")
            self.assertContains(mit, 'name="kopie_bilder"')
            # ohne Haken: Galerie leer, nichts wird kopiert
            self.client.post(reverse(NEU), self.daten(bilder_geladen="1", bild_reihe=[]))
            self.assertEqual(ArtikelBild.objects.count(), 1)
            # mit Haken: Bild der Vorlage in der Reihe -> neue Datei für den neuen Artikel
            r = self.client.post(reverse(NEU), self.daten(nummer="5002", kopie_bilder="1", bilder_geladen="1", bild_reihe=[f"b{bild.pk}"],
                                                          **{f"bild_alt_b{bild.pk}": "Vorn"}))
            self.assertEqual(r.status_code, 200, r.content[:300])
            neu = Artikel.objects.get(nummer="5002")
            kopiert = neu.bilder.get()
            self.assertEqual(kopiert.alt_text, "Vorn")
            self.assertNotEqual(kopiert.bild.name, bild.bild.name)  # eigene Datei, nicht verlinkt
            self.assertEqual(self.vorlage.bilder.get().pk, bild.pk)  # Vorlage behält ihr Bild
            self.assertTrue(self.vorlage.bilder.get().bild.storage.exists(bild.bild.name))

    def test_abbrechen_legt_keine_dateien_an(self):
        with tempfile.TemporaryDirectory() as ordner, override_settings(MEDIA_ROOT=ordner):
            bild = ArtikelBild(artikel=self.vorlage, position=1)
            bild.bild.save("a.png", png(), save=True)
            import os
            vorher = sorted(os.listdir(ordner + "/artikel"))
            self.client.get(self.kopie_url(bilder=True))
            self.assertEqual(sorted(os.listdir(ordner + "/artikel")), vorher)
            self.assertEqual(ArtikelBild.objects.count(), 1)


class KopierenVerwaltungTest(KopieBasis):
    def test_knopf_nur_im_geoeffneten_artikel(self):
        aendern = self.client.get(reverse("admin:stammdaten_artikel_change", args=[self.vorlage.pk]))
        self.assertContains(aendern, "data-kopieren")
        self.assertContains(aendern, reverse("admin:stammdaten_artikel_add"))
        self.assertNotContains(self.client.get(reverse("admin:stammdaten_artikel_add")), "data-kopieren")
        self.assertNotContains(self.client.get(reverse("admin:stammdaten_artikel_changelist")), "data-kopieren")

    def test_knopf_braucht_recht_artikel_anlegen(self):
        from django.contrib.auth.models import Permission
        u = get_user_model().objects.create_user("sb", password="x" * 14, is_staff=True)
        u.user_permissions.add(Permission.objects.get(codename="change_artikel"), Permission.objects.get(codename="view_artikel"))
        self.client.force_login(u)
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()
        aendern = self.client.get(reverse("admin:stammdaten_artikel_change", args=[self.vorlage.pk]))
        self.assertEqual(aendern.status_code, 200)
        self.assertNotContains(aendern, "data-kopieren")
        self.assertEqual(self.client.get(reverse("admin:stammdaten_artikel_add") + f"?kopie_von={self.vorlage.pk}").status_code, 403)

    def test_formular_vorausgefuellt_ohne_nummer_ean_herstellernummer(self):
        vorher = self.stand(self.vorlage)
        r = self.client.get(reverse("admin:stammdaten_artikel_add") + f"?kopie_von={self.vorlage.pk}")
        self.assertEqual(r.status_code, 200)
        form = r.context["adminform"].form
        self.assertEqual(form.initial["bezeichnung"], "Maus M185 (Kopie)")
        self.assertEqual(form.initial["nummer"], "5001")
        self.assertEqual(form.initial["kategorie"], self.kat.pk)
        self.assertEqual(form.initial["vk1"], D("15.00"))
        self.assertNotIn("ean", form.initial)
        self.assertNotIn("herstellernummer", form.initial)
        self.assertNotContains(r, "910-002")
        self.assertEqual(Artikel.objects.count(), 1)
        self.assertEqual(self.stand(self.vorlage), vorher)

    def post_daten(self, **extra):
        d = {"nummer": "5001", "typ": "A", "bezeichnung": "Maus M185 (Kopie)", "mengeneinheit": "Stück", "steuersatz": self.st.pk,
             "kategorie": self.kat.pk, "hersteller": self.hs.pk, "vk1": "15,00", "fix_vk1": "vk", "garantie_monate": "24", "nicht_aktiv": "on",
             "ek_manuell": "8,50"}
        for prefix in ("mandantendaten", "lieferanten", "komponenten", "bilder"):
            d.update({f"{prefix}-TOTAL_FORMS": "0", f"{prefix}-INITIAL_FORMS": "0", f"{prefix}-MIN_NUM_FORMS": "0", f"{prefix}-MAX_NUM_FORMS": "1000"})
        d.update(extra)
        return d

    def test_speichern_legt_neuen_artikel_an(self):
        vorher = self.stand(self.vorlage)
        url = reverse("admin:stammdaten_artikel_add") + f"?kopie_von={self.vorlage.pk}"
        r = self.client.post(url, self.post_daten(**{"mandantendaten-TOTAL_FORMS": "1", "mandantendaten-0-mandant": self.m.pk,
                                                     "mandantendaten-0-ek2": "8,10", "mandantendaten-0-ek2_fix": "on"}))
        self.assertEqual(r.status_code, 302, getattr(r, "context_data", None) and r.context_data["errors"])
        neu = Artikel.objects.get(nummer="5001")
        self.assertEqual((neu.ean, neu.herstellernummer), ("", ""))
        self.assertEqual(neu.lieferanten.count(), 0)
        self.assertEqual(self.stand(self.vorlage), vorher)

    def test_bilder_nur_mit_kopie_bilder(self):
        with tempfile.TemporaryDirectory() as ordner, override_settings(MEDIA_ROOT=ordner):
            bild = ArtikelBild(artikel=self.vorlage, position=1, alt_text="Vorn")
            bild.bild.save("a.png", png(), save=True)
            basis = reverse("admin:stammdaten_artikel_add") + f"?kopie_von={self.vorlage.pk}"
            self.client.post(basis, self.post_daten())
            self.assertEqual(Artikel.objects.get(nummer="5001").bilder.count(), 0)
            self.client.post(basis + "&kopie_bilder=1", self.post_daten(nummer="5002"))
            kopiert = Artikel.objects.get(nummer="5002").bilder.get()
            self.assertEqual(kopiert.alt_text, "Vorn")
            self.assertNotEqual(kopiert.bild.name, bild.bild.name)
            self.assertEqual(self.vorlage.bilder.count(), 1)

    def test_gruppe_zeigt_komponenten_im_formular(self):
        ram = Artikel.objects.create(nummer="6001", bezeichnung="RAM", steuersatz=self.st, vk1=D("50"))
        gruppe = Artikel.objects.create(nummer="6000", typ="G", bezeichnung="PC", steuersatz=self.st, vk1=D("500"))
        GruppenKomponente.objects.create(gruppe=gruppe, komponente=ram, menge=D("2"))
        r = self.client.get(reverse("admin:stammdaten_artikel_add") + f"?kopie_von={gruppe.pk}")
        satz = next(s for s in r.context["inline_admin_formsets"] if s.formset.model is GruppenKomponente).formset
        self.assertEqual(satz.forms[0].initial, {"komponente": ram.pk, "menge": D("2")})

    def test_leistung_kopieren_ueber_leistungsartikel(self):
        leistung = Artikel.objects.create(nummer="7000", typ="L", bezeichnung="Montage", steuersatz=self.st, kosten=D("20"), vk1=D("35"))
        aendern = self.client.get(reverse("admin:stammdaten_leistungsartikel_change", args=[leistung.pk]))
        self.assertContains(aendern, "data-kopieren")
        self.assertContains(aendern, reverse("admin:stammdaten_leistungsartikel_add"))
        r = self.client.get(reverse("admin:stammdaten_leistungsartikel_add") + f"?kopie_von={leistung.pk}")
        form = r.context["adminform"].form
        self.assertEqual(form.initial["bezeichnung"], "Montage (Kopie)")
        self.assertEqual(form.initial["kosten"], D("20"))
