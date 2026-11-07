"""Angebot: nach einem G-Artikel landen die folgenden Artikel standardmäßig in dieser Gruppe."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.kern.models import Mandant
from apps.stammdaten.models import Artikel, GruppenKomponente, Kunde, Steuersatz

from .models import Beleg, Belegart


@override_settings(SECURE_SSL_REDIRECT=False)
class GruppeVorgabeTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.kunde = Kunde.objects.create(nummer="1", name1="K", preisstufe=1)
        st = Steuersatz.objects.create(bezeichnung="19", prozent=Decimal("19"))
        self.cpu = Artikel.objects.create(nummer="CPU", bezeichnung="Prozessor", steuersatz=st, vk1=Decimal("100"))
        self.ram = Artikel.objects.create(nummer="RAM", bezeichnung="Speicher", steuersatz=st, vk1=Decimal("40"))
        self.pc = Artikel.objects.create(nummer="PC1", typ="G", bezeichnung="Büro-PC", steuersatz=st)
        self.nb = Artikel.objects.create(nummer="NB1", typ="G", bezeichnung="Notebook-Set", steuersatz=st)
        GruppenKomponente.objects.create(gruppe=self.pc, komponente=self.cpu, menge=Decimal("1"))
        GruppenKomponente.objects.create(gruppe=self.nb, komponente=self.ram, menge=Decimal("1"))
        self.client.force_login(get_user_model().objects.create_user("t", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    def beleg(self, art=Belegart.ANGEBOT):
        self.b = Beleg.objects.create(mandant=self.m, art=art, kunde=self.kunde)
        return self.b

    def seite(self):
        return self.client.get(reverse("verkauf:beleg", args=[self.b.pk]))

    def post(self, **daten):
        return self.client.post(reverse("verkauf:position_neu", args=[self.b.pk]), daten)

    def kopf(self, artikel):
        return self.b.positionen.get(artikel=artikel, gruppe__isnull=True)

    def test_nach_g_artikel_ist_die_gruppe_vorausgewaehlt(self):
        self.beleg()
        self.assertNotContains(self.seite(), 'name="gruppe_auto"')  # noch keine Gruppe
        self.post(artikel="PC1")
        kopf = self.kopf(self.pc)
        r = self.seite()
        self.assertContains(r, f'<option value="{kopf.pk}" selected>')
        self.assertContains(r, f'name="gruppe_auto" id="gruppe_auto" value="1" data-vorgabe="{kopf.pk}"')

    def test_folgeartikel_landen_in_der_gruppe(self):
        self.beleg()
        self.post(artikel="PC1")
        kopf = self.kopf(self.pc)
        self.post(artikel="RAM", menge="2", gruppe=str(kopf.pk), gruppe_auto="1")  # Vorbelegung so abgeschickt
        ram = self.b.positionen.get(artikel=self.ram)
        self.assertEqual(ram.gruppe, kopf)

    def test_abgewaehlt_gilt_nur_fuer_diese_position(self):
        self.beleg()
        self.post(artikel="PC1")
        kopf = self.kopf(self.pc)
        self.post(artikel="RAM", gruppe="", gruppe_auto="0")  # für diese Position abgewählt
        self.assertIsNone(self.b.positionen.get(artikel=self.ram).gruppe)
        self.assertContains(self.seite(), f'<option value="{kopf.pk}" selected>')  # nächste Position wieder vorbelegt

    def test_neuer_g_artikel_wird_eigene_gruppe_und_neue_vorgabe(self):
        self.beleg()
        self.post(artikel="PC1")
        pc = self.kopf(self.pc)
        self.post(artikel="NB1", typ="A", gruppe=str(pc.pk), gruppe_auto="1")  # Nummer getippt, Typ-Feld nicht umgestellt
        nb = self.kopf(self.nb)
        self.assertEqual(nb.unterpositionen.count(), 1)  # mit eigenen Komponenten, nicht in den PC einsortiert
        self.assertEqual(pc.unterpositionen.count(), 1)
        self.assertContains(self.seite(), f'<option value="{nb.pk}" selected>')  # die jüngste Gruppe gilt

    def test_freie_gruppe_und_freie_textposition_bleiben_ohne_vorgabe(self):
        self.beleg()
        self.post(artikel="PC1")
        pc = self.kopf(self.pc)
        self.post(typ="G", bezeichnung="Netzwerk", preis="50", gruppe=str(pc.pk), gruppe_auto="1")
        self.assertIsNone(self.b.positionen.get(bezeichnung="Netzwerk").gruppe)
        self.post(typ="A", bezeichnung="Anfahrt", preis="30", gruppe=str(pc.pk), gruppe_auto="1")
        self.assertIsNone(self.b.positionen.get(bezeichnung="Anfahrt").gruppe)

    def test_selbst_gewaehlte_gruppe_bleibt_wie_bisher(self):
        self.beleg()
        self.post(typ="G", bezeichnung="Netzwerk", preis="50")
        kopf = self.b.positionen.get(bezeichnung="Netzwerk")
        r = self.post(bezeichnung="Anfahrt", preis="30", gruppe=str(kopf.pk), gruppe_auto="0")
        self.assertFalse(self.b.positionen.filter(bezeichnung="Anfahrt").exists())  # freie Unterposition weiter abgelehnt
        self.assertEqual(r.status_code, 302)

    def test_andere_belegarten_ohne_vorbelegung(self):
        self.beleg(Belegart.AUFTRAG)
        self.post(artikel="PC1")
        html = self.seite().content.decode()
        formular = html[html.index('id="positionsform"'):]
        formular = formular[:formular.index("</form>")]
        self.assertIn('name="gruppe"', formular)
        self.assertNotIn("selected", formular.split('name="typ"')[1].split("</select>", 1)[1])  # „zu Gruppe“ steht auf „–“
        self.assertNotIn('name="gruppe_auto"', formular)

    def test_anderer_steuersatz_landet_ohne_gruppe_statt_fehler(self):
        buch = Artikel.objects.create(nummer="BUCH", bezeichnung="Handbuch", vk1=Decimal("20"),
                                      steuersatz=Steuersatz.objects.create(bezeichnung="7", prozent=Decimal("7")))
        self.beleg()
        self.post(artikel="PC1")
        pc = self.kopf(self.pc)
        r = self.client.post(reverse("verkauf:position_neu", args=[self.b.pk]),
                             {"artikel": "BUCH", "gruppe": str(pc.pk), "gruppe_auto": "1"}, follow=True)
        self.assertIsNone(self.b.positionen.get(artikel=buch).gruppe)
        self.assertContains(r, "anderer Steuersatz")
        self.post(artikel="BUCH", gruppe=str(pc.pk), gruppe_auto="0")  # ausdrücklich gewählt: weiter abgelehnt
        self.assertEqual(self.b.positionen.filter(artikel=buch).count(), 1)

    def test_rabatt_in_gruppe_wird_nicht_still_verworfen(self):
        self.beleg()
        self.post(artikel="PC1")
        pc = self.kopf(self.pc)
        r = self.client.post(reverse("verkauf:position_neu", args=[self.b.pk]),
                             {"artikel": "RAM", "rabatt": "10", "gruppe": str(pc.pk), "gruppe_auto": "1"}, follow=True)
        self.assertContains(r, "keinen eigenen Rabatt")
        self.assertFalse(self.b.positionen.filter(artikel=self.ram).exists())
        self.post(artikel="RAM", rabatt="10", gruppe="", gruppe_auto="0")  # abgewählt: normale Position mit Rabatt
        self.assertEqual(self.b.positionen.get(artikel=self.ram).rabatt_prozent, Decimal("10.00"))
