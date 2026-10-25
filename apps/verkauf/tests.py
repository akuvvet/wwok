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
        self.assertEqual(self.beleg.summe_netto, Decimal("500.00"))
        self.client.post(reverse("verkauf:gruppenpreis", args=[self.beleg.pk, kopf.pk]), {"preis": ""})
        kopf.refresh_from_db()
        self.assertEqual(kopf.einzelpreis, Decimal("260.00"))  # Summe der Unterpositionen: 100 + 4 × 40
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
        Beleg.objects.create(mandant=m, art=Belegart.ANGEBOT, kunde=k)
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
