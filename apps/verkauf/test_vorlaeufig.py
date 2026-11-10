"""Vorläufige Belege: Was in der Belegmaske neu entsteht oder umgewandelt wird, ist erst mit „Speichern: abschließen & Nummer
vergeben“ endgültig. Ohne Abschluss wird alles zurückgenommen (Verwerfen: Rückfrage, „Löschen“, Bereinigung verlassener Belege).
Ein vorläufiger Beleg hat keine Nummer, ein Verwerfen erzeugt also keine Lücke im Nummernkreis."""
from datetime import date, timedelta
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.einkauf.models import Bestellung
from apps.esd.models import ESDVergabe
from apps.finanzen.models import Zahlung
from apps.finanzen.services import zahlung_buchen
from apps.kern import dashboard
from apps.kern.models import AenderungsProtokoll, Mandant
from apps.lager.models import Bewegungsart, Seriennummer, reserviert
from apps.lager.services import buche_manuell, seriennummern_einlagern, seriennummern_zuordnen
from apps.shop.models import Shop, ShopBestellung
from apps.stammdaten.models import Artikel, Kunde, Lieferant, Preislistenartikel, Steuersatz
from apps.werkstatt.models import Reparatur

from . import uebernahme, vorgang
from .models import (Abo, AboPosition, Beleg, BelegUebernahme, BelegUmwandlung, BelegVorgang, Belegart, MailVersand, Nummernkreis,
                     Position, Versand, Vorgangsweg, rechnung_aus_lieferscheinen)

D = Decimal


def letzte(mandant, art) -> int:
    kreis = Nummernkreis.objects.filter(mandant=mandant, art=art).first()
    return kreis.letzte_nummer if kreis else 0


@override_settings(SECURE_SSL_REDIRECT=False)
class VorlaeufigBasis(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.kunde = Kunde.objects.create(nummer="1", name1="Kunde GmbH")
        st = Steuersatz.objects.get(nummer=1)
        self.maus = Artikel.objects.create(nummer="M1", bezeichnung="Maus", steuersatz=st, vk1=D("10"))
        self.ram = Artikel.objects.create(nummer="R1", bezeichnung="RAM", steuersatz=st, vk1=D("5"))
        buche_manuell(self.m, self.maus, Bewegungsart.WARENEINGANG, D("50"))
        api = Lieferant.objects.create(nummer="70272", name1="API GmbH", kurzname="API")
        self.pl = Preislistenartikel.objects.create(
            lieferant=api, bestellnummer="500", herstellernummer="PSAZ-650", bezeichnung="Netzteil", ek=D("40.0000"), bestand=5)
        self.user = get_user_model().objects.create_user("t", password="x" * 14)
        self.client.force_login(self.user)
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    # --- Hilfen -----------------------------------------------------------------------------------------------------------
    def beleg(self, art=Belegart.AUFTRAG, **felder) -> Beleg:
        return Beleg.objects.create(mandant=self.m, art=art, kunde=self.kunde, **felder)

    def pos(self, beleg, artikel=None, menge="10", preis="10", **felder) -> Position:
        artikel = self.maus if artikel is None else artikel
        return Position.objects.create(beleg=beleg, artikel=artikel, bezeichnung=artikel.bezeichnung, menge=D(menge),
                                       einzelpreis=D(preis), **felder)

    def ab(self, menge="10") -> tuple:
        ab = self.beleg()
        p = self.pos(ab, menge=menge)
        ab.berechne()
        return ab, p

    def vorlaeufig(self, art=Belegart.RECHNUNG, mit_position=True) -> Beleg:
        """Wie die Belegmaske: neuer Beleg ohne Nummer mit Vorgang NEU."""
        b = Beleg.objects.create(mandant=self.m, art=art, kunde=self.kunde, vorlaeufig=True)
        vorgang.vorgang_beginnen(b, Vorgangsweg.NEU)
        if mit_position:
            self.pos(b, menge="2")
            b.berechne()
        return b

    def teilweise(self, ab, p, menge="7", art=Belegart.LIEFERSCHEIN, **kw):
        """Folgebeleg (vorläufig) mit einer Teilmenge aus der Bestellung."""
        with mock.patch.object(Beleg, "_uebernahme", return_value={p.pk: D(menge)}):
            return ab.folgebeleg_erzeugen(art=art, vorlaeufig=True, **kw)

    def meldungen(self, antwort) -> list:
        return [str(m) for m in antwort.context["messages"]]

    def stand(self, ab) -> list:
        return [(p.pk, p.pos, p.menge, p.gruppe_id, p.verschoben) for p in Position.objects.filter(beleg=ab).order_by("pos", "gruppe", "id")]


class GrundverhaltenTest(VorlaeufigBasis):
    def test_beleg_neu_ist_vorlaeufig_ohne_nummer_und_abschluss_macht_ihn_endgueltig(self):
        for art in (Belegart.ANGEBOT, Belegart.AUFTRAG, Belegart.RECHNUNG):
            vorher = letzte(self.m, art)
            self.client.post(reverse("verkauf:beleg_neu", args=[self.kunde.pk]), {"art": art})
            b = Beleg.objects.filter(art=art).get()
            self.assertTrue(b.vorlaeufig, art)
            self.assertIsNone(b.nummer, art)
            self.assertIsNotNone(b.zuletzt_aktiv)
            self.assertEqual(b.vorgang.weg, Vorgangsweg.NEU)
            self.assertEqual(letzte(self.m, art), vorher)  # keine Nummer verbraucht
            self.assertIn("(nicht gespeichert)", str(b))
            self.pos(b, menge="1")
            b.abschliessen()
            b.refresh_from_db()
            self.assertTrue(b.nummer and b.nummer.startswith(art), art)
            self.assertFalse(b.vorlaeufig)
            self.assertIsNone(b.zuletzt_aktiv)
            self.assertTrue(b.abgeschlossen)
            self.assertFalse(BelegVorgang.objects.filter(beleg=b).exists())  # ab jetzt gilt alles endgültig
            self.assertEqual(letzte(self.m, art), vorher + 1)

    def test_verwerfen_loescht_den_beleg_und_laesst_den_nummernkreis_unveraendert(self):
        for art in (Belegart.ANGEBOT, Belegart.AUFTRAG, Belegart.RECHNUNG):
            vorher = letzte(self.m, art)
            b = self.vorlaeufig(art)
            ergebnis = vorgang.beleg_verwerfen(b, self.user, "test")
            self.assertEqual(ergebnis["weiter"], reverse("verkauf:kunde", args=[self.kunde.pk]))
            self.assertFalse(Beleg.objects.filter(pk=b.pk).exists())
            self.assertFalse(Position.objects.filter(beleg_id=b.pk).exists())
            self.assertEqual(letzte(self.m, art), vorher, art)

    def test_verwerfen_ohne_vorlaeufig_oder_nach_abschluss_tut_nichts(self):
        alt = self.beleg(Belegart.ANGEBOT)  # Altentwurf: nicht vorläufig
        self.pos(alt)
        self.assertIsNone(vorgang.beleg_verwerfen(alt))
        self.assertTrue(Beleg.objects.filter(pk=alt.pk).exists())
        b = self.vorlaeufig(Belegart.AUFTRAG)
        b.abschliessen()
        self.assertIsNone(vorgang.beleg_verwerfen(b))  # schon gespeichert
        self.assertTrue(Beleg.objects.filter(pk=b.pk).exists())

    def test_loeschen_am_vorlaeufigen_beleg_verwirft(self):
        b = self.vorlaeufig(Belegart.AUFTRAG)
        r = self.client.post(reverse("verkauf:beleg_loeschen", args=[b.pk]), follow=True)
        self.assertFalse(Beleg.objects.filter(pk=b.pk).exists())
        self.assertTrue(any("verworfen" in m for m in self.meldungen(r)))

    def test_verwerfen_view_json_und_redirect(self):
        b = self.vorlaeufig(Belegart.RECHNUNG)
        r = self.client.post(reverse("verkauf:beleg_verwerfen", args=[b.pk]), HTTP_X_WWOK_STAND="1")
        self.assertEqual(r.json(), {"ok": True, "weiter": reverse("verkauf:kunde", args=[self.kunde.pk])})
        self.assertFalse(Beleg.objects.filter(pk=b.pk).exists())
        # zweiter Aufruf (Doppelklick, schon verworfen): kein Fehler
        r = self.client.post(reverse("verkauf:beleg_verwerfen", args=[b.pk]), HTTP_X_WWOK_STAND="1")
        self.assertTrue(r.json()["ok"])
        b2 = self.vorlaeufig(Belegart.AUFTRAG)
        r = self.client.post(reverse("verkauf:beleg_verwerfen", args=[b2.pk]))
        self.assertRedirects(r, reverse("verkauf:kunde", args=[self.kunde.pk]), fetch_redirect_response=False)
        # gespeicherter Beleg: nichts zu verwerfen, Beleg bleibt
        b3 = self.vorlaeufig(Belegart.AUFTRAG)
        b3.abschliessen()
        r = self.client.post(reverse("verkauf:beleg_verwerfen", args=[b3.pk]), HTTP_X_WWOK_STAND="1")
        self.assertFalse(r.json()["ok"])
        self.assertTrue(Beleg.objects.filter(pk=b3.pk).exists())

    def test_rechnung_mit_altem_datum_bekommt_beim_abschluss_das_abschlussdatum(self):
        re = self.vorlaeufig(Belegart.RECHNUNG)
        Beleg.objects.filter(pk=re.pk).update(belegdatum=timezone.localdate() - timedelta(days=3))
        re.refresh_from_db()
        re.abschliessen()
        re.refresh_from_db()
        self.assertEqual(re.belegdatum, timezone.localdate())
        ab = self.vorlaeufig(Belegart.AUFTRAG)  # andere Belegarten behalten das Anlagedatum
        Beleg.objects.filter(pk=ab.pk).update(belegdatum=timezone.localdate() - timedelta(days=3))
        ab.refresh_from_db()
        ab.abschliessen()
        ab.refresh_from_db()
        self.assertEqual(ab.belegdatum, timezone.localdate() - timedelta(days=3))

    def test_aenderungsprotokoll_haelt_das_verwerfen_fest(self):
        b = self.vorlaeufig(Belegart.RECHNUNG)
        pk = str(b.pk)
        vorgang.beleg_verwerfen(b)
        self.assertTrue(AenderungsProtokoll.objects.filter(tabelle="verkauf.Beleg", datensatz_id=pk, aktion="loeschen").exists())
        an = self.beleg(Belegart.ANGEBOT)
        self.pos(an)
        ab = an.folgebeleg_erzeugen(vorlaeufig=True)
        vorgang.beleg_verwerfen(ab)
        aktionen = AenderungsProtokoll.objects.filter(tabelle="verkauf.Beleg", datensatz_id=str(an.pk)).values_list("aktion", flat=True)
        self.assertGreaterEqual(list(aktionen).count("aendern"), 2)  # Umwandlung und Rückumwandlung


class UmwandlungTest(VorlaeufigBasis):
    def test_angebot_zu_bestellung_verwerfen_stellt_alles_wieder_her(self):
        an = self.beleg(Belegart.ANGEBOT, gueltig_bis=timezone.localdate() + timedelta(days=14),
                        liefertermin=timezone.localdate() + timedelta(days=3), belegdatum=timezone.localdate() - timedelta(days=5))
        p1 = self.pos(an, menge="2")
        p2 = self.pos(an, self.ram, menge="3", preis="5")
        pk1, pk2 = p1.pk, p2.pk
        an.berechne()
        alt = (an.nummer, an.belegdatum, an.gueltig_bis, an.liefertermin, an.abgeschlossen, an.summe_brutto)
        ab_kreis = letzte(self.m, "AB")
        ab = an.folgebeleg_erzeugen(vorlaeufig=True)
        self.assertEqual(ab.pk, an.pk)
        self.assertEqual((ab.art, ab.nummer, ab.vorlaeufig), (Belegart.AUFTRAG, None, True))
        self.assertEqual(BelegVorgang.objects.get(beleg=ab).weg, Vorgangsweg.UMWANDLUNG)
        self.assertEqual(letzte(self.m, "AB"), ab_kreis)  # die Bestellung hat noch keine Nummer
        # in der vorläufigen Phase verändert: Position ändern, löschen, neu
        p1.menge = D("9")
        p1.save()
        p2.delete()
        self.pos(ab, self.ram, menge="1", preis="5")
        ab.berechne()
        vorgang.beleg_verwerfen(ab)
        an.refresh_from_db()
        self.assertEqual((an.art, an.nummer, an.belegdatum, an.gueltig_bis, an.liefertermin, an.abgeschlossen, an.summe_brutto),
                         (Belegart.ANGEBOT, alt[0], alt[1], alt[2], alt[3], alt[4], alt[5]))
        self.assertFalse(an.vorlaeufig)
        self.assertFalse(BelegUmwandlung.objects.filter(beleg=an).exists())
        self.assertFalse(BelegVorgang.objects.filter(beleg=an).exists())
        self.assertEqual([(p.pk, p.menge) for p in an.positionen.order_by("pk")], [(pk1, D("2.000")), (pk2, D("3.000"))])
        self.assertEqual(letzte(self.m, "AB"), ab_kreis)  # keine Lücke im AB-Kreis
        self.assertEqual(an.vormals, "")

    def test_umwandlung_abschliessen_macht_nummer_und_loescht_den_vorgang(self):
        an = self.beleg(Belegart.ANGEBOT)
        self.pos(an)
        alt = an.nummer
        ab = an.folgebeleg_erzeugen(vorlaeufig=True)
        ab.abschliessen()
        ab.refresh_from_db()
        self.assertTrue(ab.nummer.startswith("AB"))
        self.assertEqual(ab.umwandlungen.get().von_nummer, alt)
        self.assertFalse(BelegVorgang.objects.filter(beleg=ab).exists())

    def test_bestellung_zu_rechnung_verwerfen_gibt_alte_nummer_und_abschluss_zurueck(self):
        ab, p = self.ab("2")
        ab.abschliessen()  # abgeschlossene Bestellung (z. B. beim Kunden versandt)
        ab.refresh_from_db()
        nummer, re_kreis = ab.nummer, letzte(self.m, "RE")
        re = ab.folgebeleg_erzeugen(art=Belegart.RECHNUNG, vorlaeufig=True)
        self.assertEqual((re.pk, re.art, re.nummer, re.abgeschlossen, re.vorlaeufig), (ab.pk, Belegart.RECHNUNG, None, False, True))
        vorgang.beleg_verwerfen(re)
        ab.refresh_from_db()
        self.assertEqual((ab.art, ab.nummer, ab.abgeschlossen, ab.vorlaeufig), (Belegart.AUFTRAG, nummer, True, False))
        self.assertEqual(letzte(self.m, "RE"), re_kreis)
        self.assertEqual(ab.positionen.get().pk, p.pk)
        self.assertFalse(ab.umwandlungen.exists())

    def test_umwandlung_ueber_die_view_ist_vorlaeufig(self):
        an = self.beleg(Belegart.ANGEBOT)
        self.pos(an)
        r = self.client.post(reverse("verkauf:beleg_folgebeleg", args=[an.pk]), {"von_art": "AN"}, follow=True)
        an.refresh_from_db()
        self.assertEqual((an.art, an.nummer, an.vorlaeufig), (Belegart.AUFTRAG, None, True))
        self.assertTrue(any("umgewandelt" in m for m in self.meldungen(r)))

    def test_abo_bestellung_wird_nie_vor_dem_stichtag_zur_rechnung(self):
        abo = Abo.objects.create(mandant=self.m, kunde=self.kunde, bezeichnung="Wartung", monate=Abo.HALBMONATLICH,
                                 naechster_termin=timezone.localdate() + timedelta(days=40))
        AboPosition.objects.create(abo=abo, bezeichnung="Wartung", einzelpreis=D("100"))
        ab = abo.rechnung_erzeugen(timezone.localdate())
        self.assertGreater(ab.belegdatum, timezone.localdate())
        re = ab.folgebeleg_erzeugen(art=Belegart.RECHNUNG, vorlaeufig=True)
        self.assertEqual((re.art, re.belegdatum, re.abo_id), (Belegart.RECHNUNG, ab.belegdatum, abo.pk))


class TeiluebernahmeVerwerfenTest(VorlaeufigBasis):
    def test_teilmenge_mit_gruppe_seriennummern_und_versandkosten_exakt_zurueck(self):
        ab = self.beleg(versandkosten=D("5"))
        andere = self.pos(ab, self.ram, menge="4", preis="5")
        p = self.pos(ab, menge="10")
        kopf = Position.objects.create(beleg=ab, typ="G", bezeichnung="PC", menge=D("10"), einzelpreis=D("0"))
        r = Position.objects.create(beleg=ab, gruppe=kopf, artikel=self.ram, bezeichnung="RAM", menge=D("20"), einzelpreis=D("5"))
        mm = Position.objects.create(beleg=ab, gruppe=kopf, artikel=self.maus, bezeichnung="Maus im PC", menge=D("10"), einzelpreis=D("10"))
        ab.berechne()
        sn_text = "\n".join(f"S{i:02d}" for i in range(1, 11))
        seriennummern_einlagern(self.m, self.maus, sn_text)
        seriennummern_zuordnen(p, sn_text)
        vorher = self.stand(ab)
        ls_kreis = letzte(self.m, "LS")
        with mock.patch.object(Beleg, "_uebernahme", return_value={p.pk: D("7"), kopf.pk: D("7")}):
            ls = ab.folgebeleg_erzeugen(art=Belegart.LIEFERSCHEIN, vorlaeufig=True)
        ab.refresh_from_db()
        self.assertEqual((ls.nummer, ls.vorlaeufig, ls.versandkosten, ab.versandkosten), (None, True, D("5"), D("0")))
        self.assertEqual(Position.objects.get(pk=p.pk).menge, D("3"))
        self.assertEqual(Seriennummer.objects.filter(position__beleg=ls).count(), 7)
        ergebnis = vorgang.beleg_verwerfen(ls)
        self.assertEqual(ergebnis["meldungen"], [])  # exakter Weg: nichts zwischendurch geändert
        self.assertEqual(ergebnis["weiter"], reverse("verkauf:beleg", args=[ab.pk]))
        ab.refresh_from_db()
        self.assertEqual(self.stand(ab), vorher)  # gleiche Schlüssel, Positionsnummern, Mengen, Gruppen
        self.assertEqual(ab.versandkosten, D("5"))
        self.assertEqual(Seriennummer.objects.filter(position=p, status=Seriennummer.VERKAUFT).count(), 10)
        self.assertFalse(Beleg.objects.filter(pk=ls.pk).exists())
        self.assertFalse(BelegUebernahme.objects.exists())  # der Verlauf zum verworfenen Beleg verschwindet
        self.assertEqual(letzte(self.m, "LS"), ls_kreis)
        self.assertEqual(Position.objects.get(pk=mm.pk).menge, D("10"))
        self.assertEqual(Position.objects.get(pk=r.pk).menge, D("20"))
        self.assertEqual(Position.objects.get(pk=andere.pk).pos, 1)

    def test_neue_seriennummer_im_ziel_ist_nach_dem_verwerfen_wieder_im_lager(self):
        ab, p = self.ab("4")
        seriennummern_einlagern(self.m, self.maus, "A1\nA2\nA3\nA4")
        ls = self.teilweise(ab, p, "3")
        neu = ls.positionen.get()
        seriennummern_zuordnen(neu, "A1\nA2")
        vorgang.beleg_verwerfen(ls)
        self.assertEqual(Seriennummer.objects.filter(status=Seriennummer.IM_LAGER, position__isnull=True).count(), 4)

    def test_volle_menge_bei_vorhandenen_folgebelegen_haengt_dieselben_positionen_zurueck(self):
        ab = self.beleg()
        p1 = self.pos(ab, menge="10")
        p2 = self.pos(ab, self.ram, menge="5", preis="5")
        ab.berechne()
        erster = Beleg.objects.create(mandant=self.m, art=Belegart.LIEFERSCHEIN, kunde=self.kunde, vorgaenger=ab)
        uebernahme.menge_verschieben(ab, erster, {p1.pk: D("4")})  # Folgebeleg vorhanden: keine Umwandlung mehr möglich
        stand = self.stand(ab)
        re = ab.folgebeleg_erzeugen(art=Belegart.RECHNUNG, vorlaeufig=True)
        self.assertNotEqual(re.pk, ab.pk)
        self.assertEqual(Position.objects.get(pk=p2.pk).beleg_id, re.pk)  # ganz umgehängt
        vorgang.beleg_verwerfen(re)
        p2.refresh_from_db()
        self.assertEqual((p2.beleg_id, p2.verschoben, p2.menge), (ab.pk, False, D("5")))  # dieselbe Position
        self.assertEqual(self.stand(ab), stand)
        self.assertEqual(BelegUebernahme.objects.filter(ziel=erster).count(), 1)  # der frühere Lieferschein behält seinen Verlauf
        self.assertEqual(BelegUebernahme.objects.count(), 1)

    def test_nachziehen_im_vorlaeufigen_ziel_loescht_die_position_und_verwerfen_bringt_sie_zurueck(self):
        ab = self.beleg()
        erste = self.pos(ab, self.ram, menge="2", preis="5")
        p = self.pos(ab, menge="10")
        ab.berechne()
        vorher = self.stand(ab)
        ls = self.teilweise(ab, p, "7")
        neu = ls.positionen.get()
        r = self.client.post(reverse("verkauf:position_aendern", args=[ls.pk, neu.pk]), {"menge": "10", "preis": "10"})
        self.assertEqual(r.status_code, 302)
        self.assertFalse(Position.objects.filter(pk=p.pk).exists())  # aufgebraucht: die Bestellposition verschwindet
        ergebnis = vorgang.beleg_verwerfen(ls)
        self.assertEqual(ergebnis["meldungen"], [])  # Weg A
        self.assertEqual(self.stand(ab), vorher)
        self.assertEqual(Position.objects.get(pk=p.pk).pos, 2)  # an der alten Stelle

    def test_bestellung_zwischendurch_geaendert_robuster_weg_mit_meldung(self):
        ab = self.beleg()
        p = self.pos(ab, menge="10")
        q = self.pos(ab, self.ram, menge="4", preis="5")
        ab.berechne()
        ls = self.teilweise(ab, p, "7")
        q.refresh_from_db()
        q.menge = D("6")  # jemand ändert die Bestellung in der Zwischenzeit
        q.save()
        ergebnis = vorgang.beleg_verwerfen(ls)
        self.assertTrue(any("zwischendurch geändert" in m for m in ergebnis["meldungen"]))
        p.refresh_from_db()
        q.refresh_from_db()
        self.assertEqual((p.menge, q.menge), (D("10"), D("6")))  # Mengen stimmen, die fremde Änderung bleibt
        self.assertEqual([x.pos for x in ab.positionen.all()], [1, 2])
        self.assertFalse(Beleg.objects.filter(pk=ls.pk).exists())
        self.assertFalse(BelegUebernahme.objects.exists())

    def test_verwerfen_der_bestellung_ohne_hindernis_ist_unabhaengig_von_der_menge_der_zeilen(self):
        ab, p = self.ab("10")
        ls = ab.folgebeleg_erzeugen(art=Belegart.LIEFERSCHEIN, vorlaeufig=True)  # alles: die Position wandert komplett
        self.assertEqual(ab.positionen.count(), 0)
        vorgang.beleg_verwerfen(ls)
        self.assertEqual(Position.objects.get(pk=p.pk).beleg_id, ab.pk)


class NebenwirkungenTest(VorlaeufigBasis):
    def ab_mit_p(self):
        ab = self.beleg()
        maus = self.pos(ab, menge="2")
        p = Position.objects.create(beleg=ab, typ="P", preisliste_artikel=self.pl, bezeichnung="Netzteil", menge=D("1"), einzelpreis=D("50"))
        ab.berechne()
        return ab, maus, p

    def test_p_angebot_ohne_nummer_verwerfen_bringt_p_zurueck_und_entfernt_das_angebot(self):
        ab, maus, p = self.ab_mit_p()
        an_kreis = letzte(self.m, "AN")
        ls = ab.folgebeleg_erzeugen(art=Belegart.LIEFERSCHEIN, vorlaeufig=True)
        angebot = BelegVorgang.objects.get(beleg=ls).angebot
        self.assertEqual((angebot.art, angebot.nummer, angebot.vorlaeufig), (Belegart.ANGEBOT, None, True))
        self.assertEqual(list(angebot.positionen.values_list("pk", flat=True)), [p.pk])
        self.assertEqual(letzte(self.m, "AN"), an_kreis)
        vorgang.beleg_verwerfen(ls)
        self.assertFalse(Beleg.objects.filter(pk=angebot.pk).exists())
        self.assertEqual(Position.objects.get(pk=p.pk).beleg_id, ab.pk)
        self.assertEqual(letzte(self.m, "AN"), an_kreis)

    def test_p_angebot_wird_beim_abschluss_nummeriert(self):
        ab, maus, p = self.ab_mit_p()
        ls = ab.folgebeleg_erzeugen(art=Belegart.LIEFERSCHEIN, vorlaeufig=True)
        angebot = BelegVorgang.objects.get(beleg=ls).angebot
        ls.abschliessen()
        angebot.refresh_from_db()
        self.assertTrue(angebot.nummer.startswith("AN"))
        self.assertFalse(angebot.vorlaeufig)
        self.assertFalse(angebot.abgeschlossen)  # gespeichert, nicht abgeschlossen: wie bisher
        self.assertEqual(letzte(self.m, "AN"), 1)

    def test_bestellung_wird_erst_mit_dem_abschluss_des_ziels_erledigt(self):
        ab, maus, p = self.ab_mit_p()
        ls = ab.folgebeleg_erzeugen(art=Belegart.LIEFERSCHEIN, vorlaeufig=True)  # alles übernommen, P ins Angebot
        ab.refresh_from_db()
        self.assertFalse(ab.abgeschlossen)  # bleibt offen, solange das Ziel vorläufig ist
        vorgang.beleg_verwerfen(ls)
        ab.refresh_from_db()
        self.assertFalse(ab.abgeschlossen)
        self.assertEqual(ab.positionen.count(), 2)
        ls = ab.folgebeleg_erzeugen(art=Belegart.LIEFERSCHEIN, vorlaeufig=True)
        ls.abschliessen()
        ab.refresh_from_db()
        self.assertTrue(ab.abgeschlossen)
        self.assertTrue(ab.automatisch_erledigt)

    def test_leer_gelaufene_bestellung_ohne_p_erst_beim_abschluss_erledigt(self):
        ab, p = self.ab("3")
        ls = ab.folgebeleg_erzeugen(art=Belegart.LIEFERSCHEIN, vorlaeufig=True)
        ab.refresh_from_db()
        self.assertFalse(ab.abgeschlossen)  # sonst wäre sie beim Verwerfen zurückzudrehen
        ls.abschliessen()
        ab.refresh_from_db()
        self.assertTrue(ab.abgeschlossen and ab.automatisch_erledigt)

    def test_kopieren_mit_urbeleg_abschliessen_schliesst_die_quelle_erst_beim_abschluss(self):
        an = self.beleg(Belegart.ANGEBOT)
        self.pos(an)
        kopie = an.kopieren_als(Belegart.AUFTRAG, urbeleg_abschliessen=True, vorlaeufig=True)
        an.refresh_from_db()
        self.assertFalse(an.abgeschlossen)
        vorgang.beleg_verwerfen(kopie)
        an.refresh_from_db()
        self.assertFalse(an.abgeschlossen)
        self.assertFalse(Beleg.objects.filter(pk=kopie.pk).exists())
        kopie = an.kopieren_als(Belegart.AUFTRAG, urbeleg_abschliessen=True, vorlaeufig=True)
        kopie.abschliessen()
        an.refresh_from_db()
        self.assertTrue(an.abgeschlossen)

    def test_lieferschein_zu_rechnung_und_sammelrechnung_verwerfen_gibt_lieferscheine_frei(self):
        lss = []
        for _ in range(2):
            ls = self.beleg(Belegart.LIEFERSCHEIN)
            self.pos(ls, menge="1")
            ls.abschliessen()
            lss.append(ls)
        sammel = rechnung_aus_lieferscheinen(lss, vorlaeufig=True)
        self.assertEqual((sammel.nummer, sammel.vorlaeufig, BelegVorgang.objects.get(beleg=sammel).weg), (None, True, Vorgangsweg.SAMMEL))
        self.assertFalse(any(Beleg.objects.get(pk=ls.pk).abrechenbar for ls in lss))
        vorgang.beleg_verwerfen(sammel)
        self.assertTrue(all(Beleg.objects.get(pk=ls.pk).abrechenbar for ls in lss))
        folge = lss[0].folgebeleg_erzeugen(vorlaeufig=True)
        self.assertFalse(Beleg.objects.get(pk=lss[0].pk).abrechenbar)
        vorgang.beleg_verwerfen(folge)
        self.assertTrue(Beleg.objects.get(pk=lss[0].pk).abrechenbar)
        self.assertEqual(letzte(self.m, "RE"), 0)

    def test_gutschrift_vorlaeufig_verwerfen(self):
        re = self.beleg(Belegart.RECHNUNG)
        self.pos(re, menge="1")
        re.abschliessen()
        gs = re.gutschrift_erzeugen(vorlaeufig=True)
        self.assertEqual((gs.nummer, gs.vorlaeufig), (None, True))
        vorgang.beleg_verwerfen(gs)
        self.assertFalse(Beleg.objects.filter(pk=gs.pk).exists())
        self.assertEqual(letzte(self.m, "GS"), 0)
        gs = re.gutschrift_erzeugen()  # nicht vorläufig (Modellaufruf wie bisher)
        self.assertFalse(gs.vorlaeufig)

    def test_proforma_verwerfen_reservierung_liegt_wieder_bei_der_bestellung(self):
        ab, p = self.ab("5")
        self.assertEqual(reserviert(self.m, self.maus), D("5"))
        pf = ab.kopieren_als(Belegart.PROFORMA, vorlaeufig=True)
        self.assertEqual(reserviert(self.m, self.maus), D("5"))  # vorläufige Proforma reserviert als Schutz vor Doppelverkauf
        self.assertEqual(pf.nummer, None)
        vorgang.beleg_verwerfen(pf)
        self.assertFalse(Beleg.objects.filter(pk=pf.pk).exists())
        self.assertEqual(Position.objects.get(pk=p.pk).beleg_id, ab.pk)
        self.assertEqual(reserviert(self.m, self.maus), D("5"))


class SperrenTest(VorlaeufigBasis):
    def lies(self, antwort):
        return " ".join(self.meldungen(antwort))

    def test_alles_nach_aussen_ist_vor_dem_speichern_gesperrt(self):
        re = self.vorlaeufig(Belegart.RECHNUNG)
        ls = self.vorlaeufig(Belegart.LIEFERSCHEIN)
        ziel = "Speichern: abschließen"
        for name, args in (("verkauf:beleg_mail", [re.pk]), ("verkauf:beleg_pdf", [re.pk]), ("verkauf:beleg_ansicht", [re.pk]),
                           ("verkauf:beleg_xrechnung", [re.pk]), ("verkauf:beleg_lieferschein_pdf", [re.pk]),
                           ("verkauf:beleg_versand", [re.pk]), ("werkstatt:reklamation", [ls.pk]), ("esd:beleg", [re.pk])):
            r = self.client.get(reverse(name, args=args), follow=True)
            self.assertIn(ziel, self.lies(r), name)
        # Schreibende Aktionen
        r = self.client.post(reverse("verkauf:beleg_versand", args=[re.pk]), {"aktion": "neu", "sendungsnummer": "123"}, follow=True)
        self.assertEqual(Versand.objects.count(), 0)
        self.assertIn(ziel, self.lies(r))
        r = self.client.post(reverse("finanzen:zahlung", args=[re.pk]), {"betrag": "10", "datum": "2026-01-01", "zahlart": "ueberweisung"}, follow=True)
        self.assertEqual(Zahlung.objects.count(), 0)
        self.assertIn(ziel, self.lies(r))
        r = self.client.post(reverse("esd:zuordnen", args=[re.pk]), {"position": re.positionen.first().pk}, follow=True)
        self.assertEqual(ESDVergabe.objects.count(), 0)
        self.assertIn(ziel, self.lies(r))
        r = self.client.post(reverse("einkauf:aus_beleg", args=[re.pk]), follow=True)
        self.assertEqual(Bestellung.objects.count(), 0)
        self.assertIn(ziel, self.lies(r))
        r = self.client.post(reverse("verkauf:abo_aus_rechnung", args=[re.pk]), {"monate": "1", "start": "2026-02-01"}, follow=True)
        self.assertEqual(Abo.objects.count(), 0)
        self.assertIn(ziel, self.lies(r))
        r = self.client.post(reverse("lager:retoure", args=[re.pk]), follow=True)
        self.assertIn(ziel, self.lies(r))
        self.assertEqual(MailVersand.objects.count(), 0)

    def test_zahlung_im_service_ist_gesperrt(self):
        re = self.vorlaeufig(Belegart.RECHNUNG)
        with self.assertRaisesMessage(ValueError, "Nummer vergeben"):
            zahlung_buchen(re, D("10"), date(2026, 1, 1), "ueberweisung")
        Beleg.objects.filter(pk=re.pk).update(vorlaeufig=False)  # veraltetes Objekt im Speicher: die Datenbank entscheidet
        re_alt = Beleg.objects.get(pk=re.pk)
        Beleg.objects.filter(pk=re.pk).update(vorlaeufig=True)
        with self.assertRaises(ValueError):
            zahlung_buchen(re_alt, D("10"), date(2026, 1, 1), "ueberweisung")

    def test_vorlaeufig_sperre_methode(self):
        re = self.vorlaeufig(Belegart.RECHNUNG)
        with self.assertRaisesMessage(ValueError, "Drucken erst nach „Speichern: abschließen & Nummer vergeben“."):
            re.vorlaeufig_sperre("Drucken")
        re.abschliessen()
        re.vorlaeufig_sperre("Drucken")  # gespeichert: erlaubt

    def test_verwerfhindernis_zahlung_altfall_ergibt_fehler_und_aendert_nichts(self):
        re = self.vorlaeufig(Belegart.RECHNUNG)
        Zahlung.objects.create(mandant=self.m, beleg=re, betrag=D("5"), datum=date(2026, 1, 1), zahlart="ueberweisung")
        with self.assertRaisesMessage(ValueError, "Zahlungen"):
            vorgang.beleg_verwerfen(re)
        self.assertTrue(Beleg.objects.filter(pk=re.pk).exists())
        self.assertTrue(Position.objects.filter(beleg=re).exists())
        r = self.client.post(reverse("verkauf:beleg_verwerfen", args=[re.pk]), HTTP_X_WWOK_STAND="1")
        self.assertEqual((r.status_code, r.json()["ok"]), (409, False))

    def test_erst_abschliessen_vor_dem_uebernehmen(self):
        ab = self.vorlaeufig(Belegart.AUFTRAG)
        url = reverse("verkauf:beleg_folgebeleg", args=[ab.pk])
        r = self.client.post(url, {"von_art": "AB", "art": "LS"})
        self.assertEqual(r.status_code, 200)  # Rückfrage „Erst abschließen & Nummer vergeben?“
        self.assertTemplateUsed(r, "verkauf/erst_abschliessen_frage.html")
        ab.refresh_from_db()
        self.assertTrue(ab.vorlaeufig)
        self.assertFalse(Beleg.objects.filter(art=Belegart.LIEFERSCHEIN).exists())
        self.client.post(url, {"von_art": "AB", "art": "LS", "erst_abschliessen": "1"})
        ab.refresh_from_db()
        self.assertTrue(ab.abgeschlossen and ab.nummer.startswith("AB") and not ab.vorlaeufig)
        ls = Beleg.objects.get(art=Belegart.LIEFERSCHEIN)
        self.assertTrue(ls.vorlaeufig and ls.nummer is None and ls.vorgaenger_id == ab.pk)  # nie Ketten vorläufiger Belege
        # Kopieren genauso (nur ein nicht gespeichertes ANGEBOT fragt nicht: siehe test_vorlaeufig_angebot)
        re = self.vorlaeufig(Belegart.RECHNUNG)
        r = self.client.post(reverse("verkauf:beleg_kopieren", args=[re.pk]), {"art": "AN"})
        self.assertTemplateUsed(r, "verkauf/erst_abschliessen_frage.html")
        self.client.post(reverse("verkauf:beleg_kopieren", args=[re.pk]), {"art": "AN", "erst_abschliessen": "1"})
        re.refresh_from_db()
        self.assertFalse(re.vorlaeufig)
        self.assertTrue(Beleg.objects.filter(art=Belegart.ANGEBOT, vorlaeufig=True, vorgaenger=re).exists())


class BereinigungTest(VorlaeufigBasis):
    def alt(self, beleg, stunden=2, minuten=1):
        Beleg.objects.filter(pk=beleg.pk).update(zuletzt_aktiv=timezone.now() - timedelta(hours=stunden, minutes=minuten))

    def test_nach_einer_stunde_ohne_lebenszeichen_verworfen_vorher_nicht(self):
        b = self.vorlaeufig(Belegart.RECHNUNG)
        self.alt(b, 0, 59)
        self.assertEqual(Beleg.vorlaeufige_verwerfen(), [])
        self.assertTrue(Beleg.objects.filter(pk=b.pk).exists())
        self.alt(b, 1, 1)
        liste = Beleg.vorlaeufige_verwerfen()
        self.assertEqual([(e["beleg"].pk, e["verworfen"], e["fehler"]) for e in liste], [(b.pk, True, "")])
        self.assertFalse(Beleg.objects.filter(pk=b.pk).exists())

    def test_ohne_aktivitaetszeit_zaehlt_das_anlagedatum(self):
        b = self.vorlaeufig(Belegart.RECHNUNG)
        Beleg.objects.filter(pk=b.pk).update(zuletzt_aktiv=None, angelegt_am=timezone.now() - timedelta(hours=3))
        Beleg.vorlaeufige_verwerfen()
        self.assertFalse(Beleg.objects.filter(pk=b.pk).exists())

    def test_probelauf_aendert_nichts(self):
        b = self.vorlaeufig(Belegart.RECHNUNG)
        self.alt(b)
        liste = Beleg.vorlaeufige_verwerfen(ausfuehren=False)
        self.assertEqual([(e["beleg"].pk, e["verworfen"]) for e in liste], [(b.pk, False)])
        self.assertTrue(Beleg.objects.filter(pk=b.pk).exists())
        ausgabe = StringIO()
        call_command("belege_verwerfen", stdout=ausgabe)
        self.assertIn("PROBELAUF", ausgabe.getvalue())
        self.assertTrue(Beleg.objects.filter(pk=b.pk).exists())
        call_command("belege_verwerfen", "--ausfuehren", "--mandant", "okay", stdout=ausgabe)
        self.assertFalse(Beleg.objects.filter(pk=b.pk).exists())
        with self.assertRaises(CommandError):
            call_command("belege_verwerfen", "--mandant", "gibtsnicht")

    def test_shop_abo_werkstatt_und_altentwuerfe_werden_nie_angefasst(self):
        shop_ab = self.vorlaeufig(Belegart.AUFTRAG)
        shop = Shop.objects.create(mandant=self.m, name="S", url="https://s.example", client_id="x")
        ShopBestellung.objects.create(shop=shop, shop_id_extern="1", bestellnummer="1", beleg=shop_ab)
        abo = Abo.objects.create(mandant=self.m, kunde=self.kunde, bezeichnung="W", naechster_termin=date(2026, 1, 31))
        abo_ab = self.vorlaeufig(Belegart.AUFTRAG)
        Beleg.objects.filter(pk=abo_ab.pk).update(abo=abo)
        ws_re = self.vorlaeufig(Belegart.RECHNUNG)
        Reparatur.objects.create(mandant=self.m, nummer="WS1", kunde=self.kunde, geraet="Notebook", fehler="x", rechnung=ws_re)
        altentwurf = self.beleg(Belegart.ANGEBOT)
        self.pos(altentwurf)
        for b in (shop_ab, abo_ab, ws_re, altentwurf):
            self.alt(b, 5)
        Beleg.vorlaeufige_verwerfen()
        for b in (shop_ab, abo_ab, ws_re, altentwurf):
            self.assertTrue(Beleg.objects.filter(pk=b.pk).exists(), b)

    def test_puls_nach_der_auswahl_verhindert_das_verwerfen(self):
        b = self.vorlaeufig(Belegart.RECHNUNG)
        self.alt(b)

        def puls(selbst):  # läuft nach der Auswahl der Kandidaten, vor der Sperre
            Beleg.objects.filter(pk=b.pk).update(zuletzt_aktiv=timezone.now())
            return "Neu angelegt"
        with mock.patch.object(BelegVorgang, "get_weg_display", puls):
            liste = Beleg.vorlaeufige_verwerfen()
        self.assertEqual([e["verworfen"] for e in liste], [False])
        self.assertTrue(Beleg.objects.filter(pk=b.pk).exists())

    def test_bereinigung_stellt_umwandlung_und_mengen_zurueck(self):
        ab, p = self.ab("10")
        ls = self.teilweise(ab, p, "7")
        self.alt(ls)
        Beleg.vorlaeufige_verwerfen(self.m)
        self.assertFalse(Beleg.objects.filter(pk=ls.pk).exists())
        self.assertEqual(Position.objects.get(pk=p.pk).menge, D("10"))

    def test_lazy_bereinigung_auf_startseite_und_kundenseite(self):
        b = self.vorlaeufig(Belegart.RECHNUNG)
        self.alt(b)
        self.client.get(reverse("verkauf:start"))
        self.assertFalse(Beleg.objects.filter(pk=b.pk).exists())
        b = self.vorlaeufig(Belegart.RECHNUNG)
        self.alt(b)
        self.client.get(reverse("verkauf:kunde", args=[self.kunde.pk]))
        self.assertFalse(Beleg.objects.filter(pk=b.pk).exists())

    def test_leere_entwuerfe_entfernen_fasst_vorlaeufige_nicht_an(self):
        b = self.vorlaeufig(Belegart.RECHNUNG, mit_position=False)
        Beleg.objects.filter(pk=b.pk).update(angelegt_am=timezone.now() - timedelta(hours=1))
        Beleg.leere_entwuerfe_entfernen(self.m)
        self.assertTrue(Beleg.objects.filter(pk=b.pk).exists())


class AktivitaetTest(VorlaeufigBasis):
    def test_beleg_aktiv_aktualisiert_und_meldet_verworfene(self):
        b = self.vorlaeufig(Belegart.RECHNUNG)
        Beleg.objects.filter(pk=b.pk).update(zuletzt_aktiv=timezone.now() - timedelta(hours=1))
        vor = Beleg.objects.get(pk=b.pk).zuletzt_aktiv
        r = self.client.post(reverse("verkauf:beleg_aktiv", args=[b.pk]))
        self.assertEqual(r.json(), {"ok": True})
        self.assertGreater(Beleg.objects.get(pk=b.pk).zuletzt_aktiv, vor)
        vorgang.beleg_verwerfen(b)
        r = self.client.post(reverse("verkauf:beleg_aktiv", args=[b.pk]))
        data = r.json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["weiter"], reverse("verkauf:start"))
        self.assertEqual(self.client.get(reverse("verkauf:beleg_aktiv", args=[b.pk])).status_code, 405)

    def test_verlassen_signal_verwirft_bei_der_naechsten_bereinigung(self):
        b = self.vorlaeufig(Belegart.RECHNUNG)
        r = self.client.post(reverse("verkauf:beleg_verlassen", args=[b.pk]))
        self.assertEqual(r.status_code, 204)
        self.assertEqual(Beleg.vorlaeufige_verwerfen(), [])  # gleich danach noch nicht: ein Neuladen (F5) hält den Beleg
        with mock.patch("django.utils.timezone.now", return_value=timezone.now() + timedelta(seconds=35)):
            liste = Beleg.vorlaeufige_verwerfen()
        self.assertEqual([(e["beleg"].pk, e["verworfen"]) for e in liste], [(b.pk, True)])
        self.assertFalse(Beleg.objects.filter(pk=b.pk).exists())

    def test_neuladen_nach_verlassen_signal_haelt_den_beleg(self):
        # echte Reihenfolge im Browser: neue Seite (GET), dann das verspätete „verlassen“ der alten, dann das Lebenszeichen nach 3 s
        b = self.vorlaeufig(Belegart.RECHNUNG)
        self.client.get(reverse("verkauf:beleg", args=[b.pk]))
        self.client.post(reverse("verkauf:beleg_verlassen", args=[b.pk]))
        self.client.post(reverse("verkauf:beleg_aktiv", args=[b.pk]))
        with mock.patch("django.utils.timezone.now", return_value=timezone.now() + timedelta(seconds=35)):
            self.assertEqual(Beleg.vorlaeufige_verwerfen(), [])
        self.assertTrue(Beleg.objects.filter(pk=b.pk).exists())

    def test_verlassen_signal_laesst_gespeicherte_und_fremde_belege_in_ruhe(self):
        b = self.vorlaeufig(Belegart.AUFTRAG)
        b.abschliessen()
        self.client.post(reverse("verkauf:beleg_verlassen", args=[b.pk]))
        self.assertIsNone(Beleg.objects.get(pk=b.pk).zuletzt_aktiv)
        fremd = Mandant.objects.create(kurzname="ZONE", firmenname="ZONE Handel")
        f = Beleg.objects.create(mandant=fremd, art=Belegart.RECHNUNG, kunde=self.kunde, vorlaeufig=True)
        vorgang.vorgang_beginnen(f, Vorgangsweg.NEU)
        vor = Beleg.objects.get(pk=f.pk).zuletzt_aktiv
        self.client.post(reverse("verkauf:beleg_verlassen", args=[f.pk]))
        self.assertEqual(Beleg.objects.get(pk=f.pk).zuletzt_aktiv, vor)
        self.assertEqual(self.client.get(reverse("verkauf:beleg_verlassen", args=[b.pk])).status_code, 405)

    def test_maske_ohne_hinweisbalken_mit_verlassen_signal(self):
        b = self.vorlaeufig(Belegart.RECHNUNG)
        html = self.client.get(reverse("verkauf:beleg", args=[b.pk])).content.decode()
        self.assertNotIn("Automatisch nach 2 Stunden", html)
        self.assertNotIn("Ohne „Speichern: abschließen &amp; Nummer vergeben“ wird dieser Beleg verworfen", html)
        self.assertIn(reverse("verkauf:beleg_verlassen", args=[b.pk]), html)
        self.assertIn("pagehide", html)
        self.assertIn("setTimeout(lebenszeichen, 3000)", html)

    def test_beleg_aktiv_auf_gespeicherten_beleg_ist_nicht_ok(self):
        b = self.vorlaeufig(Belegart.AUFTRAG)
        b.abschliessen()
        data = self.client.post(reverse("verkauf:beleg_aktiv", args=[b.pk])).json()
        self.assertEqual((data["ok"], data["weiter"]), (False, reverse("verkauf:beleg", args=[b.pk])))

    def test_positions_posts_und_aufruf_zaehlen_als_aktivitaet(self):
        b = self.vorlaeufig(Belegart.RECHNUNG)
        p = b.positionen.get()
        for aktion in ("detail", "neu", "aendern", "kopf"):
            Beleg.objects.filter(pk=b.pk).update(zuletzt_aktiv=timezone.now() - timedelta(hours=1))
            vor = Beleg.objects.get(pk=b.pk).zuletzt_aktiv
            if aktion == "detail":
                self.client.get(reverse("verkauf:beleg", args=[b.pk]))
            elif aktion == "neu":
                self.client.post(reverse("verkauf:position_neu", args=[b.pk]), {"artikel": "M1", "menge": "1", "preis": "10"})
            elif aktion == "aendern":
                self.client.post(reverse("verkauf:position_aendern", args=[b.pk, p.pk]), {"menge": "3", "preis": "10"})
            else:
                self.client.post(reverse("verkauf:beleg_kopf", args=[b.pk]), {"bemerkung": "x"})
            self.assertGreater(Beleg.objects.get(pk=b.pk).zuletzt_aktiv, vor, aktion)


class TeillieferungTest(VorlaeufigBasis):
    def setUp(self):
        super().setUp()
        st = Steuersatz.objects.get(nummer=1)
        self.knapp = Artikel.objects.create(nummer="K1", bezeichnung="Knapp", steuersatz=st, vk1=D("8"))  # kein Bestand

    def test_vorlaeufiger_lieferschein_aus_bestellung_rest_geht_sofort_zurueck(self):
        ab = self.beleg()
        maus = self.pos(ab, menge="2")
        k = self.pos(ab, self.knapp, menge="5", preis="8")
        ab.berechne()
        ls = ab.folgebeleg_erzeugen(art=Belegart.LIEFERSCHEIN, vorlaeufig=True)
        neu = ls.teillieferung_abschliessen()
        neu.refresh_from_db()
        self.assertTrue(neu.abgeschlossen and neu.nummer.startswith("LS") and not neu.vorlaeufig)
        self.assertFalse(Beleg.objects.filter(pk=ls.pk).exists())  # kein Rest-Entwurf
        self.assertEqual(neu.rest_beleg.pk, ab.pk)
        ab.refresh_from_db()
        self.assertFalse(ab.abgeschlossen)
        self.assertEqual([(p.bezeichnung, p.menge) for p in ab.positionen.all()], [("Knapp", D("5.000"))])  # Rest wieder in der Bestellung (robuster Weg: neue Position)
        self.assertEqual(Position.objects.get(pk=maus.pk).beleg_id, neu.pk)
        self.assertTrue(BelegUebernahme.objects.filter(zielposition=maus).exists())  # Verlauf des gelieferten Teils bleibt
        self.assertFalse(BelegUebernahme.objects.filter(zielposition=k).exists())

    def test_teillieferung_ueber_die_view_leitet_auf_die_bestellung(self):
        ab = self.beleg()
        self.pos(ab, menge="2")
        self.pos(ab, self.knapp, menge="5", preis="8")
        ab.berechne()
        ls = ab.folgebeleg_erzeugen(art=Belegart.LIEFERSCHEIN, vorlaeufig=True)
        r = self.client.post(reverse("verkauf:beleg_teillieferung", args=[ls.pk]))
        self.assertRedirects(r, reverse("verkauf:beleg", args=[ab.pk]), fetch_redirect_response=False)

    def test_vorlaeufige_umwandlung_zu_rechnung_wird_zurueckgedreht_und_teil_rechnung_hat_die_bestellung_als_vorgaenger(self):
        ab = self.beleg()
        self.pos(ab, menge="2")
        k = self.pos(ab, self.knapp, menge="5", preis="8")
        ab.berechne()
        ab.abschliessen()
        ab.refresh_from_db()
        nummer = ab.nummer
        re = ab.folgebeleg_erzeugen(art=Belegart.RECHNUNG, vorlaeufig=True)
        self.assertEqual(re.pk, ab.pk)
        neu = re.teillieferung_abschliessen()
        ab.refresh_from_db()
        self.assertEqual((ab.art, ab.nummer, ab.vorlaeufig), (Belegart.AUFTRAG, nummer, False))  # wieder die Bestellung mit alter Nummer
        self.assertEqual([p.pk for p in ab.positionen.all()], [k.pk])
        self.assertEqual((neu.art, neu.vorgaenger_id, neu.abgeschlossen), (Belegart.RECHNUNG, ab.pk, True))
        self.assertTrue(neu.nummer.startswith("RE"))
        self.assertEqual(neu.rest_beleg.pk, ab.pk)
        self.assertTrue(BelegUebernahme.objects.filter(ziel=neu).exists())

    def test_neuer_beleg_ohne_bestellung_bleibt_mit_dem_rest_vorlaeufig(self):
        re = self.vorlaeufig(Belegart.RECHNUNG, mit_position=False)
        self.pos(re, menge="2")
        k = self.pos(re, self.knapp, menge="5", preis="8")
        re.berechne()
        neu = re.teillieferung_abschliessen()
        self.assertTrue(neu.abgeschlossen and not neu.vorlaeufig)
        self.assertEqual(neu.rest_beleg.pk, re.pk)
        re.refresh_from_db()
        self.assertTrue(re.vorlaeufig and re.nummer is None)
        self.assertEqual([p.pk for p in re.positionen.all()], [k.pk])
        self.assertTrue(BelegVorgang.objects.filter(beleg=re).exists())


class AboTest(VorlaeufigBasis):
    def abo(self, **felder):
        abo = Abo.objects.create(mandant=self.m, kunde=self.kunde, bezeichnung="Wartung", naechster_termin=date(2026, 1, 31),
                                 starttag=31, **felder)
        AboPosition.objects.create(abo=abo, bezeichnung="Wartung", einzelpreis=D("100"))
        return abo

    def test_cron_legt_bestellung_mit_nummer_an_nicht_vorlaeufig(self):
        abo = self.abo()
        b = abo.rechnung_erzeugen(date(2026, 1, 31))
        self.assertEqual((b.art, b.abo_id, b.vorlaeufig), (Belegart.AUFTRAG, abo.pk, False))
        self.assertTrue(b.nummer.startswith("AB"))
        self.assertEqual(b.bemerkung, "Wiederkehrende Bestellung: Wartung")
        self.assertEqual(abo.letzte_rechnung, b)
        ausgabe = StringIO()
        abo.naechster_termin = date(2026, 2, 28)
        abo.save()
        call_command("wiederkehrende_rechnungen", stdout=ausgabe)
        self.assertIn("Bestellung AB", ausgabe.getvalue())

    def test_jetzt_oeffnet_die_offene_bestellung_statt_den_naechsten_zeitraum_anzulegen(self):
        abo = self.abo()
        b = abo.rechnung_erzeugen(date(2026, 1, 31))
        anzahl = Beleg.objects.count()
        r = self.client.post(reverse("verkauf:abo_aktion", args=[abo.pk]), {"aktion": "jetzt"})
        self.assertRedirects(r, reverse("verkauf:beleg", args=[b.pk]), fetch_redirect_response=False)
        self.assertEqual(Beleg.objects.count(), anzahl)
        # in Rechnung umgewandelt, noch nicht abgeschlossen: ebenfalls offen
        re = b.folgebeleg_erzeugen(art=Belegart.RECHNUNG)
        self.assertEqual(abo.offener_beleg().pk, re.pk)
        r = self.client.post(reverse("verkauf:abo_aktion", args=[abo.pk]), {"aktion": "jetzt"})
        self.assertRedirects(r, reverse("verkauf:beleg", args=[re.pk]), fetch_redirect_response=False)
        re.abschliessen()
        self.assertIsNone(Abo.objects.get(pk=abo.pk).offener_beleg())
        self.client.post(reverse("verkauf:abo_aktion", args=[abo.pk]), {"aktion": "jetzt"})
        self.assertEqual(Beleg.objects.filter(abo=abo).count(), 2)  # die Rechnung und die neue Bestellung

    def test_abo_bestellung_wird_nie_automatisch_verworfen(self):
        abo = self.abo()
        b = abo.rechnung_erzeugen(date(2026, 1, 31))
        Beleg.objects.filter(pk=b.pk).update(zuletzt_aktiv=timezone.now() - timedelta(days=3), vorlaeufig=True)
        vorgang.vorgang_beginnen(Beleg.objects.get(pk=b.pk), Vorgangsweg.NEU)
        Beleg.vorlaeufige_verwerfen()
        self.assertTrue(Beleg.objects.filter(pk=b.pk).exists())


class AnsichtenTest(VorlaeufigBasis):
    def test_dashboard_zaehlt_vorlaeufige_nicht(self):
        ab = self.vorlaeufig(Belegart.AUFTRAG)
        an = self.vorlaeufig(Belegart.ANGEBOT)
        self.assertEqual(dashboard.offene_kundenbestellungen(self.m)["anzahl"], 0)
        self.assertEqual(dashboard.offene_angebote(self.m, timezone.localdate()), 0)
        ab.abschliessen()  # gespeichert, aber abgeschlossen: zählt auch nicht; ein offener (ungespeicherter Altentwurf) schon
        alt_ab = self.beleg(Belegart.AUFTRAG)
        self.pos(alt_ab)
        alt_an = self.beleg(Belegart.ANGEBOT)
        self.pos(alt_an)
        self.assertEqual(dashboard.offene_kundenbestellungen(self.m)["anzahl"], 2)
        self.assertEqual(dashboard.offene_angebote(self.m, timezone.localdate()), 1)
        an.abschliessen()

    def test_startseite_trennt_vorlaeufige_von_altentwuerfen(self):
        vorl = self.vorlaeufig(Belegart.RECHNUNG)
        alt = self.beleg(Belegart.ANGEBOT)
        self.pos(alt)
        r = self.client.get(reverse("verkauf:start"))
        self.assertEqual([b.pk for b in r.context["offene"]], [alt.pk])
        self.assertEqual([b.pk for b in r.context["vorlaeufige"]], [vorl.pk])
        r = self.client.get(reverse("verkauf:beleg", args=[vorl.pk]))
        self.assertTrue(r.context["verwerfen_moeglich"])
        self.assertTrue(r.context["loeschbar"])


class OberflaecheTest(VorlaeufigBasis):
    """Belegmaske und Listen: Balken, Verlassen-Skript, Verwerfen-Knopf und Marke gibt es nur bei nicht gespeicherten Belegen;
    vor dem Speichern sind alle Wege nach außen (PDF, Druck, Mail, Zahlung, Versand, ESD, Lieferantenbestellung …) ausgeblendet."""

    def seite(self, beleg) -> str:
        r = self.client.get(reverse("verkauf:beleg", args=[beleg.pk]))
        self.assertEqual(r.status_code, 200)
        return r.content.decode()

    def test_vorlaeufiger_beleg_zeigt_balken_skript_und_verwerfen(self):
        b = self.vorlaeufig(Belegart.RECHNUNG)
        html = self.seite(b)
        self.assertNotIn("vorlaeufigbalken", html)  # kein Hinweisbalken (Wunsch des Nutzers), Marke am Titel genügt
        self.assertIn('id="vorlaeufigskript"', html)
        self.assertIn(reverse("verkauf:beleg_verwerfen", args=[b.pk]), html)
        self.assertIn(reverse("verkauf:beleg_aktiv", args=[b.pk]), html)
        self.assertIn('id="verwerfenform"', html)
        self.assertIn(">Verwerfen</button>", html)
        self.assertNotIn(reverse("verkauf:beleg_loeschen", args=[b.pk]), html)  # „Löschen“ wird „Verwerfen“
        self.assertIn("(nicht gespeichert)", html)
        for text in ("Abschließen & Nummer vergeben", "Zurück zum Beleg", "erst_abschliessen"):
            self.assertIn(text, html)
        self.assertNotIn("(Entwurf)", html)

    def test_vorlaeufiger_beleg_blendet_alle_wege_nach_aussen_aus(self):
        b = self.vorlaeufig(Belegart.RECHNUNG)
        html = self.seite(b)
        for url in (reverse("verkauf:beleg_pdf", args=[b.pk]), reverse("verkauf:beleg_ansicht", args=[b.pk]),
                    reverse("verkauf:beleg_mail", args=[b.pk]), reverse("finanzen:beleg", args=[b.pk]),
                    reverse("verkauf:beleg_versand", args=[b.pk]), reverse("esd:beleg", args=[b.pk]),
                    reverse("verkauf:abo_aus_rechnung", args=[b.pk]), reverse("verkauf:beleg_xrechnung", args=[b.pk])):
            self.assertNotIn(url, html)
        ab = self.vorlaeufig(Belegart.AUFTRAG)
        self.assertNotIn(reverse("einkauf:aus_beleg", args=[ab.pk]), self.seite(ab))

    def test_uebernehmen_und_kopieren_fragen_erst_nach_dem_abschliessen(self):
        ab = self.vorlaeufig(Belegart.AUFTRAG)
        html = self.seite(ab)
        self.assertIn(reverse("verkauf:beleg_folgebeleg", args=[ab.pk]), html)
        self.assertIn("data-bleibt data-erst", html)
        self.assertGreaterEqual(html.count("data-erst"), 2)

    def test_gespeicherter_beleg_hat_weder_balken_noch_skript(self):
        b = self.vorlaeufig(Belegart.RECHNUNG)
        b.abschliessen()
        html = self.seite(b)
        for text in ("vorlaeufigbalken", "vorlaeufigskript", "verwerfenform", "data-erst", "(nicht gespeichert)", ">nicht gespeichert<"):
            self.assertNotIn(text, html)
        self.assertIn(reverse("verkauf:beleg_pdf", args=[b.pk]), html)  # Knöpfe wieder da
        self.assertIn(reverse("verkauf:beleg_mail", args=[b.pk]), html)

    def test_altentwurf_bleibt_normaler_entwurf(self):
        alt = self.beleg(Belegart.ANGEBOT)
        self.pos(alt)
        html = self.seite(alt)
        for text in ("vorlaeufigbalken", "vorlaeufigskript", "verwerfenform"):
            self.assertNotIn(text, html)
        self.assertIn(">Entwurf<", html)
        self.assertIn(reverse("verkauf:beleg_loeschen", args=[alt.pk]), html)

    def test_listen_markieren_nur_vorlaeufige(self):
        vorl = self.vorlaeufig(Belegart.RECHNUNG)
        alt = self.beleg(Belegart.ANGEBOT)
        self.pos(alt)
        start = self.client.get(reverse("verkauf:start")).content.decode()
        self.assertIn("Nicht gespeicherte Belege", start)
        self.assertEqual(start.count("nicht gespeichert</span>"), 1)
        self.assertIn(reverse("verkauf:beleg", args=[vorl.pk]), start)
        for url in (reverse("verkauf:kunde", args=[self.kunde.pk]), reverse("verkauf:belege")):
            html = self.client.get(url, {"status": ""} if url.endswith("belege/") else {}).content.decode()
            self.assertEqual(html.count("nicht gespeichert</span>"), 1, url)
            self.assertIn(alt.nummer, html, url)  # der Altentwurf behält seine Nummer ohne Marke

    def test_startseite_ohne_vorlaeufige_hat_keine_karte(self):
        alt = self.beleg(Belegart.ANGEBOT)
        self.pos(alt)
        self.assertNotIn("Nicht gespeicherte Belege", self.client.get(reverse("verkauf:start")).content.decode())

    def test_verwerfen_per_fetch_und_aktiv_antworten_json(self):
        b = self.vorlaeufig(Belegart.RECHNUNG)
        r = self.client.post(reverse("verkauf:beleg_verwerfen", args=[b.pk]), headers={"X-Wwok-Stand": "1"})
        self.assertTrue(r.json()["ok"])
        self.assertFalse(Beleg.objects.filter(pk=b.pk).exists())
        data = self.client.post(reverse("verkauf:beleg_aktiv", args=[b.pk])).json()  # der Puls einer alten Maske
        self.assertFalse(data["ok"])
        self.assertIn("weiter", data)
