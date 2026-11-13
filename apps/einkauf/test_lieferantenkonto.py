"""Einkauf wie Verkauf: Menü „Lieferant“, Suche wie bei Kunden, offene Belege, Lieferantenkonto."""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.kern.models import Mandant
from apps.stammdaten.models import Ansprechpartner, Artikel, ArtikelLieferant, Lieferant, LieferantMandant, Steuersatz

from . import lieferantenkonto, services
from .models import Lieferantenrechnung, RechnungStatus


@override_settings(SECURE_SSL_REDIRECT=False)
class LieferantenkontoTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.m2 = Mandant.objects.create(kurzname="ZONE", firmenname="ZONE Handel")
        self.kos = Lieferant.objects.create(nummer="L1", name1="Kosatec Computer GmbH", kurzname="KOSATEC", ort="Braunschweig")
        self.api = Lieferant.objects.create(nummer="L2", name1="API GmbH", kurzname="API", ort="Baesweiler")
        st = Steuersatz.objects.create(bezeichnung="19", prozent=Decimal("19"))
        self.maus = Artikel.objects.create(nummer="M1", bezeichnung="Maus", steuersatz=st)
        ArtikelLieferant.objects.create(artikel=self.maus, lieferant=self.kos, bestellnummer="K-1", ek=Decimal("9"))
        user = get_user_model().objects.create_user("t", password="x" * 14)
        self.client.force_login(user)
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    def bestellung(self, lieferant, mandant=None, bestellen=True):
        b = services.bestellung_anlegen(mandant or self.m, lieferant)
        services.bestellposition(b, self.maus, Decimal("2"))
        if bestellen:
            services.bestellen(b)
        return b

    def rechnung(self, lieferant, nr, status=RechnungStatus.OFFEN, mandant=None):
        heute = timezone.localdate()
        return Lieferantenrechnung.objects.create(mandant=mandant or self.m, lieferant=lieferant, rechnungsnummer=nr, datum=heute,
                                                  faellig=heute + timedelta(days=14), netto=Decimal("100"), brutto=Decimal("119"),
                                                  status=status)

    # Menü
    def test_menue_lieferant_oben_unter_einkauf(self):
        html = self.client.get(reverse("einkauf:bestellungen")).content.decode()
        einkauf = html[html.index('<summary title="Einkauf">'):]
        einkauf = einkauf[:einkauf.index("</details>")]
        links = [teil.split('title="')[1].split('"')[0] for teil in einkauf.split("<a class=")[1:]]
        self.assertEqual(links[0], "Lieferant")
        self.assertNotIn("Lieferanten", links)  # alter Eintrag unten ist weg
        seite = self.client.get(reverse("einkauf:lieferant", args=[self.kos.pk])).content.decode()
        self.assertIn('aria-current="page" href="' + reverse("einkauf:lieferanten") + '" title="Lieferant"', seite)

    # Startseite „Lieferant“
    def test_ohne_suche_alle_offenen_belege(self):
        b_offen = self.bestellung(self.kos)
        entwurf = self.bestellung(self.api, bestellen=False)
        b_fertig = self.bestellung(self.kos)
        we = services.wareneingang_anlegen(self.m, self.kos, b_fertig)
        services.wareneingang_aus_bestellung(we)
        services.wareneingang_buchen(we)  # b_fertig: geliefert, WE gebucht → nicht offen
        we_offen = services.wareneingang_anlegen(self.m, self.api)
        r_offen = self.rechnung(self.kos, "R-OFFEN")
        self.rechnung(self.kos, "R-BEZAHLT", RechnungStatus.BEZAHLT)
        rma = services.rma_anlegen(self.m, self.api, "Defekte Maus", "geht nicht")
        fremd = self.bestellung(self.kos, mandant=self.m2)
        seite = self.client.get(reverse("einkauf:lieferanten"))
        nummern = {z["nummer"] for z in seite.context["offene"]}
        self.assertEqual(nummern, {b_offen.nummer, entwurf.nummer, we_offen.nummer, "R-OFFEN", rma.nummer})
        self.assertNotIn(("BE", fremd.pk), {(z["art"], z["pk"]) for z in seite.context["offene"]})  # anderer Mandant (gleiche Nummer!)
        self.assertEqual(len(seite.context["offene"]), 5)
        self.assertContains(seite, "Offene Belege zum Bearbeiten")
        self.assertContains(seite, reverse("einkauf:rechnung", args=[r_offen.pk]))
        self.assertContains(seite, reverse("einkauf:lieferant", args=[self.kos.pk]))  # Lieferant in der Liste anklickbar

    def test_suche_wie_kundensuche(self):
        Lieferant.objects.create(nummer="L3", name1="Kosmos Handel", ort="Köln")
        Ansprechpartner.objects.create(lieferant=self.api, vorname="Petra", nachname="Einkauf")
        url = reverse("einkauf:lieferanten")
        self.assertRedirects(self.client.get(url, {"q": "API"}), reverse("einkauf:lieferant", args=[self.api.pk]))  # ein Treffer
        self.assertRedirects(self.client.get(url, {"q": "petra"}), reverse("einkauf:lieferant", args=[self.api.pk]))  # Ansprechpartner
        self.assertRedirects(self.client.get(url, {"q": "computer braunschweig"}), reverse("einkauf:lieferant", args=[self.kos.pk]))
        mehrere = self.client.get(url, {"q": "Kos*"})
        self.assertEqual({l.nummer for l in mehrere.context["lieferanten"]}, {"L1", "L3"})
        self.assertIsNone(mehrere.context["offene"])  # bei Suche keine Belegliste
        self.assertEqual(self.client.get(url, {"q": "*"}).context["lieferanten"].paginator.count, 3)
        self.assertContains(self.client.get(url, {"q": "gibtsnicht"}), "Filter zurücksetzen")
        self.assertEqual(lieferantenkonto.lieferanten_suchen("tec*").count(), 0)  # Stern = Wortanfang

    # Lieferantenkonto
    def test_lieferant_zeigt_offene_belege_und_kopf(self):
        LieferantMandant.objects.create(lieferant=self.kos, mandant=self.m, unsere_kundennummer="K-4711")
        b = self.bestellung(self.kos)
        self.bestellung(self.api)  # anderer Lieferant
        self.rechnung(self.kos, "R-1")
        self.rechnung(self.kos, "R-2", RechnungStatus.BEZAHLT)
        url = reverse("einkauf:lieferant", args=[self.kos.pk])
        seite = self.client.get(url)
        self.assertEqual({z["nummer"] for z in seite.context["seite"].object_list}, {b.nummer, "R-1"})  # Standard: nur offene
        self.assertEqual(seite.context["filter"], "offen")
        self.assertEqual(seite.context["offen_rechnungen"], Decimal("119"))
        for text in ("L1 – Kosatec Computer GmbH", "K-4711", "+ Bestellung", "+ Wareneingang", f"zu {b.nummer}", "data-livefilter"):
            self.assertContains(seite, text)
        alle = self.client.get(url, {"art": "alle"})
        self.assertEqual({z["nummer"] for z in alle.context["seite"].object_list}, {b.nummer, "R-1", "R-2"})
        nur_lr = self.client.get(url, {"art": "LR"})
        self.assertEqual({z["nummer"] for z in nur_lr.context["seite"].object_list}, {"R-1", "R-2"})
        morgen = (timezone.localdate() + timedelta(days=1)).isoformat()
        self.assertEqual(len(self.client.get(url, {"art": "alle", "ab": morgen}).context["seite"].object_list), 0)
        self.assertEqual(self.client.get(url, {"ab": "kein-datum"}).status_code, 200)

    def test_bestellung_und_wareneingang_vom_lieferanten_aus(self):
        url = reverse("einkauf:lieferant", args=[self.kos.pk])
        r = self.client.post(reverse("einkauf:bestellung_neu"), {"lieferant": self.kos.pk})
        self.assertEqual(r.status_code, 302)
        b = self.kos.bestellungen.get()
        services.bestellposition(b, self.maus, Decimal("1"))
        services.bestellen(b)
        self.client.post(reverse("einkauf:wareneingang_neu"), {"lieferant": self.kos.pk, "bestellung": b.pk})
        we = self.kos.wareneingaenge.get()
        self.assertEqual(we.bestellung, b)
        self.assertEqual(we.positionen.count(), 1)
        self.assertContains(self.client.get(url), we.nummer)

    def test_lieferantenname_in_belegen_verlinkt(self):
        b = self.bestellung(self.kos)
        we = services.wareneingang_anlegen(self.m, self.kos, b)
        r = self.rechnung(self.kos, "R-LINK")
        rma = services.rma_anlegen(self.m, self.kos, "Defekte Maus", "geht nicht")
        link = 'href="' + reverse("einkauf:lieferant", args=[self.kos.pk]) + '"'
        for name, args in (("bestellungen", []), ("bestellung", [b.pk]), ("wareneingaenge", []), ("wareneingang", [we.pk]),
                           ("rechnungen", []), ("rechnung", [r.pk]), ("rma_liste", []), ("rma", [rma.pk])):
            with self.subTest(seite=name):
                self.assertContains(self.client.get(reverse("einkauf:" + name, args=args)), link)

    def test_gesperrt_ohne_neue_belege(self):
        self.kos.gesperrt = True
        self.kos.save()
        seite = self.client.get(reverse("einkauf:lieferant", args=[self.kos.pk]))
        self.assertContains(seite, "Lieferant ist gesperrt")
        self.assertNotContains(seite, "+ Bestellung")
