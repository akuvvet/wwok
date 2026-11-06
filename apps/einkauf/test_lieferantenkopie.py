"""Issue 61: Kundenbestellung → Lieferantenbestellung nur nach Rückfrage erneut kopieren; Verlauf am Kundenbeleg."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.kern.models import Mandant
from apps.stammdaten.models import Artikel, ArtikelLieferant, Kunde, Lieferant, Steuersatz
from apps.verkauf.models import Beleg, Belegart, Position

from . import services
from .models import BestellPosition, BestellStatus, Bestellung

D = Decimal


@override_settings(SECURE_SSL_REDIRECT=False)
class LieferantenkopieTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.lf = Lieferant.objects.create(nummer="L1", name1="Techdata GmbH")
        st = Steuersatz.objects.create(bezeichnung="19", prozent=D("19"))
        self.maus = Artikel.objects.create(nummer="M1", bezeichnung="Maus", steuersatz=st, vk1=D("15"))
        self.tast = Artikel.objects.create(nummer="T1", bezeichnung="Tastatur", steuersatz=st, vk1=D("25"))
        for a in (self.maus, self.tast):
            ArtikelLieferant.objects.create(artikel=a, lieferant=self.lf, bestellnummer=a.nummer, ek=D("5"), bestand=10)
        self.kunde = Kunde.objects.create(nummer="1", name1="K")
        self.ab = self.beleg(Belegart.AUFTRAG)
        self.p_maus = self.pos(self.ab, self.maus, "5")
        self.p_tast = self.pos(self.ab, self.tast, "2")
        self.user = get_user_model().objects.create_user("t", password="x" * 14)

    def beleg(self, art, vorgaenger=None):
        return Beleg.objects.create(mandant=self.m, art=art, kunde=self.kunde, vorgaenger=vorgaenger)

    def pos(self, beleg, artikel, menge):
        return Position.objects.create(beleg=beleg, artikel=artikel, bezeichnung=artikel.bezeichnung, menge=D(menge), einzelpreis=D("10"))

    def kopieren(self, beleg=None, **kw):
        liste = services.bestellungen_aus_beleg(beleg or self.ab, benutzer=self.user, **kw)
        return liste

    def bestellt(self, liste):
        for b in liste:
            services.bestellen(b)

    def login(self):
        self.client.force_login(self.user)
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    # ---- Service ----------------------------------------------------------------------------------

    def test_erstes_kopieren_ohne_rueckfrage(self):
        self.assertFalse(services.rueckfrage_noetig(self.ab))
        b = self.kopieren()[0]
        self.assertEqual(sorted((p.artikel.nummer, p.menge) for p in b.positionen.all()), [("M1", D("5")), ("T1", D("2"))])
        self.assertEqual((b.aus_beleg, b.angelegt_von), (self.ab, self.user))

    def test_entwurf_zaehlt_nicht_als_bestellt(self):
        self.kopieren()
        self.assertFalse(services.rueckfrage_noetig(self.ab))
        self.assertEqual(set(services.bestellt_je_position(self.ab).values()), {D("0")})
        self.assertEqual(len(self.kopieren()), 1)  # ohne Rückfrage, wie entschieden

    def test_nach_bestellen_nur_mit_auswahl(self):
        self.bestellt(self.kopieren())
        self.assertTrue(services.rueckfrage_noetig(self.ab))
        with self.assertRaises(ValueError):
            self.kopieren()
        self.assertEqual(Bestellung.objects.count(), 1)

    def test_nur_fehlendes(self):
        self.bestellt(self.kopieren())
        self.p_maus.menge = D("8")  # Kunde will mehr
        self.p_maus.save()
        self.pos(self.ab, self.tast, "1")  # neue Position desselben Artikels
        b = self.kopieren(modus=services.MODUS_FEHLEND)[0]
        self.assertEqual(sorted((p.artikel.nummer, p.menge) for p in b.positionen.all()), [("M1", D("3")), ("T1", D("1"))])
        self.assertIn("nur fehlende Mengen", b.bemerkung)

    def test_alles_bestellt_nur_fehlendes_meldet(self):
        self.bestellt(self.kopieren())
        with self.assertRaisesMessage(ValueError, "Alle Positionen sind bereits"):
            self.kopieren(modus=services.MODUS_FEHLEND)

    def test_komplett_nur_mit_grund(self):
        self.bestellt(self.kopieren())
        with self.assertRaisesMessage(ValueError, "Grund"):
            self.kopieren(modus=services.MODUS_KOMPLETT, grund="  ")
        b = self.kopieren(modus=services.MODUS_KOMPLETT, grund="Lieferant liefert nicht")[0]
        self.assertEqual(b.positionen.count(), 2)
        self.assertIn("erneut komplett bestellt: Lieferant liefert nicht", b.bemerkung)

    def test_storno_zaehlt_nicht_bleibt_in_historie(self):
        liste = self.kopieren()
        self.bestellt(liste)
        Bestellung.objects.filter(pk=liste[0].pk).update(status=BestellStatus.STORNIERT)
        self.assertFalse(services.rueckfrage_noetig(self.ab))
        self.assertEqual(list(services.lieferantenbestellungen(self.ab)), liste)

    def test_teilweise_bestellt(self):
        b = services.bestellung_anlegen(self.m, self.lf, aus_beleg=self.ab)  # alte Bestellung ohne Positionsbezug
        services.bestellposition(b, self.maus, D("2"))
        services.bestellen(b)
        bestellt = services.bestellt_je_position(self.ab)
        self.assertEqual((bestellt[self.p_maus.pk], bestellt[self.p_tast.pk]), (D("2"), D("0")))

    def test_angebot_zaehlt_fuer_kundenbestellung_angebot_ohne_rueckfrage(self):
        an = self.beleg(Belegart.ANGEBOT)
        self.pos(an, self.maus, "5")
        self.bestellt(self.kopieren(an))
        self.assertEqual(len(self.kopieren(an)), 1)  # Angebot: keine Rückfrage
        ab = self.beleg(Belegart.AUFTRAG, vorgaenger=an)
        p = self.pos(ab, self.maus, "5")
        self.assertTrue(services.rueckfrage_noetig(ab))
        self.assertEqual(services.bestellt_je_position(ab)[p.pk], D("5"))
        self.assertEqual(len(services.lieferantenbestellungen(ab)), 2)

    def test_anderer_mandant_zaehlt_nicht(self):
        m2 = Mandant.objects.create(kurzname="ZONE", firmenname="ZONE")
        b = Bestellung.objects.create(mandant=m2, lieferant=self.lf, nummer="X1", aus_beleg=self.ab, status=BestellStatus.BESTELLT)
        BestellPosition.objects.create(bestellung=b, artikel=self.maus, bezeichnung="Maus", menge=D("5"), ek=D("5"))
        self.assertFalse(services.rueckfrage_noetig(self.ab))

    # ---- Oberfläche -------------------------------------------------------------------------------

    def test_rueckfrage_und_historie_in_der_oberflaeche(self):
        self.login()
        url = reverse("einkauf:aus_beleg", args=[self.ab.pk])
        self.assertEqual(self.client.post(url).status_code, 302)  # erstes Mal: direkt angelegt
        self.bestellt(Bestellung.objects.all())
        r = self.client.post(url)
        self.assertContains(r, "schon beim Lieferanten bestellt")
        self.assertContains(r, 'value="komplett"')
        self.assertNotContains(r, 'value="fehlend"')  # alles bestellt
        self.assertEqual(Bestellung.objects.count(), 1)
        r = self.client.post(url, {"modus": "komplett", "grund": ""}, follow=True)
        self.assertContains(r, "Grund")
        self.assertEqual(Bestellung.objects.count(), 1)
        self.client.post(url, {"modus": "komplett", "grund": "Nachbestellung"})
        self.assertEqual(Bestellung.objects.count(), 2)
        seite = self.client.get(reverse("verkauf:beleg", args=[self.ab.pk]))
        self.assertContains(seite, "Lieferantenbestellungen:")
        self.assertContains(seite, "bestellt: 5 von 5")
        for b in Bestellung.objects.all():
            self.assertContains(seite, b.nummer)
        liste = self.client.get(reverse("einkauf:bestellungen"))
        self.assertContains(liste, "aus Beleg")
