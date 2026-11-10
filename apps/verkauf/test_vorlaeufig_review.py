"""Vorläufige Belege, Nachbesserungen aus der Prüfung: Weg A mit berührten Positionen, Seriennummern bei Weg B, Kopfdaten der
Umwandlung, Teillieferungen, P-Angebot, Abo/Shop, Sperrreihenfolge, Übernahme aus vorläufiger Bestellung mit P."""
from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

from django.utils import timezone
from django.urls import reverse

from apps.einkauf.models import Bestellung
from apps.lager.models import Seriennummer
from apps.lager.services import seriennummern_einlagern, seriennummern_zuordnen
from apps.shop.models import Shop, ShopBestellung
from apps.stammdaten.models import Artikel, Lieferant, Steuersatz

from . import vorgang
from .models import Abo, AboPosition, Beleg, BelegVorgang, Belegart, MailVersand, Position, Vorgangsweg, rechnung_aus_lieferscheinen
from .test_vorlaeufig import VorlaeufigBasis, letzte

D = Decimal


class WegATest(VorlaeufigBasis):
    """Weg A (exakt) prüft auch die vom Vorgang berührten Positionen: Eine zweite Übernahme aus derselben Bestellung oder eine
    Änderung an der Position darf nie zu doppelten Mengen führen oder Positionen aus fremden Belegen zurückholen."""

    def zwei_uebernahmen(self, zweite):
        ab = self.beleg()
        p = self.pos(ab, menge="10")
        q = self.pos(ab, self.ram, menge="4", preis="5")
        ab.berechne()
        ls1 = self.teilweise(ab, p, "7")
        ls2 = self.teilweise(ab, p, zweite)
        return ab, p, q, ls1, ls2

    def test_zweite_teiluebernahme_wird_beim_verwerfen_der_ersten_nicht_doppelt(self):
        ab, p, q, ls1, ls2 = self.zwei_uebernahmen("2")
        ergebnis = vorgang.beleg_verwerfen(ls1)
        self.assertTrue(any("zwischendurch geändert" in m for m in ergebnis["meldungen"]))  # Weg B
        p.refresh_from_db()
        self.assertEqual(p.menge, D("8"))  # 1 Rest + 7 zurück; die 2 Stück bleiben im zweiten Lieferschein
        self.assertEqual(ls2.positionen.get().menge, D("2"))
        self.assertEqual(p.menge + ls2.positionen.get().menge, D("10"))
        self.assertFalse(Beleg.objects.filter(pk=ls1.pk).exists())
        self.assertTrue(Beleg.objects.filter(pk=ls2.pk).exists())

    def test_zweite_uebernahme_der_restmenge_holt_die_position_nicht_aus_dem_fremden_beleg(self):
        ab, p, q, ls1, ls2 = self.zwei_uebernahmen("3")  # die Position selbst wandert in den zweiten Lieferschein
        self.assertEqual(Position.objects.get(pk=p.pk).beleg_id, ls2.pk)
        ls2.abschliessen()  # womöglich schon abgeschlossen und gebucht
        vorgang.beleg_verwerfen(ls1)
        p.refresh_from_db()
        self.assertEqual((p.beleg_id, p.menge), (ls2.pk, D("3")))  # bleibt im abgeschlossenen Lieferschein
        neu = ab.positionen.get(artikel=self.maus)
        self.assertEqual(neu.menge, D("7"))  # die 7 Stück sind als neue Position zurück in der Bestellung
        self.assertEqual(ls2.positionen.count(), 1)

    def test_aenderung_an_der_bestellposition_waehrend_des_vorgangs_wird_nicht_ueberschrieben(self):
        ab = self.beleg()
        p = self.pos(ab, menge="10")
        self.pos(ab, self.ram, menge="4", preis="5")
        ab.berechne()
        ls = self.teilweise(ab, p, "7")
        p.refresh_from_db()
        p.menge = D("5")  # jemand ändert die Rest-Position der Bestellung (3 -> 5)
        p.save()
        ergebnis = vorgang.beleg_verwerfen(ls)
        self.assertTrue(any("zwischendurch geändert" in m for m in ergebnis["meldungen"]))
        p.refresh_from_db()
        self.assertEqual(p.menge, D("12"))  # 5 geänderte + 7 zurück, die Änderung bleibt erhalten

    def test_ohne_zwischenaenderung_bleibt_weg_a_exakt(self):
        ab = self.beleg()
        p = self.pos(ab, menge="10")
        ab.berechne()
        vorher = self.stand(ab)
        ls = self.teilweise(ab, p, "7")
        self.assertEqual(vorgang.beleg_verwerfen(ls)["meldungen"], [])
        self.assertEqual(self.stand(ab), vorher)


class SeriennummernTest(VorlaeufigBasis):
    def test_weg_b_haengt_seriennummern_einer_umgehaengten_position_an_die_bestellung(self):
        ab = self.beleg()
        p = self.pos(ab, menge="4")
        q = self.pos(ab, self.ram, menge="4", preis="5")
        ab.berechne()
        seriennummern_einlagern(self.m, self.maus, "A1\nA2\nA3\nA4")
        seriennummern_zuordnen(p, "A1\nA2\nA3\nA4")
        ls = self.teilweise(ab, p, "4")  # volle Menge: dieselbe Position hängt im Lieferschein
        self.assertEqual(Position.objects.get(pk=p.pk).beleg_id, ls.pk)
        q.refresh_from_db()
        q.menge = D("6")  # die Bestellung wird zwischendurch geändert: Weg B
        q.save()
        vorgang.beleg_verwerfen(ls)
        sns = list(Seriennummer.objects.order_by("nummer"))
        self.assertEqual(len(sns), 4)
        for sn in sns:
            self.assertIsNotNone(sn.position_id, sn.nummer)  # nie verwaist
            self.assertEqual(sn.position.beleg_id, ab.pk)
            self.assertEqual(sn.status, Seriennummer.VERKAUFT)

    def test_teillieferung_laesst_seriennummern_bei_der_gelieferten_position(self):
        knapp = Artikel.objects.create(nummer="K9", bezeichnung="Knapp", steuersatz=Steuersatz.objects.get(nummer=1), vk1=D("8"))
        ab = self.beleg()
        p = self.pos(ab, menge="2")
        self.pos(ab, knapp, menge="5", preis="8")
        ab.berechne()
        seriennummern_einlagern(self.m, self.maus, "B1\nB2")
        seriennummern_zuordnen(p, "B1\nB2")
        ls = ab.folgebeleg_erzeugen(art=Belegart.LIEFERSCHEIN, vorlaeufig=True)
        neu = ls.teillieferung_abschliessen()  # die Maus ist lieferbar, der Rest geht zurück
        self.assertEqual(neu.positionen.get().artikel, self.maus)
        for sn in Seriennummer.objects.all():
            self.assertEqual(sn.position.beleg_id, neu.pk)


class UmwandlungTest(VorlaeufigBasis):
    def test_kopfdaten_werden_beim_verwerfen_der_umwandlung_zurueckgesetzt(self):
        an = self.beleg(Belegart.ANGEBOT, versandkosten=D("3"), verpackung=D("1"), rabatt_prozent=D("2"), referenz="R-1",
                        vortext="Vor", schlusstext="Schluss", bemerkung="intern", versandart="DHL",
                        rechnungsanschrift="Alt Str. 1", versandanschrift="Alt Str. 2", bezugnahme="Ihr Anruf")
        self.pos(an, menge="2")
        an.berechne()
        felder = ("versandkosten", "verpackung", "versicherung", "rabatt_prozent", "referenz", "vortext", "schlusstext", "bemerkung",
                  "versandart", "rechnungsanschrift", "versandanschrift", "bezugnahme", "ansprechpartner_id", "zahlungsbedingung_id",
                  "summe_netto", "summe_brutto")
        vorher = {f: getattr(an, f) for f in felder}
        ab = an.folgebeleg_erzeugen(vorlaeufig=True)
        for f, w in (("versandkosten", D("9")), ("verpackung", D("4")), ("versicherung", D("2")), ("rabatt_prozent", D("10")),
                     ("referenz", "Neu"), ("vortext", "V2"), ("schlusstext", "S2"), ("bemerkung", "b2"), ("versandart", "Abholung"),
                     ("rechnungsanschrift", "Neu 1"), ("versandanschrift", "Neu 2"), ("bezugnahme", "x")):
            setattr(ab, f, w)
        ab.save()
        ab.berechne()
        self.assertNotEqual(ab.summe_brutto, vorher["summe_brutto"])
        vorgang.beleg_verwerfen(ab)
        an.refresh_from_db()
        self.assertEqual({f: getattr(an, f) for f in felder}, vorher)

    def test_gemailtes_angebot_laesst_sich_nach_der_umwandlung_verwerfen(self):
        an = self.beleg(Belegart.ANGEBOT)
        self.pos(an)
        an.abschliessen()
        MailVersand.objects.create(beleg=an, mandant=self.m, an="k@example.org", betreff="Angebot", text="x", erfolgreich=True,
                                   zeitpunkt=timezone.now() - timedelta(days=1))
        ab = an.folgebeleg_erzeugen(vorlaeufig=True)
        self.assertEqual((ab.art, ab.nummer), (Belegart.AUFTRAG, None))
        self.assertIsNone(vorgang.verwerfhindernis(ab, BelegVorgang.objects.get(beleg=ab)))
        vorgang.beleg_verwerfen(ab)
        an.refresh_from_db()
        self.assertEqual((an.art, an.vorlaeufig), (Belegart.ANGEBOT, False))
        self.assertTrue(an.nummer)
        self.assertEqual(an.mails.count(), 1)  # der Versandnachweis bleibt

    def test_mail_nach_der_umwandlung_bleibt_ein_hindernis(self):
        an = self.beleg(Belegart.ANGEBOT)
        self.pos(an)
        ab = an.folgebeleg_erzeugen(vorlaeufig=True)
        MailVersand.objects.create(beleg=ab, mandant=self.m, an="k@example.org", betreff="x", text="x", erfolgreich=True)
        with self.assertRaises(ValueError):
            vorgang.beleg_verwerfen(ab)
        self.assertTrue(Beleg.objects.filter(pk=ab.pk, vorlaeufig=True).exists())

    def test_bestellung_mit_lieferantenbestellung_shop_und_abo_zu_rechnung_verwerfen(self):
        ab, p = self.ab("2")
        ab.abschliessen()
        ab.refresh_from_db()
        nummer = ab.nummer
        shop = Shop.objects.create(mandant=self.m, name="S", url="https://s.example", client_id="x")
        ShopBestellung.objects.create(shop=shop, shop_id_extern="1", bestellnummer="1", beleg=ab)
        Bestellung.objects.create(mandant=self.m, lieferant=Lieferant.objects.get(nummer="70272"), nummer="B-1", aus_beleg=ab)
        abo = Abo.objects.create(mandant=self.m, kunde=self.kunde, bezeichnung="W", naechster_termin=date(2026, 1, 31))
        Beleg.objects.filter(pk=ab.pk).update(abo=abo)
        re = ab.folgebeleg_erzeugen(art=Belegart.RECHNUNG, vorlaeufig=True)
        self.assertEqual((re.pk, re.art), (ab.pk, Belegart.RECHNUNG))
        self.assertIsNotNone(vorgang.beleg_verwerfen(re))
        ab.refresh_from_db()
        self.assertEqual((ab.art, ab.nummer, ab.vorlaeufig), (Belegart.AUFTRAG, nummer, False))
        self.assertTrue(Bestellung.objects.filter(aus_beleg=ab).exists())


class TeillieferungTest(VorlaeufigBasis):
    def setUp(self):
        super().setUp()
        self.knapp = Artikel.objects.create(nummer="K1", bezeichnung="Knapp", steuersatz=Steuersatz.objects.get(nummer=1), vk1=D("8"))

    def test_teillieferung_aus_umwandlung_mit_hinzugefuegter_position_wird_abgelehnt(self):
        ab = self.beleg()
        self.pos(ab, menge="2")
        self.pos(ab, self.knapp, menge="5", preis="8")
        ab.berechne()
        ab.abschliessen()
        ab.refresh_from_db()
        re = ab.folgebeleg_erzeugen(art=Belegart.RECHNUNG, vorlaeufig=True)
        extra = self.pos(re, self.ram, menge="1", preis="5")  # in der vorläufigen Rechnung ergänzt
        re.berechne()
        with self.assertRaises(ValueError) as fehler:
            re.teillieferung_abschliessen()
        self.assertIn("geändert", str(fehler.exception))
        re.refresh_from_db()  # nichts verloren: weiter vorläufige Rechnung mit allen drei Positionen
        self.assertEqual((re.art, re.vorlaeufig, re.positionen.count()), (Belegart.RECHNUNG, True, 3))
        self.assertTrue(Position.objects.filter(pk=extra.pk).exists())

    def test_teillieferung_aus_umwandlung_mit_geaenderter_menge_wird_abgelehnt(self):
        ab = self.beleg()
        maus = self.pos(ab, menge="2")
        self.pos(ab, self.knapp, menge="5", preis="8")
        ab.berechne()
        ab.abschliessen()
        re = ab.folgebeleg_erzeugen(art=Belegart.RECHNUNG, vorlaeufig=True)
        Position.objects.filter(pk=maus.pk).update(menge=D("1"))
        with self.assertRaises(ValueError):
            re.teillieferung_abschliessen()

    def test_kopie_aus_angebot_mit_urbeleg_abschluss_schliesst_das_angebot_auch_nach_teillieferung(self):
        an = self.beleg(Belegart.ANGEBOT)
        self.pos(an, menge="2")
        self.pos(an, self.knapp, menge="5", preis="8")
        an.berechne()
        re = an.kopieren_als(Belegart.RECHNUNG, urbeleg_abschliessen=True, vorlaeufig=True)
        an.refresh_from_db()
        self.assertFalse(an.abgeschlossen)  # vorgemerkt
        neu = re.teillieferung_abschliessen()
        self.assertTrue(neu.abgeschlossen)
        an.refresh_from_db()
        self.assertTrue(an.abgeschlossen)  # der gewählte Abschluss geht nicht verloren
        re.refresh_from_db()
        self.assertTrue(re.vorlaeufig)  # der Rest bleibt vorläufig
        self.assertFalse(BelegVorgang.objects.get(beleg=re).quelle_abgeschlossen_durch_vorgang)

    def test_kopie_aus_angebot_ohne_urbeleg_abschluss_laesst_das_angebot_offen(self):
        an = self.beleg(Belegart.ANGEBOT)
        self.pos(an, menge="2")
        self.pos(an, self.knapp, menge="5", preis="8")
        an.berechne()
        re = an.kopieren_als(Belegart.RECHNUNG, vorlaeufig=True)
        re.teillieferung_abschliessen()
        an.refresh_from_db()
        self.assertFalse(an.abgeschlossen)

    def test_teillieferung_aus_bestellung_gibt_den_rest_weiter_zurueck(self):
        ab = self.beleg()
        self.pos(ab, menge="2")
        self.pos(ab, self.knapp, menge="5", preis="8")
        ab.berechne()
        ls = ab.folgebeleg_erzeugen(art=Belegart.LIEFERSCHEIN, vorlaeufig=True)
        neu = ls.teillieferung_abschliessen()
        self.assertEqual(neu.rest_beleg.pk, ab.pk)
        ab.refresh_from_db()
        self.assertFalse(ab.abgeschlossen)


class PAngebotTest(VorlaeufigBasis):
    def ls_mit_p(self):
        ab = self.beleg()
        maus = self.pos(ab, menge="2")
        p = Position.objects.create(beleg=ab, typ="P", preisliste_artikel=self.pl, bezeichnung="Netzteil", menge=D("1"), einzelpreis=D("50"))
        ab.berechne()
        ls = ab.folgebeleg_erzeugen(art=Belegart.LIEFERSCHEIN, vorlaeufig=True)
        return ab, maus, p, ls, BelegVorgang.objects.get(beleg=ls).angebot

    def test_vorlaeufiges_p_angebot_laesst_sich_nicht_einzeln_abschliessen(self):
        ab, maus, p, ls, angebot = self.ls_mit_p()
        with self.assertRaises(ValueError) as fehler:
            angebot.abschliessen()
        self.assertIn("gehört zum nicht gespeicherten Beleg", str(fehler.exception))
        r = self.client.post(reverse("verkauf:beleg_abschliessen", args=[angebot.pk]), follow=True)
        self.assertTrue(any("gehört zum nicht gespeicherten Beleg" in m for m in self.meldungen(r)))
        angebot.refresh_from_db()
        self.assertEqual((angebot.nummer, angebot.vorlaeufig, angebot.abgeschlossen), (None, True, False))
        ls.abschliessen()  # mit dem Lieferschein wird das Angebot nummeriert
        angebot.refresh_from_db()
        self.assertTrue(angebot.nummer and not angebot.vorlaeufig)

    def test_vorlaeufiges_p_angebot_laesst_sich_nicht_loeschen(self):
        ab, maus, p, ls, angebot = self.ls_mit_p()
        r = self.client.post(reverse("verkauf:beleg_loeschen", args=[angebot.pk]), follow=True)
        self.assertTrue(any("gehört zum nicht gespeicherten Beleg" in m for m in self.meldungen(r)))
        self.assertTrue(Beleg.objects.filter(pk=angebot.pk).exists())
        self.assertEqual(angebot.positionen.count(), 1)

    def test_maske_des_p_angebots_hat_weder_verlassen_dialog_noch_verwerfen_noch_speichern(self):
        ab, maus, p, ls, angebot = self.ls_mit_p()
        html = self.client.get(reverse("verkauf:beleg", args=[angebot.pk])).content.decode()
        self.assertIn("vorlaeufigbalken", html)
        self.assertIn("Dieses Angebot gehört zu", html)
        for text in ("vorlaeufigskript", "verwerfenform", 'id="abschlussform"'):
            self.assertNotIn(text, html)
        self.assertIn("vorlaeufigskript", self.client.get(reverse("verkauf:beleg", args=[ls.pk])).content.decode())

    def test_verwerfen_laesst_ein_inzwischen_gespeichertes_angebot_bestehen(self):
        ab, maus, p, ls, angebot = self.ls_mit_p()
        an_kreis = letzte(self.m, "AN")
        Beleg.objects.filter(pk=angebot.pk).update(nummer="AN2026-90001", abgeschlossen=True, vorlaeufig=False)  # Altfall
        ergebnis = vorgang.beleg_verwerfen(ls)
        self.assertTrue(any("bleibt" in m for m in ergebnis["meldungen"]))
        self.assertTrue(Beleg.objects.filter(pk=angebot.pk, nummer="AN2026-90001").exists())
        self.assertEqual(Position.objects.get(pk=p.pk).beleg_id, angebot.pk)  # P bleibt im gespeicherten Angebot
        self.assertEqual(letzte(self.m, "AN"), an_kreis)
        self.assertEqual([(x.artikel, x.menge) for x in ab.positionen.all()], [(self.maus, D("2"))])  # die Maus ist zurück

    def test_eigene_positionen_im_p_angebot_gehen_nicht_in_die_bestellung(self):
        ab, maus, p, ls, angebot = self.ls_mit_p()
        eigene = self.pos(angebot, self.ram, menge="3", preis="5")
        vorgang.beleg_verwerfen(ls)
        angebot.refresh_from_db()
        self.assertEqual(Position.objects.get(pk=p.pk).beleg_id, ab.pk)  # die P-Position ist zurück
        self.assertEqual(Position.objects.get(pk=eigene.pk).beleg_id, angebot.pk)  # die ergänzte bleibt im Angebot
        self.assertTrue(angebot.nummer and not angebot.vorlaeufig)  # und das Angebot ist gespeichert


class AboVerwerfenTest(VorlaeufigBasis):
    def abo_ab(self):
        abo = Abo.objects.create(mandant=self.m, kunde=self.kunde, bezeichnung="Wartung", naechster_termin=date(2026, 1, 31), starttag=31)
        AboPosition.objects.create(abo=abo, bezeichnung="Wartung", einzelpreis=D("100"))
        return abo, abo.rechnung_erzeugen(date(2026, 1, 31))

    def test_vorlaeufiger_lieferschein_aus_abo_bestellung_laesst_sich_verwerfen(self):
        abo, ab = self.abo_ab()
        position = ab.positionen.get()
        ls = ab.folgebeleg_erzeugen(art=Belegart.LIEFERSCHEIN, vorlaeufig=True)
        self.assertEqual(ls.abo_id, abo.pk)
        self.assertEqual(ab.positionen.count(), 0)
        r = self.client.post(reverse("verkauf:beleg_verwerfen", args=[ls.pk]))
        self.assertEqual(r.status_code, 302)
        self.assertFalse(Beleg.objects.filter(pk=ls.pk).exists())
        self.assertEqual(Position.objects.get(pk=position.pk).beleg_id, ab.pk)  # volle Menge zurück in der Abo-Bestellung

    def test_vorlaeufige_umwandlung_der_abo_bestellung_laesst_sich_verwerfen(self):
        abo, ab = self.abo_ab()
        nummer = ab.nummer
        re = ab.folgebeleg_erzeugen(art=Belegart.RECHNUNG, vorlaeufig=True)
        self.assertEqual((re.pk, re.abo_id), (ab.pk, abo.pk))
        r = self.client.post(reverse("verkauf:beleg_verwerfen", args=[re.pk]), headers={"X-Wwok-Stand": "1"})
        self.assertTrue(r.json()["ok"])
        ab.refresh_from_db()
        self.assertEqual((ab.art, ab.nummer, ab.vorlaeufig, ab.abo_id), (Belegart.AUFTRAG, nummer, False, abo.pk))

    def test_bereinigung_verwirft_vorlaeufige_uebernahmen_aus_der_abo_bestellung(self):
        abo, ab = self.abo_ab()
        ls = ab.folgebeleg_erzeugen(art=Belegart.LIEFERSCHEIN, vorlaeufig=True)
        Beleg.objects.filter(pk=ls.pk).update(zuletzt_aktiv=timezone.now() - timedelta(hours=3))
        Beleg.vorlaeufige_verwerfen()
        self.assertFalse(Beleg.objects.filter(pk=ls.pk).exists())
        self.assertTrue(Beleg.objects.filter(pk=ab.pk).exists())

    def test_beleg_der_selbst_zum_abo_gehoert_wird_weiter_nie_verworfen(self):
        abo, ab = self.abo_ab()
        Beleg.objects.filter(pk=ab.pk).update(vorlaeufig=True, zuletzt_aktiv=timezone.now() - timedelta(days=3), nummer=None)
        vorgang.vorgang_beginnen(Beleg.objects.get(pk=ab.pk), Vorgangsweg.NEU)
        with self.assertRaises(ValueError):
            vorgang.beleg_verwerfen(Beleg.objects.get(pk=ab.pk))
        Beleg.vorlaeufige_verwerfen()
        self.assertTrue(Beleg.objects.filter(pk=ab.pk).exists())

    def test_jetzt_erkennt_vollstaendig_gelieferten_aber_nicht_abgerechneten_zeitraum(self):
        abo, ab = self.abo_ab()
        ls = ab.folgebeleg_erzeugen(art=Belegart.LIEFERSCHEIN)
        ls.abschliessen()
        self.assertEqual(Abo.objects.get(pk=abo.pk).offener_beleg().pk, ls.pk)  # geliefert, noch nicht abgerechnet
        re = rechnung_aus_lieferscheinen([ls])
        self.assertEqual(re.abo_id, abo.pk)
        self.assertEqual(Abo.objects.get(pk=abo.pk).offener_beleg().pk, re.pk)  # Sammelrechnung noch offen
        re.abschliessen()
        self.assertIsNone(Abo.objects.get(pk=abo.pk).offener_beleg())

    def test_sammelrechnung_aus_verschiedenen_abos_bekommt_kein_abo(self):
        abo, ab = self.abo_ab()
        ls = ab.folgebeleg_erzeugen(art=Belegart.LIEFERSCHEIN)
        ls.abschliessen()
        ls2 = self.beleg(Belegart.LIEFERSCHEIN)
        self.pos(ls2)
        ls2.abschliessen()
        self.assertIsNone(rechnung_aus_lieferscheinen([ls, ls2]).abo_id)

    def test_sammelrechnung_mit_abo_laesst_sich_verwerfen(self):
        abo, ab = self.abo_ab()
        ls = ab.folgebeleg_erzeugen(art=Belegart.LIEFERSCHEIN)
        ls.abschliessen()
        re = rechnung_aus_lieferscheinen([ls], vorlaeufig=True)
        self.assertEqual(re.abo_id, abo.pk)
        self.assertIsNotNone(vorgang.beleg_verwerfen(re))
        self.assertTrue(Beleg.objects.get(pk=ls.pk).abrechenbar)


class SperrenTest(VorlaeufigBasis):
    """Sperrreihenfolge: überall erst die Quelle, dann das Ziel (sonst Deadlock gegen das Verwerfen von Hand)."""

    def aufzeichnen(self):
        import re as regex
        from django.db.models.query import QuerySet
        original, reihenfolge = QuerySet._fetch_all, []

        def spion(qs):
            if qs.query.select_for_update and qs._result_cache is None:
                treffer = regex.search(r'"verkauf_beleg"\."id" = (\d+)', str(qs.query))
                if treffer:
                    reihenfolge.append(int(treffer.group(1)))
            return original(qs)
        return mock.patch.object(QuerySet, "_fetch_all", spion), reihenfolge

    def test_bereinigung_sperrt_erst_die_quelle_dann_das_ziel(self):
        ab, p = self.ab("10")
        ls = self.teilweise(ab, p, "7")
        Beleg.objects.filter(pk=ls.pk).update(zuletzt_aktiv=timezone.now() - timedelta(hours=3))
        patch, reihenfolge = self.aufzeichnen()
        with patch:
            Beleg.vorlaeufige_verwerfen()
        self.assertIn(ls.pk, reihenfolge)
        self.assertLess(reihenfolge.index(ab.pk), reihenfolge.index(ls.pk))

    def test_abschluss_sperrt_erst_die_quelle_dann_das_ziel(self):
        ab, p = self.ab("10")
        ls = ab.folgebeleg_erzeugen(art=Belegart.LIEFERSCHEIN, vorlaeufig=True)  # alles: Abschluss der Bestellung vorgemerkt
        self.assertTrue(BelegVorgang.objects.get(beleg=ls).quelle_abgeschlossen_durch_vorgang)
        patch, reihenfolge = self.aufzeichnen()
        with patch:
            ls.abschliessen()
        self.assertLess(reihenfolge.index(ab.pk), reihenfolge.index(ls.pk))
        ab.refresh_from_db()
        self.assertTrue(ab.abgeschlossen)

    def test_deadlock_wird_als_meldung_statt_fehlerseite_gezeigt(self):
        from django.db import OperationalError
        b = self.vorlaeufig(Belegart.RECHNUNG)
        with mock.patch("apps.verkauf.vorgang.beleg_verwerfen", side_effect=OperationalError("Deadlock found")):
            r = self.client.post(reverse("verkauf:beleg_verwerfen", args=[b.pk]), headers={"X-Wwok-Stand": "1"})
            self.assertEqual(r.status_code, 409)
            self.assertIn("erneut versuchen", r.json()["fehler"])
            r = self.client.post(reverse("verkauf:beleg_verwerfen", args=[b.pk]), follow=True)
            self.assertTrue(any("erneut versuchen" in m for m in self.meldungen(r)))
        with mock.patch.object(Beleg, "abschliessen", side_effect=OperationalError("Deadlock found")):
            r = self.client.post(reverse("verkauf:beleg_abschliessen", args=[b.pk]), follow=True)
            self.assertEqual(r.status_code, 200)
            self.assertTrue(any("erneut versuchen" in m for m in self.meldungen(r)))


class UebernahmeAusVorlaeufigemBelegTest(VorlaeufigBasis):
    def test_vorlaeufige_bestellung_mit_p_laesst_sich_erst_abschliessen_und_uebernehmen(self):
        ab = self.vorlaeufig(Belegart.AUFTRAG)  # eine Maus (2)
        p = Position.objects.create(beleg=ab, typ="P", preisliste_artikel=self.pl, bezeichnung="Netzteil", menge=D("1"), einzelpreis=D("50"))
        ab.berechne()
        r = self.client.post(reverse("verkauf:beleg_folgebeleg", args=[ab.pk]), {"von_art": "AB", "erst_abschliessen": "1"}, follow=True)
        self.assertEqual(r.status_code, 200)
        ab.refresh_from_db()
        self.assertTrue(ab.nummer and not ab.vorlaeufig)  # erst abgeschlossen ...
        ls = Beleg.objects.get(art=Belegart.LIEFERSCHEIN)  # ... dann übernommen
        self.assertEqual([x.artikel for x in ls.positionen.all()], [self.maus])
        angebot = BelegVorgang.objects.get(beleg=ls).angebot
        self.assertEqual(Position.objects.get(pk=p.pk).beleg_id, angebot.pk)  # P im neuen Angebot
        self.assertFalse(any("nur als Angebot möglich" in m for m in self.meldungen(r)))

    def test_vorlaeufiger_lieferschein_mit_p_bleibt_beim_abschluss_gesperrt(self):
        ls = self.vorlaeufig(Belegart.LIEFERSCHEIN)
        Position.objects.create(beleg=ls, typ="P", preisliste_artikel=self.pl, bezeichnung="Netzteil", menge=D("1"), einzelpreis=D("50"))
        with self.assertRaises(ValueError):
            ls.abschliessen()

    def test_normales_speichern_einer_bestellung_mit_p_bleibt_gesperrt(self):
        ab = self.vorlaeufig(Belegart.AUFTRAG)
        Position.objects.create(beleg=ab, typ="P", preisliste_artikel=self.pl, bezeichnung="Netzteil", menge=D("1"), einzelpreis=D("50"))
        with self.assertRaises(ValueError):
            ab.abschliessen()

    def test_kopieren_aus_vorlaeufigem_angebot_fragt_nicht_nach_dem_urbeleg(self):
        an = self.vorlaeufig(Belegart.ANGEBOT)
        html = self.client.get(reverse("verkauf:beleg", args=[an.pk])).content.decode()
        self.assertIn("'vorlaeufig' in f.dataset", html)
        self.assertNotIn("data-bleibt data-erst", html)
        r = self.client.post(reverse("verkauf:beleg_kopieren", args=[an.pk]), {"art": "AB", "urbeleg_abschliessen": "0"}, follow=True)
        self.assertEqual(r.status_code, 200)
        an.refresh_from_db()
        # neue Regel: keine Rückfrage, das Angebot wird erst mit der Kopie abgeschlossen und nummeriert (test_vorlaeufig_angebot)
        self.assertTrue(an.vorlaeufig and an.nummer is None)
