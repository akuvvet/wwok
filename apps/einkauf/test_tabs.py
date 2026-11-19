"""Wunsch des Nutzers 18.11.2026: Lieferantenbestellung und Wareneingang mit Tabs wie die Kundenbestellung.
Bestellung: Positionen, Bedarf, Versand & Konditionen, Texte & Notizen, Wareneingänge.
Wareneingang: Positionen, Bestellungen (laufende, komplett oder einzeln übernehmen), Lieferschein & Kosten, Rücksendungen."""
from decimal import Decimal as D

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.kern.models import Mandant
from apps.lager.models import Bewegungsart, bestand
from apps.lager.services import buche_manuell
from apps.stammdaten.models import Artikel, ArtikelLieferant, Kunde, Lieferant, Preislistenartikel, Steuersatz
from apps.verkauf.models import Beleg, Belegart, Position

from . import services
from .models import BestellStatus, Bestellung
from .pdf import bestellung_pdf


class Basis(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.lf = Lieferant.objects.create(nummer="L1", name1="Techdata GmbH", kurzname="TD")
        self.lf2 = Lieferant.objects.create(nummer="L2", name1="Wave AG", kurzname="WAVE")
        st = Steuersatz.objects.get(nummer=1)
        self.maus = Artikel.objects.create(nummer="M1", bezeichnung="Maus", steuersatz=st, vk1=D("15"))
        self.tast = Artikel.objects.create(nummer="T1", bezeichnung="Tastatur", steuersatz=st, vk1=D("25"))
        self.kabel = Artikel.objects.create(nummer="K1", bezeichnung="Kabel", steuersatz=st, vk1=D("5"))
        ArtikelLieferant.objects.create(artikel=self.maus, lieferant=self.lf, bestellnummer="TD-M", ek=D("9"), bestand=40)
        ArtikelLieferant.objects.create(artikel=self.maus, lieferant=self.lf2, bestellnummer="W-M", ek=D("8"))
        ArtikelLieferant.objects.create(artikel=self.tast, lieferant=self.lf, bestellnummer="TD-T", ek=D("18"))
        ArtikelLieferant.objects.create(artikel=self.kabel, lieferant=self.lf2, bestellnummer="W-K", ek=D("2"))
        self.kunde = Kunde.objects.create(nummer="1000", name1="Muster GmbH")
        self.client.force_login(get_user_model().objects.create_superuser("chef", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    def kundenbestellung(self, artikel, menge):
        b = Beleg.objects.create(mandant=self.m, art=Belegart.AUFTRAG, kunde=self.kunde)
        Position.objects.create(beleg=b, artikel=artikel, bezeichnung=artikel.bezeichnung, menge=D(menge), einzelpreis=D("10"))
        return b

    def bestellt(self, *zeilen, lieferant=None):
        b = services.bestellung_anlegen(self.m, lieferant or self.lf)
        for artikel, menge in zeilen:
            services.bestellposition(b, artikel, D(menge))
        b.status = BestellStatus.BESTELLT
        b.save()
        return b


class BestellungTabsTest(Basis):
    def test_tabs_vorhanden(self):
        b = services.bestellung_anlegen(self.m, self.lf)
        r = self.client.get(reverse("einkauf:bestellung", args=[b.pk]))
        for tab in ("positionen", "bedarf", "versand", "texte", "wareneingaenge"):
            self.assertContains(r, f'data-tab="{tab}"')
            self.assertContains(r, f'id="tab-{tab}"')
        self.assertContains(r, 'id="positionsform"')  # Artikel hinzufügen bleibt im Tab Positionen

    def test_bedarf_zeigt_gesamten_bedarf_filter_auf_lieferant_bleibt(self):
        """Wunsch des Nutzers 19.11.2026: der Tab zeigt den gesamten Bedarf, nicht nur Artikel dieses Lieferanten."""
        self.kundenbestellung(self.maus, "3")
        self.kundenbestellung(self.kabel, "2")  # führt nur WAVE
        b = services.bestellung_anlegen(self.m, self.lf)
        alle = services.bedarf_fuer_lieferant(b)
        self.assertEqual({z["artikel"] for z in alle}, {self.maus, self.kabel})
        self.assertFalse(next(z for z in alle if z["artikel"] == self.kabel)["fuehrt"])
        zeilen = services.bedarf_fuer_lieferant(b, nur_lieferant=True)
        self.assertEqual([z["artikel"] for z in zeilen], [self.maus])
        z = zeilen[0]
        self.assertEqual((z["menge"], z["ek_hier"], z["bestellnr_hier"], z["bestand_hier"]), (D("3"), D("9"), "TD-M", 40))
        self.assertTrue(z["guenstiger"])  # WAVE hat 8 statt 9
        url = reverse("einkauf:bestellung", args=[b.pk])
        r = self.client.get(url)
        self.assertEqual({z["artikel"] for z in r.context["bedarf"]}, {self.maus, self.kabel})
        self.assertContains(r, f'name="menge_{self.maus.pk}" value="3"')
        self.assertContains(r, f'name="auswahl" value="{self.maus.pk}" aria-label="M1 übernehmen" checked')
        self.assertContains(r, f'name="auswahl" value="{self.kabel.pk}" aria-label="K1 übernehmen">')  # führt TD nicht: kein Haken
        self.assertContains(r, 'name="bedarf" value="lieferant"  onchange')  # Haken „nur Artikel, die TD führt“, nicht gesetzt
        r = self.client.get(url, {"bedarf": "lieferant"})
        self.assertEqual([z["artikel"] for z in r.context["bedarf"]], [self.maus])
        self.assertContains(r, 'name="bedarf" value="lieferant" checked onchange')
        self.assertContains(r, "nur Artikel, die TD führt</label>")
        self.assertContains(r, 'id="bedarf-alle"', count=1)  # Kopf-Haken „alle / keine“
        self.assertContains(r, "alle.indeterminate")
        # Summe der Auswahl: EK bei TD je Zeile (leer, wenn TD den Artikel nicht führt), Anzeige im Tab und als Endsumme im Kopf
        self.assertContains(r, '<tr data-ek="9.0000">')
        self.assertContains(r, 'id="bedarf-endsumme" hidden')
        self.assertContains(r, 'data-netto="0"')
        self.assertContains(r, 'id="bedarf-auswahl"')

    def test_bedarf_zeigt_guenstigste_lieferanten_je_position(self):
        self.kundenbestellung(self.maus, "3")
        self.kundenbestellung(self.kabel, "2")
        self.kundenbestellung(self.tast, "1")
        lf3 = Lieferant.objects.create(nummer="L3", name1="Alpha KG", kurzname="ALPHA")
        lf4 = Lieferant.objects.create(nummer="L4", name1="Billig OHG", kurzname="BILLIG")
        lf5 = Lieferant.objects.create(nummer="L5", name1="Gesperrt AG", kurzname="GESP", gesperrt=True)
        ArtikelLieferant.objects.create(artikel=self.maus, lieferant=lf3, bestellnummer="A-M", ek=D("7"), bestand=0)   # billig, aber nicht lieferbar
        ArtikelLieferant.objects.create(artikel=self.maus, lieferant=lf4, bestellnummer="B-M", ek=D("8.50"), bestand=3)
        ArtikelLieferant.objects.create(artikel=self.maus, lieferant=lf5, bestellnummer="G-M", ek=D("1"))             # gesperrt
        b = services.bestellung_anlegen(self.m, self.lf)
        zeilen = {z["artikel"]: z for z in services.bedarf_fuer_lieferant(b)}
        self.assertEqual([(g["lieferant"].kurz, g["ek"], g["hier"]) for g in zeilen[self.maus]["guenstigste"]],
                         [("WAVE", D("8"), False), ("BILLIG", D("8.50"), False), ("TD", D("9"), True)])  # höchstens 3, Bestand 0 zuletzt
        self.assertEqual([g["lieferant"].kurz for g in zeilen[self.kabel]["guenstigste"]], ["WAVE"])
        self.assertEqual([(g["lieferant"].kurz, g["hier"]) for g in zeilen[self.tast]["guenstigste"]], [("TD", True)])
        r = self.client.get(reverse("einkauf:bestellung", args=[b.pk]))
        self.assertContains(r, "Günstigste Lieferanten")
        self.assertContains(r, "WAVE: 8,00")
        self.assertContains(r, "BILLIG: 8,50")
        self.assertNotContains(r, "GESP")

    def test_bedarf_ohne_lieferant_in_den_preislisten(self):
        ohne = Artikel.objects.create(nummer="O1", bezeichnung="Ohne Preisliste", steuersatz=Steuersatz.objects.get(nummer=1), vk1=D("5"))
        self.kundenbestellung(ohne, "1")
        b = services.bestellung_anlegen(self.m, self.lf)
        self.assertEqual(services.bedarf_fuer_lieferant(b)[0]["guenstigste"], [])
        r = self.client.get(reverse("einkauf:bestellung", args=[b.pk]))
        self.assertContains(r, "kein Lieferant in den Preislisten")
        self.assertContains(r, '<tr data-ek="">')  # ohne Preis bei diesem Lieferanten: zählt nicht in die Summe
        leer = services.bestellung_anlegen(self.m, self.lf2)
        Position.objects.filter(artikel=ohne).delete()  # kein Bedarf mehr: kein Kopf-Haken
        self.assertNotContains(self.client.get(reverse("einkauf:bestellung", args=[leer.pk])), 'id="bedarf-alle"')

    def test_bedarf_uebernehmen_und_danach_weg(self):
        self.kundenbestellung(self.maus, "3")
        b = services.bestellung_anlegen(self.m, self.lf)
        r = self.client.post(reverse("einkauf:bestellung_bedarf", args=[b.pk]),
                             {"auswahl": [self.maus.pk], f"menge_{self.maus.pk}": "4"})
        self.assertRedirects(r, reverse("einkauf:bestellung", args=[b.pk]) + "#positionen", fetch_redirect_response=False)
        pos = b.positionen.get()
        self.assertEqual((pos.artikel, pos.menge, pos.ek, pos.bestellnummer), (self.maus, D("4"), D("9.00"), "TD-M"))
        self.assertEqual(services.bedarf_fuer_lieferant(b), [])  # Entwurf zählt als bestellt

    def test_bedarf_ohne_auswahl_meldet_fehler(self):
        b = services.bestellung_anlegen(self.m, self.lf)
        r = self.client.post(reverse("einkauf:bestellung_bedarf", args=[b.pk]), {}, follow=True)
        self.assertContains(r, "mindestens einen Artikel")
        self.assertFalse(b.positionen.exists())

    def test_bedarf_tab_fehlt_bei_gelieferter_bestellung(self):
        b = services.bestellung_anlegen(self.m, self.lf)
        Bestellung.objects.filter(pk=b.pk).update(status=BestellStatus.GELIEFERT)
        self.assertNotContains(self.client.get(reverse("einkauf:bestellung", args=[b.pk])), 'data-tab="bedarf"')

    def test_versand_und_texte_speichern_und_pdf(self):
        b = services.bestellung_anlegen(self.m, self.lf)
        services.bestellposition(b, self.maus, D("2"))
        r = self.client.post(reverse("einkauf:bestellung_kopf", args=[b.pk]), {
            "tab": "versand", "liefertermin": "2026-12-01", "bezugnahme": "Angebot 4711", "versandart": "Spedition",
            "versandkosten": "12,50", "verpackung": "", "versicherung": "1,20"})
        self.assertRedirects(r, reverse("einkauf:bestellung", args=[b.pk]) + "#versand", fetch_redirect_response=False)
        self.client.post(reverse("einkauf:bestellung_kopf", args=[b.pk]), {"tab": "texte", "bemerkung": "Bitte mit Lieferschein", "notiz": "intern"})
        b.refresh_from_db()
        self.assertEqual((str(b.liefertermin), b.bezugnahme, b.versandart), ("2026-12-01", "Angebot 4711", "Spedition"))
        self.assertEqual((b.versandkosten, b.verpackung, b.versicherung, b.nebenkosten), (D("12.50"), D("0.00"), D("1.20"), D("13.70")))
        self.assertEqual((b.bemerkung, b.notiz, b.liefertermin.year), ("Bitte mit Lieferschein", "intern", 2026))
        self.assertTrue(bestellung_pdf(b).startswith(b"%PDF"))

    def test_falsche_eingaben(self):
        b = services.bestellung_anlegen(self.m, self.lf)
        r = self.client.post(reverse("einkauf:bestellung_kopf", args=[b.pk]), {"versandkosten": "-1"}, follow=True)
        self.assertContains(r, "dürfen nicht negativ sein")
        r = self.client.post(reverse("einkauf:bestellung_kopf", args=[b.pk]), {"liefertermin": "morgen"}, follow=True)
        self.assertContains(r, "als Datum")
        b.refresh_from_db()
        self.assertEqual((b.versandkosten, b.liefertermin), (D("0"), None))

    def test_erster_wareneingang_uebernimmt_die_nebenkosten(self):
        b = self.bestellt((self.maus, "5"))
        Bestellung.objects.filter(pk=b.pk).update(versandkosten=D("9.90"), versicherung=D("1"))
        b.refresh_from_db()
        we = services.wareneingang_anlegen(self.m, self.lf, b)
        self.assertEqual((we.versandkosten, we.versicherung, we.verpackung), (D("9.90"), D("1.00"), D("0")))
        zweiter = services.wareneingang_anlegen(self.m, self.lf, b)
        self.assertEqual(zweiter.versandkosten, D("0"))  # nur beim ersten

    def test_fremder_mandant(self):
        b = services.bestellung_anlegen(self.m, self.lf)
        zone = Mandant.objects.create(kurzname="ZONE", firmenname="ZONE Handel")
        s = self.client.session
        s["mandant_id"] = zone.pk
        s.save()
        self.assertEqual(self.client.post(reverse("einkauf:bestellung_kopf", args=[b.pk]), {"notiz": "x"}).status_code, 404)
        self.assertEqual(self.client.post(reverse("einkauf:bestellung_bedarf", args=[b.pk]), {}).status_code, 404)


class WareneingangTabsTest(Basis):
    def test_laufende_bestellungen_des_lieferanten(self):
        b1 = self.bestellt((self.maus, "5"), (self.tast, "2"))
        b2 = self.bestellt((self.maus, "1"))
        self.bestellt((self.kabel, "9"), lieferant=self.lf2)  # anderer Lieferant
        entwurf = services.bestellung_anlegen(self.m, self.lf)
        services.bestellposition(entwurf, self.maus, D("7"))  # Entwurf: noch nicht bestellt
        we = services.wareneingang_anlegen(self.m, self.lf)
        gruppen = services.laufende_bestellungen(we)
        self.assertEqual([g["bestellung"] for g in gruppen], [b1, b2])
        self.assertEqual([bp.rest for bp in gruppen[0]["positionen"]], [D("5"), D("2")])
        r = self.client.get(reverse("einkauf:wareneingang", args=[we.pk]))
        self.assertContains(r, 'data-tab="bestellungen"')
        self.assertContains(r, f'name="komplett" value="{b1.pk}"')

    def test_komplett_uebernehmen(self):
        b1 = self.bestellt((self.maus, "5"), (self.tast, "2"))
        we = services.wareneingang_anlegen(self.m, self.lf)
        r = self.client.post(reverse("einkauf:wareneingang_uebernehmen", args=[we.pk]), {"komplett": b1.pk})
        self.assertRedirects(r, reverse("einkauf:wareneingang", args=[we.pk]) + "#positionen", fetch_redirect_response=False)
        we.refresh_from_db()
        self.assertEqual(we.bestellung, b1)  # ohne Bestellung angelegt: die übernommene gilt als Bezug
        self.assertEqual(sorted((p.artikel.nummer, p.menge) for p in we.positionen.all()), [("M1", D("5")), ("T1", D("2"))])
        self.assertEqual(sum(bp.rest for g in services.laufende_bestellungen(we) for bp in g["positionen"]), D("0"))

    def test_einzelne_positionen_teillieferung_und_buchen(self):
        b1 = self.bestellt((self.maus, "5"), (self.tast, "2"))
        we = services.wareneingang_anlegen(self.m, self.lf, b1)
        maus_bp = b1.positionen.get(artikel=self.maus)
        self.client.post(reverse("einkauf:wareneingang_uebernehmen", args=[we.pk]), {"bp": [maus_bp.pk], f"menge_{maus_bp.pk}": "3"})
        self.assertEqual([(p.artikel, p.menge) for p in we.positionen.all()], [(self.maus, D("3"))])
        services.wareneingang_buchen(we, bestaetigt=True)
        b1.refresh_from_db()
        self.assertEqual(b1.status, BestellStatus.TEIL)
        self.assertEqual(bestand(self.m, self.maus), D("3"))
        we2 = services.wareneingang_anlegen(self.m, self.lf, b1)
        g = services.laufende_bestellungen(we2)[0]
        self.assertEqual([(bp.artikel, bp.rest) for bp in g["positionen"]], [(self.maus, D("2")), (self.tast, D("2"))])

    def test_hoechstens_offen_und_nicht_doppelt(self):
        b1 = self.bestellt((self.maus, "5"))
        bp = b1.positionen.get()
        we_a = services.wareneingang_anlegen(self.m, self.lf)
        services.wareneingang_uebernehmen(we_a, [(bp, D("4"))])
        we_b = services.wareneingang_anlegen(self.m, self.lf)
        with self.assertRaisesMessage(ValueError, "Höchstens 1 noch offen"):
            services.wareneingang_uebernehmen(we_b, [(bp, D("2"))])  # 4 stehen schon im anderen offenen Wareneingang
        services.wareneingang_uebernehmen(we_b, [(bp, D("1"))])
        with self.assertRaisesMessage(ValueError, "Höchstens 0 noch offen"):
            services.wareneingang_uebernehmen(we_a, [(bp, D("1"))])

    def test_positionen_aus_zwei_bestellungen_buchen_beide(self):
        b1 = self.bestellt((self.maus, "2"))
        b2 = self.bestellt((self.tast, "1"))
        we = services.wareneingang_anlegen(self.m, self.lf)
        services.wareneingang_uebernehmen(we, [(b1.positionen.get(), D("2")), (b2.positionen.get(), D("1"))])
        services.wareneingang_buchen(we, bestaetigt=True)
        b1.refresh_from_db()
        b2.refresh_from_db()
        self.assertEqual((b1.status, b2.status), (BestellStatus.GELIEFERT, BestellStatus.GELIEFERT))

    def test_preislistenartikel_nur_mit_bestaetigung(self):
        pl = Preislistenartikel.objects.create(lieferant=self.lf, bestellnummer="TD-X", bezeichnung="Netzteil", ek=D("30"))
        b = services.bestellung_anlegen(self.m, self.lf)
        services.bestellposition(b, None, D("1"), preisliste_artikel=pl)
        b.status = BestellStatus.BESTELLT
        b.save()
        bp = b.positionen.get()
        we = services.wareneingang_anlegen(self.m, self.lf)
        with self.assertRaisesMessage(ValueError, "bitte bestätigen"):
            services.wareneingang_uebernehmen(we, [(bp, D("1"))])
        r = self.client.post(reverse("einkauf:wareneingang_uebernehmen", args=[we.pk]), {"komplett": b.pk}, follow=True)
        self.assertContains(r, "bitte das Anlegen bestätigen")
        services.wareneingang_uebernehmen(we, [(bp, D("1"))], p_anlegen=True)
        bp.refresh_from_db()
        self.assertIsNotNone(bp.artikel)
        self.assertEqual(we.positionen.get().artikel, bp.artikel)

    def test_fremde_position_wird_abgewiesen(self):
        fremd = self.bestellt((self.kabel, "3"), lieferant=self.lf2)
        we = services.wareneingang_anlegen(self.m, self.lf)
        r = self.client.post(reverse("einkauf:wareneingang_uebernehmen", args=[we.pk]),
                             {"bp": [fremd.positionen.get().pk], f"menge_{fremd.positionen.get().pk}": "1"}, follow=True)
        self.assertContains(r, "nicht (mehr) offen")
        self.assertFalse(we.positionen.exists())
        with self.assertRaisesMessage(ValueError, "nicht zu einer Bestellung dieses Lieferanten"):
            services.wareneingang_uebernehmen(we, [(fremd.positionen.get(), D("1"))])

    def test_gebucht_ohne_bestellungen_tab_mit_ruecksendungen(self):
        b1 = self.bestellt((self.maus, "1"))
        we = services.wareneingang_anlegen(self.m, self.lf, b1)
        services.wareneingang_aus_bestellung(we)
        services.wareneingang_buchen(we, bestaetigt=True)
        r = self.client.get(reverse("einkauf:wareneingang", args=[we.pk]))
        self.assertNotContains(r, 'data-tab="bestellungen"')
        self.assertContains(r, 'data-tab="ruecksendungen"')
        with self.assertRaisesMessage(ValueError, "bereits gebucht"):
            services.wareneingang_uebernehmen(we, [(b1.positionen.get(), D("1"))])

    def test_buchen_ueber_tab_lieferschein_und_kosten(self):
        b1 = self.bestellt((self.maus, "2"))
        we = services.wareneingang_anlegen(self.m, self.lf, b1)
        services.wareneingang_aus_bestellung(we)
        r = self.client.get(reverse("einkauf:wareneingang", args=[we.pk]))
        self.assertContains(r, 'id="buchform"')
        self.assertContains(r, '<button type="submit" form="buchform">Ins Lager buchen</button>')
        self.client.post(reverse("einkauf:wareneingang_buchen", args=[we.pk]),
                         {"lieferschein": "LS-9", "versandkosten": "0", "versicherung": "0", "verpackung": "0", "preis_bestaetigt": "1"})
        we.refresh_from_db()
        self.assertTrue(we.gebucht)
        self.assertEqual(we.lieferschein, "LS-9")


class MehrereBestellungenTest(Basis):
    """Review: Ein Wareneingang mit Positionen aus mehreren Bestellungen schützt auch die weiteren Bestellungen."""

    def zwei_in_einem(self):
        b1 = self.bestellt((self.maus, "2"))
        b2 = self.bestellt((self.tast, "1"))
        we = services.wareneingang_anlegen(self.m, self.lf, b1)
        services.wareneingang_uebernehmen(we, [(b1.positionen.get(), D("2")), (b2.positionen.get(), D("1"))])
        return b1, b2, we

    def test_zweite_bestellung_nicht_loeschbar_und_im_tab(self):
        b1, b2, we = self.zwei_in_einem()
        self.assertEqual(list(b2.alle_wareneingaenge()), [we])
        self.client.post(reverse("einkauf:bestellung_loeschen", args=[b2.pk]))
        self.client.post(reverse("einkauf:bestellung_stornieren", args=[b2.pk]))
        b2.refresh_from_db()
        self.assertEqual(b2.status, BestellStatus.BESTELLT)
        r = self.client.get(reverse("einkauf:bestellung", args=[b2.pk]))
        self.assertContains(r, reverse("einkauf:wareneingang", args=[we.pk]))
        services.wareneingang_buchen(we, bestaetigt=True)
        self.client.post(reverse("einkauf:bestellung_loeschen", args=[b2.pk]))
        self.assertTrue(Bestellung.objects.filter(pk=b2.pk).exists())

    def test_stornierte_zweite_bestellung_bucht_nicht(self):
        b1, b2, we = self.zwei_in_einem()
        Bestellung.objects.filter(pk=b2.pk).update(status=BestellStatus.STORNIERT)
        with self.assertRaisesMessage(ValueError, f"Die Bestellung {b2.nummer} ist storniert"):
            services.wareneingang_buchen(we, bestaetigt=True)
        self.assertEqual(bestand(self.m, self.maus), D("0"))

    def test_rest_schliessen_mit_offenem_fremden_wareneingang(self):
        b1 = self.bestellt((self.maus, "2"))
        b2 = self.bestellt((self.tast, "3"))
        we0 = services.wareneingang_anlegen(self.m, self.lf, b2)
        services.wareneingang_uebernehmen(we0, [(b2.positionen.get(), D("1"))])
        services.wareneingang_buchen(we0, bestaetigt=True)  # b2 jetzt teilweise geliefert
        we = services.wareneingang_anlegen(self.m, self.lf, b1)
        services.wareneingang_uebernehmen(we, [(b2.positionen.get(), D("1"))])
        b2.refresh_from_db()
        with self.assertRaisesMessage(ValueError, "offenen Wareneingang"):
            services.rest_schliessen(b2)

    def test_doppelte_position_im_formular(self):
        b1 = self.bestellt((self.maus, "2"))
        bp = b1.positionen.get()
        we = services.wareneingang_anlegen(self.m, self.lf)
        self.client.post(reverse("einkauf:wareneingang_uebernehmen", args=[we.pk]), {"bp": [bp.pk, bp.pk], f"menge_{bp.pk}": "2"})
        self.assertEqual(sum(p.menge for p in we.positionen.all()), D("2"))
        we2 = services.wareneingang_anlegen(self.m, self.lf)
        b2 = self.bestellt((self.tast, "2"))
        bp2 = b2.positionen.get()
        with self.assertRaisesMessage(ValueError, "Höchstens 2 noch offen"):
            services.wareneingang_uebernehmen(we2, [(bp2, D("2")), (bp2, D("1"))])

    def test_kosten_speichern_bleibt_im_tab(self):
        b1 = self.bestellt((self.maus, "2"))
        we = services.wareneingang_anlegen(self.m, self.lf, b1)
        r = self.client.post(reverse("einkauf:wareneingang_buchen", args=[we.pk]),
                             {"aktion": "kosten", "lieferschein": "LS1", "versandkosten": "5", "versicherung": "0", "verpackung": "0"})
        self.assertRedirects(r, reverse("einkauf:wareneingang", args=[we.pk]) + "#versand", fetch_redirect_response=False)
