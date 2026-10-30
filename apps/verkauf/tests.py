from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.kern.models import Mandant
from apps.stammdaten.models import Artikel, Kunde, Steuersatz

from .models import Beleg, BelegGesperrt, Belegart, Position


@override_settings(SECURE_SSL_REDIRECT=False)
class VerkaufTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.m2 = Mandant.objects.create(kurzname="ZONE", firmenname="ZONE Handel")
        self.kunde = Kunde.objects.create(nummer="1000", name1="Muster GmbH", ort="Solingen", preisstufe=2)
        st = Steuersatz.objects.create(bezeichnung="19", prozent=Decimal("19"))
        self.art = Artikel.objects.create(nummer="A1", bezeichnung="Maus", steuersatz=st,
                                          vk1=Decimal("10"), vk2=Decimal("8"))

    def beleg(self, art=Belegart.RECHNUNG, mandant=None):
        return Beleg.objects.create(mandant=mandant or self.m, art=art, kunde=self.kunde)

    def test_summen_und_steuer(self):
        b = self.beleg()
        Position.objects.create(beleg=b, artikel=self.art, bezeichnung="Maus", menge=Decimal("3"),
                                einzelpreis=Decimal("8"), steuer_prozent=Decimal("19"))
        b.versandkosten = Decimal("5")
        b.save()
        b.berechne()
        self.assertEqual(b.summe_netto, Decimal("29.00"))
        self.assertEqual(b.summe_steuer, Decimal("5.51"))
        self.assertEqual(b.summe_brutto, Decimal("34.51"))

    def test_nummernkreis_pro_mandant_lueckenlos(self):
        a = self.beleg(Belegart.ANGEBOT)
        b = self.beleg(Belegart.ANGEBOT)
        c = self.beleg(Belegart.ANGEBOT, self.m2)
        self.assertTrue(a.nummer.endswith("00001") and b.nummer.endswith("00002") and c.nummer.endswith("00001"))

    def test_rechnung_wird_beim_abschluss_nummeriert_und_gesperrt(self):
        b = self.beleg()
        self.assertIsNone(b.nummer)
        with self.assertRaises(ValueError):
            b.abschliessen()
        Position.objects.create(beleg=b, bezeichnung="Dienst", einzelpreis=Decimal("100"))
        b.abschliessen()
        b.refresh_from_db()
        self.assertTrue(b.nummer.startswith("RE") and b.gesperrt)
        with self.assertRaises(BelegGesperrt):
            Position.objects.create(beleg=b, bezeichnung="x", einzelpreis=Decimal("1"))
        b.versandart = "DHL"
        b.save()  # Versandart bleibt änderbar
        b.versandkosten = Decimal("9")
        with self.assertRaises(BelegGesperrt):
            b.save()

    def test_doppelter_abschluss_vergibt_nur_eine_nummer(self):
        b = self.beleg()
        Position.objects.create(beleg=b, bezeichnung="Dienst", einzelpreis=Decimal("100"))
        zweiter_tab = Beleg.objects.get(pk=b.pk)  # veralteter Zustand, wie bei Doppelklick/zweitem Tab
        b.abschliessen()
        nummer = b.nummer
        zweiter_tab.abschliessen()
        self.assertEqual(zweiter_tab.nummer, nummer)
        self.assertTrue(zweiter_tab.abgeschlossen)
        self.assertEqual(Beleg.objects.filter(art=Belegart.RECHNUNG, nummer__isnull=False).count(), 1)

    def test_belegfluss(self):
        a = self.beleg(Belegart.ANGEBOT)
        Position.objects.create(beleg=a, artikel=self.art, bezeichnung="Maus", einzelpreis=Decimal("8"))
        auftrag = a.folgebeleg_erzeugen()
        self.assertEqual((auftrag.art, auftrag.vorgaenger, auftrag.positionen.count()), ("AB", a, 1))
        self.assertEqual(auftrag.summe_brutto, Decimal("9.52"))

    def test_masken_und_mandantentrennung(self):
        user = get_user_model().objects.create_user("t", password="x" * 14)
        self.client.force_login(user)
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()
        r = self.client.post(reverse("verkauf:beleg_neu", args=[self.kunde.pk]), {"art": "AB"})
        b = Beleg.objects.get(art="AB")
        self.assertRedirects(r, reverse("verkauf:beleg", args=[b.pk]))
        self.client.post(reverse("verkauf:position_neu", args=[b.pk]),
                         {"artikel": "A1", "menge": "2", "preis": "", "typ": "A"})
        b.refresh_from_db()
        self.assertEqual(b.summe_netto, Decimal("16.00"))  # VK2 des Kunden
        fremd = self.beleg(Belegart.ANGEBOT, self.m2)
        self.assertEqual(self.client.get(reverse("verkauf:beleg", args=[fremd.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("verkauf:api_artikel") + "?q=Ma&kunde=%d" % self.kunde.pk)
                         .json()["treffer"][0]["preis"], "8.00")

    def test_artikelsuche_mehrere_woerter_beliebige_reihenfolge(self):
        user = get_user_model().objects.create_user("s", password="x" * 14)
        self.client.force_login(user)
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()
        Artikel.objects.create(nummer="A2", bezeichnung="Netzteil 500W", herstellernummer="AZZA-500",
                               steuersatz=self.art.steuersatz)
        url = reverse("verkauf:api_artikel")
        for q in ("netz azza", "azza netz", "NETZ 500"):
            self.assertEqual([t["nummer"] for t in self.client.get(url, {"q": q}).json()["treffer"]], ["A2"], q)
        self.assertEqual(self.client.get(url, {"q": "netz maus"}).json()["treffer"], [])

    def test_faelligkeit_pdf_und_gutschrift(self):
        from datetime import date

        from apps.stammdaten.models import Zahlungsbedingung

        from .pdf import beleg_pdf
        zb = Zahlungsbedingung.objects.create(bezeichnung="10 Tage", tage_netto=10,
                                              druck_text="Zahlbar innerhalb 10 Tagen bis zum ff.ff.ffff")
        b = Beleg.objects.create(mandant=self.m, art=Belegart.RECHNUNG, kunde=self.kunde, zahlungsbedingung=zb,
                                 belegdatum=date(2026, 3, 25))
        Position.objects.create(beleg=b, bezeichnung="Dienst äöü", einzelpreis=Decimal("100"))
        self.assertEqual(b.zahlungstext, "Zahlbar innerhalb 10 Tagen bis zum 04.04.2026")
        self.assertTrue(beleg_pdf(b).startswith(b"%PDF"))  # Entwurf
        b.abschliessen()
        b.refresh_from_db()
        g = b.gutschrift_erzeugen()
        self.assertEqual((g.art, g.vorgaenger, g.summe_brutto), ("GS", b, Decimal("119.00")))
        with self.assertRaises(ValueError):
            g.gutschrift_erzeugen()

    def test_mailversand(self):
        from django.core import mail

        from .mail import MailFehler, beleg_senden, vorschlag
        from .models import MailVersand
        self.m.smtp_host, self.m.absender_email = "smtp.example.test", "rechnung@okay.test"
        self.m.save()
        self.kunde.email = "kunde@example.test"
        self.kunde.save()
        b = Beleg.objects.create(mandant=self.m, art=Belegart.RECHNUNG, kunde=self.kunde)
        Position.objects.create(beleg=b, bezeichnung="Dienst", einzelpreis=Decimal("10"))
        with self.assertRaises(MailFehler):  # Entwurf ohne Nummer
            beleg_senden(b, "x@example.test", "s", "t")
        b.abschliessen()
        b.refresh_from_db()
        an, betreff, text = vorschlag(b)
        self.assertEqual(an, "kunde@example.test")
        self.kunde.email_weitere = "buchhaltung@example.test, kunde@example.test"
        self.kunde.save()
        b.kunde.refresh_from_db()
        self.assertEqual(vorschlag(b)[0], "kunde@example.test, buchhaltung@example.test")
        self.assertIn(b.nummer, betreff)
        self.assertTrue(text.startswith("Sehr geehrte Damen und Herren"))
        with self.settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend"), \
                self.assertRaises(MailFehler):  # SMTP-Server nicht erreichbar -> Fehler wird protokolliert
            beleg_senden(b, an, betreff, text)
        self.assertFalse(MailVersand.objects.get().erfolgreich)

    def test_login_pflicht(self):
        self.assertEqual(self.client.get(reverse("verkauf:start")).status_code, 302)


@override_settings(SECURE_SSL_REDIRECT=False)
class GruppenTest(TestCase):
    def setUp(self):
        from apps.stammdaten.models import GruppenKomponente
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.kunde = Kunde.objects.create(nummer="1", name1="K", preisstufe=1)
        st = Steuersatz.objects.create(bezeichnung="19", prozent=Decimal("19"))
        self.cpu = Artikel.objects.create(nummer="CPU", bezeichnung="Prozessor", steuersatz=st, vk1=Decimal("100"))
        self.ram = Artikel.objects.create(nummer="RAM", bezeichnung="Speicher", steuersatz=st, vk1=Decimal("40"))
        self.pc = Artikel.objects.create(nummer="PC1", typ="G", bezeichnung="Büro-PC", steuersatz=st)
        GruppenKomponente.objects.create(gruppe=self.pc, komponente=self.cpu, menge=Decimal("1"))
        GruppenKomponente.objects.create(gruppe=self.pc, komponente=self.ram, menge=Decimal("2"))
        user = get_user_model().objects.create_user("t", password="x" * 14)
        self.client.force_login(user)
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()
        self.beleg = Beleg.objects.create(mandant=self.m, art=Belegart.LIEFERSCHEIN, kunde=self.kunde)

    def post(self, **daten):
        return self.client.post(reverse("verkauf:position_neu", args=[self.beleg.pk]), daten)

    def test_gruppenartikel_komponenten_preis_und_abbuchung(self):
        from apps.lager.models import Bewegungsart, bestand, gruppen_verfuegbar
        from apps.lager.services import buche_manuell
        buche_manuell(self.m, self.cpu, Bewegungsart.WARENEINGANG, Decimal("3"))
        buche_manuell(self.m, self.ram, Bewegungsart.WARENEINGANG, Decimal("5"))
        self.assertEqual(gruppen_verfuegbar(self.m, self.pc), Decimal("2"))  # RAM begrenzt: 5 // 2
        self.post(artikel="PC1", menge="2", typ="G")
        kopf = self.beleg.positionen.get(gruppe__isnull=True)
        self.assertEqual(kopf.einzelpreis, Decimal("180"))  # 100 + 2 × 40
        self.assertEqual(self.beleg.positionen.filter(gruppe=kopf).count(), 2)
        self.beleg.refresh_from_db()
        self.assertEqual(self.beleg.summe_netto, Decimal("360.00"))  # Unterartikel zählen nicht extra
        kopf_url = reverse("verkauf:gruppenpreis", args=[self.beleg.pk, kopf.pk])
        self.client.post(kopf_url, {"preis": "150"})
        self.beleg.refresh_from_db()
        self.assertEqual(self.beleg.summe_netto, Decimal("300.00"))  # manuell überschrieben
        self.beleg.abschliessen()
        self.assertEqual((bestand(self.m, self.cpu), bestand(self.m, self.ram)), (Decimal("1"), Decimal("1")))
        from .pdf import beleg_pdf
        self.assertTrue(beleg_pdf(self.beleg).startswith(b"%PDF"))

    def test_freie_gruppe_und_folgebeleg_kopiert_struktur(self):
        self.post(typ="G", bezeichnung="Angebot Netzwerk", preis="500", menge="1")
        kopf = self.beleg.positionen.get()
        self.post(artikel="CPU", menge="1", gruppe=str(kopf.pk))
        self.post(artikel="RAM", menge="4", gruppe=str(kopf.pk))
        self.assertEqual(self.beleg.positionen.filter(gruppe=kopf).count(), 2)
        self.beleg.refresh_from_db()
        self.assertEqual(self.beleg.summe_netto, Decimal("760.00"))  # 500 Pauschale + später hinzugefügte Teile (100 + 4 × 40)
        self.client.post(reverse("verkauf:gruppenpreis", args=[self.beleg.pk, kopf.pk]), {"preis": ""})
        kopf.refresh_from_db()
        self.assertEqual(kopf.einzelpreis, Decimal("260.00"))  # Summe der Unterpositionen: 100 + 4 × 40
        Beleg.objects.filter(pk=self.beleg.pk).update(abgeschlossen=True)  # Lieferschein ist abgebucht
        self.beleg.refresh_from_db()
        re = self.beleg.folgebeleg_erzeugen()
        neu_kopf = re.positionen.get(gruppe__isnull=True)
        self.assertEqual(re.positionen.filter(gruppe=neu_kopf).count(), 2)
        self.assertEqual(re.summe_netto, Decimal("260.00"))
        self.assertContains(self.client.get(reverse("verkauf:beleg", args=[re.pk])), "Angebot Netzwerk")


@override_settings(SECURE_SSL_REDIRECT=False)
class PositionsnummerTest(TestCase):
    def test_nummern_ruecken_nach_loeschen_auf(self):
        m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        k = Kunde.objects.create(nummer="1", name1="K")
        b = Beleg.objects.create(mandant=m, art=Belegart.ANGEBOT, kunde=k)
        ps = [Position.objects.create(beleg=b, bezeichnung=f"P{i}", menge=1, einzelpreis=Decimal("1")) for i in range(3)]
        kopf = Position.objects.create(beleg=b, typ="G", bezeichnung="PC", menge=1, einzelpreis=Decimal("5"))
        Position.objects.create(beleg=b, gruppe=kopf, bezeichnung="Teil", menge=1, einzelpreis=Decimal("5"))
        ps[0].delete()
        ps[1].delete()
        b.berechne()
        self.assertEqual([(p.bezeichnung, p.pos) for p in b.positionen.all()], [("P2", 1), ("PC", 2), ("Teil", 2)])


@override_settings(SECURE_SSL_REDIRECT=False)
class StartOffeneTest(TestCase):
    def test_start_zeigt_offene_belege(self):
        m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        k = Kunde.objects.create(nummer="1", name1="Offenkunde")
        Position.objects.create(beleg=Beleg.objects.create(mandant=m, art=Belegart.ANGEBOT, kunde=k), bezeichnung="X",
                                einzelpreis=Decimal("1"))
        Beleg.objects.create(mandant=m, art=Belegart.ANGEBOT, kunde=Kunde.objects.create(nummer="2", name1="Fertigkunde"),
                             abgeschlossen=True)
        self.client.force_login(get_user_model().objects.create_user("t", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = m.pk
        s.save()
        r = self.client.get(reverse("verkauf:start"))
        self.assertContains(r, "Offenkunde")
        self.assertNotContains(r, "Fertigkunde")


@override_settings(SECURE_SSL_REDIRECT=False)
class BelegKopierenTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.k = Kunde.objects.create(nummer="1", name1="K")
        self.b = Beleg.objects.create(mandant=self.m, art=Belegart.ANGEBOT, kunde=self.k)
        Position.objects.create(beleg=self.b, bezeichnung="X", menge=2, einzelpreis=Decimal("5"))
        self.client.force_login(get_user_model().objects.create_user("t", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    def test_angebot_als_rechnung_kopieren_und_urbeleg_abschliessen(self):
        r = self.client.post(reverse("verkauf:beleg_kopieren", args=[self.b.pk]), {"art": "RE", "urbeleg_abschliessen": "1"})
        neu = Beleg.objects.get(art=Belegart.RECHNUNG)
        self.assertRedirects(r, reverse("verkauf:beleg", args=[neu.pk]))
        self.assertEqual(neu.positionen.count(), 1)
        self.assertEqual(neu.vorgaenger, self.b)
        self.b.refresh_from_db()
        self.assertTrue(self.b.abgeschlossen)

    def test_urbeleg_bleibt_offen_und_gleiche_art_moeglich(self):
        self.client.post(reverse("verkauf:beleg_kopieren", args=[self.b.pk]), {"art": "AN", "urbeleg_abschliessen": "0"})
        self.assertEqual(Beleg.objects.filter(art=Belegart.ANGEBOT).count(), 2)
        self.b.refresh_from_db()
        self.assertFalse(self.b.abgeschlossen)


@override_settings(SECURE_SSL_REDIRECT=False)
class TeillieferungTest(TestCase):
    def setUp(self):
        from apps.lager.models import Bewegungsart
        from apps.lager.services import buche_manuell
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        st = Steuersatz.objects.create(bezeichnung="19", prozent=Decimal("19"))
        self.a = Artikel.objects.create(nummer="A1", bezeichnung="Maus", steuersatz=st, vk1=Decimal("10"))
        self.b = Artikel.objects.create(nummer="A2", bezeichnung="Tastatur", steuersatz=st, vk1=Decimal("20"))
        buche_manuell(self.m, self.a, Bewegungsart.WARENEINGANG, Decimal("5"))
        k = Kunde.objects.create(nummer="1", name1="K")
        self.re = Beleg.objects.create(mandant=self.m, art=Belegart.RECHNUNG, kunde=k, versandkosten=Decimal("5"))
        Position.objects.create(beleg=self.re, artikel=self.a, bezeichnung="Maus", menge=2, einzelpreis=Decimal("10"))
        Position.objects.create(beleg=self.re, artikel=self.b, bezeichnung="Tastatur", menge=1, einzelpreis=Decimal("20"))
        self.client.force_login(get_user_model().objects.create_user("t", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    def test_abschluss_bietet_teillieferung_an(self):
        r = self.client.post(reverse("verkauf:beleg_abschliessen", args=[self.re.pk]))
        self.assertContains(r, "Teillieferung")
        self.re.refresh_from_db()
        self.assertFalse(self.re.abgeschlossen)

    def test_teillieferung_trennt_positionen(self):
        self.client.post(reverse("verkauf:beleg_teillieferung", args=[self.re.pk]))
        self.re.refresh_from_db()
        self.assertFalse(self.re.abgeschlossen)
        self.assertEqual([p.bezeichnung for p in self.re.positionen.all()], ["Tastatur"])
        fertig = Beleg.objects.get(art=Belegart.RECHNUNG, abgeschlossen=True)
        self.assertEqual([p.bezeichnung for p in fertig.positionen.all()], ["Maus"])
        self.assertEqual(fertig.versandkosten, Decimal("5"))
        self.assertEqual(self.re.versandkosten, Decimal("0"))

    def test_nichts_lieferbar_keine_teillieferung(self):
        self.re.positionen.filter(artikel=self.a).update(menge=9)
        r = self.client.post(reverse("verkauf:beleg_abschliessen", args=[self.re.pk]), follow=True)
        self.assertNotContains(r, "Ja, Teillieferung")


@override_settings(SECURE_SSL_REDIRECT=False)
class RechnungOhneLieferscheinTest(TestCase):
    def test_rechnung_aus_offenem_lieferschein_nicht_moeglich(self):
        m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        st = Steuersatz.objects.create(bezeichnung="19", prozent=Decimal("19"))
        a = Artikel.objects.create(nummer="A1", bezeichnung="Maus", steuersatz=st, vk1=Decimal("10"))
        k = Kunde.objects.create(nummer="1", name1="K")
        ls = Beleg.objects.create(mandant=m, art=Belegart.LIEFERSCHEIN, kunde=k)
        Position.objects.create(beleg=ls, artikel=a, bezeichnung="Maus", menge=1, einzelpreis=Decimal("10"))
        with self.assertRaisesMessage(ValueError, "Lieferschein erst abschließen"):
            ls.folgebeleg_erzeugen()
        with self.assertRaisesMessage(ValueError, "Lieferschein erst abschließen"):
            ls.kopieren_als("RE")
        # Rechnung, die mit offenem Lieferschein verknüpft wurde (Altbestand), lässt sich nicht abschließen
        re = Beleg.objects.create(mandant=m, art=Belegart.RECHNUNG, kunde=k, vorgaenger=ls)
        Position.objects.create(beleg=re, artikel=a, bezeichnung="Maus", menge=1, einzelpreis=Decimal("10"))
        with self.assertRaisesMessage(ValueError, "Erst den Lieferschein"):
            re.abschliessen()


@override_settings(SECURE_SSL_REDIRECT=False)
class DruckSperreTest(TestCase):
    def test_pdf_von_lieferschein_ohne_bestand_gesperrt(self):
        m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        st = Steuersatz.objects.create(bezeichnung="19", prozent=Decimal("19"))
        a = Artikel.objects.create(nummer="A1", bezeichnung="Maus", steuersatz=st, vk1=Decimal("10"))
        k = Kunde.objects.create(nummer="1", name1="K")
        ls = Beleg.objects.create(mandant=m, art=Belegart.LIEFERSCHEIN, kunde=k)
        Position.objects.create(beleg=ls, artikel=a, bezeichnung="Maus", menge=1, einzelpreis=Decimal("10"))
        angebot = Beleg.objects.create(mandant=m, art=Belegart.ANGEBOT, kunde=k)
        Position.objects.create(beleg=angebot, artikel=a, bezeichnung="Maus", menge=1, einzelpreis=Decimal("10"))
        self.client.force_login(get_user_model().objects.create_user("t", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = m.pk
        s.save()
        r = self.client.get(reverse("verkauf:beleg_pdf", args=[ls.pk]), follow=True)
        self.assertContains(r, "Lagerbestand reicht nicht")
        self.assertNotEqual(r.get("Content-Type"), "application/pdf")
        self.assertEqual(self.client.get(reverse("verkauf:beleg_pdf", args=[angebot.pk])).status_code, 200)  # Angebot immer druckbar
        self.assertContains(self.client.get(reverse("verkauf:beleg", args=[ls.pk])), "Druck nicht möglich")


@override_settings(SECURE_SSL_REDIRECT=False)
class LeereEntwuerfeTest(TestCase):
    def test_leere_entwuerfe_verschwinden_und_nummer_wird_frei(self):
        from datetime import timedelta
        from django.utils import timezone
        m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        k = Kunde.objects.create(nummer="1", name1="K")
        leer = Beleg.objects.create(mandant=m, art=Belegart.LIEFERSCHEIN, kunde=k)
        frisch = Beleg.objects.create(mandant=m, art=Belegart.LIEFERSCHEIN, kunde=k)
        mit = Beleg.objects.create(mandant=m, art=Belegart.ANGEBOT, kunde=k)
        Position.objects.create(beleg=mit, bezeichnung="X", menge=1, einzelpreis=Decimal("1"))
        alt = timezone.now() - timedelta(minutes=10)
        Beleg.objects.filter(pk__in=[leer.pk, mit.pk]).update(angelegt_am=alt)
        self.client.force_login(get_user_model().objects.create_user("t", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = m.pk
        s.save()
        self.client.get(reverse("verkauf:kunde", args=[k.pk]))
        self.assertFalse(Beleg.objects.filter(pk=leer.pk).exists())
        self.assertTrue(Beleg.objects.filter(pk=frisch.pk).exists())   # zu neu: wird eventuell gerade bearbeitet
        self.assertTrue(Beleg.objects.filter(pk=mit.pk).exists())      # hat eine Position
        neu = Beleg.objects.create(mandant=m, art=Belegart.LIEFERSCHEIN, kunde=k)
        self.assertEqual(neu.nummer.rsplit("-", 1)[1], "00003")        # 00001 leer entfernt, 00002 frisch, 00003 neu


@override_settings(SECURE_SSL_REDIRECT=False)
class LieferscheinNurAusAuftragTest(TestCase):
    def test_lieferschein_nicht_direkt_anlegbar(self):
        m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        k = Kunde.objects.create(nummer="1", name1="K")
        self.client.force_login(get_user_model().objects.create_user("t", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = m.pk
        s.save()
        self.client.post(reverse("verkauf:beleg_neu", args=[k.pk]), {"art": "LS"})
        self.assertFalse(Beleg.objects.filter(art=Belegart.LIEFERSCHEIN).exists())
        angebot = Beleg.objects.create(mandant=m, art=Belegart.ANGEBOT, kunde=k)
        Position.objects.create(beleg=angebot, bezeichnung="X", menge=1, einzelpreis=Decimal("1"))
        with self.assertRaisesMessage(ValueError, "nur aus einem Auftrag"):
            angebot.kopieren_als("LS")
        auftrag = angebot.folgebeleg_erzeugen()
        self.assertEqual(auftrag.art, Belegart.AUFTRAG)
        self.assertEqual(auftrag.folgebeleg_erzeugen().art, Belegart.LIEFERSCHEIN)


@override_settings(SECURE_SSL_REDIRECT=False)
class UrbelegLoeschenTest(TestCase):
    def test_angebot_und_auftrag_nach_uebernahme_loeschbar(self):
        m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        k = Kunde.objects.create(nummer="1", name1="K")
        angebot = Beleg.objects.create(mandant=m, art=Belegart.ANGEBOT, kunde=k)
        Position.objects.create(beleg=angebot, bezeichnung="X", menge=1, einzelpreis=Decimal("1"))
        auftrag = angebot.folgebeleg_erzeugen()
        self.assertIsNone(angebot.loeschhindernis())
        angebot.delete()
        auftrag.refresh_from_db()
        self.assertIsNone(auftrag.vorgaenger)
        self.assertEqual(auftrag.positionen.count(), 1)
        ls = auftrag.folgebeleg_erzeugen()
        self.assertIsNone(auftrag.loeschhindernis())
        auftrag.delete()
        ls.refresh_from_db()
        self.assertEqual(ls.positionen.count(), 1)


@override_settings(SECURE_SSL_REDIRECT=False)
class ArtikelSucheTypTest(TestCase):
    def test_typ_filter_und_stern(self):
        m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        st = Steuersatz.objects.create(bezeichnung="19", prozent=Decimal("19"))
        Artikel.objects.create(nummer="A1", bezeichnung="NAS Gehäuse", steuersatz=st, vk1=Decimal("100"))
        Artikel.objects.create(nummer="G1", typ="G", bezeichnung="NAS Komplettpaket", steuersatz=st)
        Artikel.objects.create(nummer="L1", typ="L", bezeichnung="Einrichtung", steuersatz=st, vk1=Decimal("50"))
        self.client.force_login(get_user_model().objects.create_user("t", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = m.pk
        s.save()
        url = reverse("verkauf:api_artikel")
        nummern = lambda **p: sorted(t["nummer"] for t in self.client.get(url, p).json()["treffer"])
        self.assertEqual(nummern(q="nas", typ="G"), ["G1"])
        self.assertEqual(nummern(q="nas", typ="A"), ["A1"])
        self.assertEqual(nummern(q="*", typ="G"), ["G1"])
        self.assertEqual(nummern(q="*", typ="L"), ["L1"])
        self.assertEqual(nummern(q="*", typ="A"), ["A1"])


@override_settings(SECURE_SSL_REDIRECT=False)
class GruppenSummeTest(TestCase):
    def test_gruppenpreis_summiert_unterpositionen_automatisch(self):
        m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        k = Kunde.objects.create(nummer="1", name1="K")
        b = Beleg.objects.create(mandant=m, art=Belegart.ANGEBOT, kunde=k)
        kopf = Position.objects.create(beleg=b, typ="G", bezeichnung="NAS Paket", menge=1, einzelpreis=Decimal("0"))
        Position.objects.create(beleg=b, gruppe=kopf, bezeichnung="Gehäuse", menge=1, einzelpreis=Decimal("371.84"))
        teil = Position.objects.create(beleg=b, gruppe=kopf, bezeichnung="HDD", menge=2, einzelpreis=Decimal("273.58"))
        b.berechne()
        kopf.refresh_from_db()
        self.assertEqual(kopf.einzelpreis, Decimal("919.00"))      # 371,84 + 2 × 273,58
        self.assertEqual(b.summe_netto, Decimal("919.00"))
        teil.delete()
        b.berechne()
        kopf.refresh_from_db()
        self.assertEqual(kopf.einzelpreis, Decimal("371.84"))      # folgt den Unterpositionen
        Position.objects.filter(pk=kopf.pk).update(einzelpreis=Decimal("300"), preis_fest=True)
        b.berechne()
        kopf.refresh_from_db()
        self.assertEqual(kopf.einzelpreis, Decimal("300"))         # fest eingetragener Preis bleibt


class EkMargeCodeTest(TestCase):
    def test_code_aus_ek_und_marge(self):
        from apps.stammdaten.models import ArtikelLieferant, Lieferant
        m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        k = Kunde.objects.create(nummer="1", name1="K")
        st = Steuersatz.objects.create(bezeichnung="19", prozent=Decimal("19"))
        a = Artikel.objects.create(nummer="A1", bezeichnung="Maus", steuersatz=st, durchschnitts_ek=Decimal("40"))
        b = Beleg.objects.create(mandant=m, art=Belegart.ANGEBOT, kunde=k)
        Position.objects.create(beleg=b, artikel=a, bezeichnung="Maus", menge=10, einzelpreis=Decimal("50"))
        self.assertEqual(b.ek_marge_code, "400,0020,00")   # EK 400,00 · Marge (500 − 400) / 500
        Position.objects.create(beleg=b, bezeichnung="Service", menge=1, einzelpreis=Decimal("100"))
        self.assertTrue(b.ek_marge_code.endswith("*"))     # Position ohne EK
        ls = Beleg.objects.create(mandant=m, art=Belegart.LIEFERSCHEIN, kunde=k)
        self.assertEqual(ls.ek_marge_code, "")


@override_settings(SECURE_SSL_REDIRECT=False)
class KreditlimitAngebotTest(TestCase):
    def test_angebot_ohne_kreditwarnung(self):
        from apps.stammdaten.models import KundeMandant
        m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        k = Kunde.objects.create(nummer="1", name1="K")
        KundeMandant.objects.create(kunde=k, mandant=m, kreditlimit=Decimal("10"))
        self.client.force_login(get_user_model().objects.create_user("t", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = m.pk
        s.save()
        for art, erwartet in ((Belegart.ANGEBOT, False), (Belegart.AUFTRAG, True)):
            b = Beleg.objects.create(mandant=m, art=art, kunde=k)
            Position.objects.create(beleg=b, bezeichnung="X", menge=1, einzelpreis=Decimal("100"))
            b.berechne()
            r = self.client.get(reverse("verkauf:beleg", args=[b.pk]))
            self.assertEqual(r.context["kredit_warnung"], erwartet, art)


@override_settings(SECURE_SSL_REDIRECT=False)
class PositionInGruppeVerschiebenTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.k = Kunde.objects.create(nummer="1", name1="K")
        self.st = Steuersatz.objects.create(bezeichnung="19", prozent=Decimal("19"))
        self.a = Artikel.objects.create(nummer="A1", bezeichnung="Gehäuse", steuersatz=self.st, vk1=Decimal("100"))
        self.client.force_login(get_user_model().objects.create_user("t", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()
        self.beleg = Beleg.objects.create(mandant=self.m, art=Belegart.ANGEBOT, kunde=self.k)

    def position(self, **werte):
        daten = dict(beleg=self.beleg, artikel=self.a, bezeichnung="Gehäuse", einzelpreis=Decimal("100"))
        return Position.objects.create(**{**daten, **werte})

    def gruppe(self, **werte):
        return Position.objects.create(**{**dict(beleg=self.beleg, typ="G", bezeichnung="PC", einzelpreis=Decimal("0")), **werte})

    def aendern(self, p, gruppe, **felder):
        daten = {"menge": str(p.menge), "preis": str(p.einzelpreis), "rabatt": str(p.rabatt_prozent), "gruppe": gruppe, **felder}
        return self.client.post(reverse("verkauf:position_aendern", args=[self.beleg.pk, p.pk]), daten)

    def test_aeltere_position_rueckt_unter_die_gruppe_und_summe_bleibt(self):
        p = self.position(menge=Decimal("2"))
        kopf = self.gruppe()
        self.beleg.berechne()
        self.aendern(p, str(kopf.pk))
        p.refresh_from_db()
        kopf.refresh_from_db()
        self.beleg.refresh_from_db()
        self.assertEqual(p.gruppe_id, kopf.pk)
        self.assertEqual(kopf.einzelpreis, Decimal("200.00"))
        self.assertEqual(self.beleg.summe_netto, Decimal("200.00"))
        self.assertEqual(list(self.beleg.positionen.all()), [kopf, p])
        self.assertEqual((kopf.pos, p.pos), (1, 1))

    def test_kopie_behaelt_die_gruppe_der_aelteren_position(self):
        p = self.position()
        kopf = self.gruppe()
        self.aendern(p, str(kopf.pk))
        kopie = self.beleg.kopieren_als(Belegart.AUFTRAG)
        teil = kopie.positionen.get(typ="A")
        self.assertEqual(teil.gruppe, kopie.positionen.get(typ="G"))
        self.assertEqual(kopie.summe_netto, Decimal("100.00"))

    def test_rabatt_geht_in_den_preis_ein(self):
        p = self.position(rabatt_prozent=Decimal("10"))
        self.aendern(p, str(self.gruppe().pk))
        p.refresh_from_db()
        self.beleg.refresh_from_db()
        self.assertEqual((p.einzelpreis, p.rabatt_prozent), (Decimal("90.00"), Decimal("0")))
        self.assertEqual(self.beleg.summe_netto, Decimal("90.00"))

    def test_fester_gruppenpreis_steigt_und_sinkt_um_den_betrag(self):
        p = self.position()
        kopf = self.gruppe(einzelpreis=Decimal("150"), preis_fest=True)
        self.aendern(p, str(kopf.pk))
        kopf.refresh_from_db()
        self.assertEqual(kopf.einzelpreis, Decimal("250.00"))
        p.refresh_from_db()
        self.aendern(p, "")
        kopf.refresh_from_db()
        p.refresh_from_db()
        self.beleg.refresh_from_db()
        self.assertIsNone(p.gruppe_id)
        self.assertEqual(kopf.einzelpreis, Decimal("150.00"))
        self.assertEqual(self.beleg.summe_netto, Decimal("250.00"))

    def test_herausnehmen_der_letzten_unterposition_zaehlt_nicht_doppelt(self):
        kopf = self.gruppe()
        p = self.position(gruppe=kopf)
        self.beleg.berechne()
        self.aendern(p, "")
        kopf.refresh_from_db()
        self.beleg.refresh_from_db()
        self.assertEqual(kopf.einzelpreis, Decimal("0.00"))
        self.assertEqual(self.beleg.summe_netto, Decimal("100.00"))

    def test_wechsel_in_andere_gruppe_mit_mengenaenderung(self):
        eins, zwei = self.gruppe(), self.gruppe(bezeichnung="Server")
        p = self.position(gruppe=eins)
        self.position(gruppe=eins)
        self.beleg.berechne()
        self.aendern(p, str(zwei.pk), menge="3")
        eins.refresh_from_db()
        zwei.refresh_from_db()
        self.assertEqual((eins.einzelpreis, zwei.einzelpreis), (Decimal("100.00"), Decimal("300.00")))

    def test_gruppe_und_freie_position_lassen_sich_nicht_verschieben(self):
        eins, zwei = self.gruppe(), self.gruppe(bezeichnung="Server")
        self.aendern(zwei, str(eins.pk))
        zwei.refresh_from_db()
        self.assertIsNone(zwei.gruppe_id)
        frei = self.position(artikel=None, bezeichnung="Sonderteil")
        self.aendern(frei, str(eins.pk))
        frei.refresh_from_db()
        self.assertIsNone(frei.gruppe_id)

    def test_fremde_gruppe_und_abgeschlossener_beleg(self):
        p = self.position()
        anderer = Beleg.objects.create(mandant=self.m, art=Belegart.ANGEBOT, kunde=self.k)
        fremd = Position.objects.create(beleg=anderer, typ="G", bezeichnung="X", einzelpreis=Decimal("0"))
        self.assertEqual(self.aendern(p, str(fremd.pk)).status_code, 404)
        p.refresh_from_db()
        self.assertIsNone(p.gruppe_id)
        kopf = self.gruppe()
        Beleg.objects.filter(pk=self.beleg.pk).update(art=Belegart.RECHNUNG, abgeschlossen=True)
        self.aendern(p, str(kopf.pk))
        p.refresh_from_db()
        self.assertIsNone(p.gruppe_id)

    def test_auswahl_erscheint_nur_wo_sie_passt(self):
        self.position()
        self.gruppe()
        self.position(artikel=None, bezeichnung="Sonderteil")
        self.beleg.berechne()
        html = self.client.get(reverse("verkauf:beleg", args=[self.beleg.pk])).content.decode()
        self.assertEqual(html.count("<label>Gruppe</label>"), 1)
        self.assertIn("– keine –", html)


    def bleibt(self, p, gruppe, **felder):
        """Der Wechsel wird abgelehnt: Position und Belegsumme bleiben unverändert."""
        vorher = (p.gruppe_id, p.einzelpreis, p.rabatt_prozent)
        self.aendern(p, gruppe, **felder)
        p.refresh_from_db()
        self.assertEqual((p.gruppe_id, p.einzelpreis, p.rabatt_prozent), vorher)

    def test_abgeschlossene_rechnung_laesst_festen_gruppenpreis_unberuehrt(self):
        kopf = self.gruppe(einzelpreis=Decimal("250"), preis_fest=True)
        p = self.position(gruppe=kopf)
        Beleg.objects.filter(pk=self.beleg.pk).update(art=Belegart.RECHNUNG, abgeschlossen=True)
        self.bleibt(p, "")
        kopf.refresh_from_db()
        self.assertEqual(kopf.einzelpreis, Decimal("250.00"))
        self.client.post(reverse("verkauf:position_loeschen", args=[self.beleg.pk, p.pk]))
        kopf.refresh_from_db()
        self.assertEqual(kopf.einzelpreis, Decimal("250.00"))
        self.assertTrue(Position.objects.filter(pk=p.pk).exists())

    def test_unterschiedliche_steuer_wird_abgelehnt(self):
        p = self.position(steuer_prozent=Decimal("7"))
        self.bleibt(p, str(self.gruppe().pk))

    def test_eingabezeile_sperrt_fremden_steuersatz(self):
        kopf = self.gruppe()
        ermaessigt = Steuersatz.objects.create(bezeichnung="7", prozent=Decimal("7"))
        Artikel.objects.create(nummer="B1", bezeichnung="Buch", steuersatz=ermaessigt, vk1=Decimal("20"))
        url = reverse("verkauf:position_neu", args=[self.beleg.pk])
        r = self.client.post(url, {"typ": "A", "artikel": "B1", "menge": "1", "gruppe": str(kopf.pk)}, follow=True)
        self.assertContains(r, "unterschiedliche Steuersätze")
        self.assertFalse(kopf.unterpositionen.exists())
        self.client.post(url, {"typ": "A", "artikel": "A1", "menge": "1", "gruppe": str(kopf.pk)})
        self.assertEqual(kopf.unterpositionen.count(), 1)

    def test_gruppe_mit_rabatt_wird_abgelehnt(self):
        kopf = self.gruppe(rabatt_prozent=Decimal("10"))
        self.bleibt(self.position(), str(kopf.pk))
        self.bleibt(self.position(gruppe=kopf), "")

    def test_rabatt_der_nicht_centgenau_aufgeht_wird_abgelehnt(self):
        p = self.position(menge=Decimal("100"), einzelpreis=Decimal("0.99"), rabatt_prozent=Decimal("15"))
        self.bleibt(p, str(self.gruppe().pk))

    def test_anderer_lieferschein_wird_abgelehnt(self):
        ls = Beleg.objects.create(mandant=self.m, art=Belegart.LIEFERSCHEIN, kunde=self.k)
        self.bleibt(self.position(lieferschein=ls), str(self.gruppe().pk))

    def test_negativer_gruppenpreis_wird_abgelehnt(self):
        kopf = self.gruppe(einzelpreis=Decimal("50"), preis_fest=True)
        self.bleibt(self.position(gruppe=kopf), "")
        kopf.refresh_from_db()
        self.assertEqual(kopf.einzelpreis, Decimal("50.00"))

    def test_verborgener_rabatt_einer_unterposition_aendert_den_wert_nicht(self):
        kopf = self.gruppe()
        p = self.position(gruppe=kopf, rabatt_prozent=Decimal("10"))
        self.beleg.berechne()
        self.aendern(p, "")
        p.refresh_from_db()
        self.beleg.refresh_from_db()
        self.assertEqual((p.einzelpreis, p.rabatt_prozent), (Decimal("100.00"), Decimal("0")))
        self.assertEqual(self.beleg.summe_netto, Decimal("100.00"))


@override_settings(SECURE_SSL_REDIRECT=False)
class FesterGruppenpreisErhoehenTest(TestCase):
    def test_spaeter_hinzugefuegte_teile_erhoehen_festen_preis(self):
        m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        k = Kunde.objects.create(nummer="1", name1="K")
        st = Steuersatz.objects.create(bezeichnung="19", prozent=Decimal("19"))
        a = Artikel.objects.create(nummer="A1", bezeichnung="Gehäuse", steuersatz=st, vk1=Decimal("100"))
        b2 = Artikel.objects.create(nummer="A2", bezeichnung="SSD", steuersatz=st, vk1=Decimal("60"))
        self.client.force_login(get_user_model().objects.create_user("t", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = m.pk
        s.save()
        beleg = Beleg.objects.create(mandant=m, art=Belegart.ANGEBOT, kunde=k)
        url = reverse("verkauf:position_neu", args=[beleg.pk])
        self.client.post(url, {"typ": "G", "bezeichnung": "Paket", "menge": "1", "preis": "150"})  # Pauschalpreis
        kopf = beleg.positionen.get()
        self.client.post(url, {"typ": "A", "artikel": "A1", "menge": "1", "gruppe": str(kopf.pk)})
        kopf.refresh_from_db()
        self.assertEqual(kopf.einzelpreis, Decimal("250.00"))   # 150 Pauschale + 100 neu
        self.client.post(url, {"typ": "A", "artikel": "A2", "menge": "2", "gruppe": str(kopf.pk)})
        kopf.refresh_from_db()
        self.assertEqual(kopf.einzelpreis, Decimal("370.00"))   # + 2 × 60


@override_settings(SECURE_SSL_REDIRECT=False)
class FesterGruppenpreisLoeschenTest(TestCase):
    def test_loeschen_senkt_festen_preis(self):
        m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        k = Kunde.objects.create(nummer="1", name1="K")
        st = Steuersatz.objects.create(bezeichnung="19", prozent=Decimal("19"))
        Artikel.objects.create(nummer="A1", bezeichnung="Gehäuse", steuersatz=st, vk1=Decimal("100"))
        self.client.force_login(get_user_model().objects.create_user("t", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = m.pk
        s.save()
        beleg = Beleg.objects.create(mandant=m, art=Belegart.ANGEBOT, kunde=k)
        url = reverse("verkauf:position_neu", args=[beleg.pk])
        self.client.post(url, {"typ": "G", "bezeichnung": "Paket", "menge": "1", "preis": "150"})
        kopf = beleg.positionen.get()
        self.client.post(url, {"typ": "A", "artikel": "A1", "menge": "1", "gruppe": str(kopf.pk)})
        teil = beleg.positionen.get(gruppe=kopf)
        self.client.post(reverse("verkauf:position_loeschen", args=[beleg.pk, teil.pk]))
        kopf.refresh_from_db()
        self.assertEqual(kopf.einzelpreis, Decimal("150.00"))   # zurück auf die Pauschale
