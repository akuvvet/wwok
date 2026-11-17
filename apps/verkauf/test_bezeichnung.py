"""Eigene Bezeichnung einer Position: für alle Typen (A, L, G) eingebbar, wird gedruckt und in alle Folgebelege übernommen."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.kern.models import Mandant
from apps.lager.models import Bewegungsart
from apps.lager.services import buche_manuell
from apps.stammdaten.models import Artikel, GruppenKomponente, Kunde, Steuersatz

from .models import Beleg, Belegart, Position


@override_settings(SECURE_SSL_REDIRECT=False)
class BezeichnungTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.kunde = Kunde.objects.create(nummer="1", name1="Kunde GmbH")
        st = Steuersatz.objects.create(bezeichnung="19", prozent=Decimal("19"))
        self.a = Artikel.objects.create(nummer="A1", bezeichnung="Notebook XY-123 i5 16GB", steuersatz=st, vk1=Decimal("500"))
        self.l = Artikel.objects.create(nummer="L1", bezeichnung="Einrichtung Windows", typ="L", steuersatz=st, vk1=Decimal("50"))
        self.g = Artikel.objects.create(nummer="G1", bezeichnung="Paket XY-123 mit Einrichtung", typ="G", steuersatz=st, vk1=Decimal("540"))
        GruppenKomponente.objects.create(gruppe=self.g, komponente=self.a, menge=Decimal("1"))
        buche_manuell(self.m, self.a, Bewegungsart.WARENEINGANG, Decimal("20"))
        self.client.force_login(get_user_model().objects.create_user("t", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()
        self.angebot = Beleg.objects.create(mandant=self.m, art=Belegart.ANGEBOT, kunde=self.kunde)

    def neu(self, **daten):
        self.client.post(reverse("verkauf:position_neu", args=[self.angebot.pk]), {"menge": "1", **daten})

    def test_eingabe_beim_uebernehmen_gilt_fuer_a_l_und_g(self):
        self.neu(typ="A", artikel="A1", bezeichnung="Business-Notebook")
        self.neu(typ="L", artikel="L1", bezeichnung="Inbetriebnahme")
        self.neu(typ="G", artikel="G1", bezeichnung="Komplettpaket Büro")
        koepfe = {p.typ: p.bezeichnung for p in self.angebot.positionen.filter(gruppe__isnull=True)}
        self.assertEqual(koepfe, {"A": "Business-Notebook", "L": "Inbetriebnahme", "G": "Komplettpaket Büro"})
        self.neu(typ="A", artikel="A1")  # ohne Eingabe: Bezeichnung des Artikels
        self.assertTrue(self.angebot.positionen.filter(bezeichnung="Notebook XY-123 i5 16GB", gruppe__isnull=True).exists())
        self.assertContains(self.client.get(reverse("verkauf:beleg", args=[self.angebot.pk])), "Bezeichnung (leer = wie Artikel)")

    def test_gruppe_und_unterposition_nachtraeglich_aenderbar(self):
        self.neu(typ="G", artikel="G1")
        kopf = self.angebot.positionen.get(typ="G", gruppe__isnull=True)
        teil = self.angebot.positionen.get(gruppe=kopf)
        for p, text in ((kopf, "Komplettpaket"), (teil, "Notebook")):
            self.client.post(reverse("verkauf:position_aendern", args=[self.angebot.pk, p.pk]),
                             {"menge": str(p.menge.normalize()), "preis": str(p.einzelpreis), "rabatt": "0", "bezeichnung": text})
            p.refresh_from_db()
            self.assertEqual(p.bezeichnung, text)

    def test_text_wandert_in_alle_folgebelege_und_ins_pdf(self):
        from .pdf import beleg_pdf
        Position.objects.create(beleg=self.angebot, artikel=self.a, typ="A", bezeichnung="Business-Notebook", einzelpreis=Decimal("500"))
        Position.objects.create(beleg=self.angebot, artikel=self.l, typ="L", bezeichnung="Inbetriebnahme", einzelpreis=Decimal("50"))
        self.angebot.abschliessen()
        erwartet = ["Business-Notebook", "Inbetriebnahme"]
        auftrag = self.angebot.kopieren_als(Belegart.AUFTRAG)
        self.assertEqual(sorted(auftrag.positionen.values_list("bezeichnung", flat=True)), erwartet)
        auftrag.abschliessen()
        rechnung = auftrag.folgebeleg_erzeugen(art=Belegart.RECHNUNG)
        self.assertEqual(sorted(rechnung.positionen.values_list("bezeichnung", flat=True)), erwartet)
        rechnung.abschliessen()
        gutschrift = rechnung.gutschrift_erzeugen()
        self.assertEqual(sorted(gutschrift.positionen.values_list("bezeichnung", flat=True)), erwartet)
        self.assertTrue(beleg_pdf(rechnung).startswith(b"%PDF"))
        self.a.refresh_from_db()
        self.assertEqual(self.a.bezeichnung, "Notebook XY-123 i5 16GB")  # der Artikel bleibt unverändert
