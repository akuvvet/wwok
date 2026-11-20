"""Wunsch des Nutzers 20.11.2026: Ansprechpartner im Kopf der Lieferantenseite auswählbar, mit Anrede, Name, Telefon, E-Mail;
ohne Auswahl der Hauptkontakt, sonst der erste. Ansprechpartner von Lieferanten gelten je Mandant."""
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.kern.models import Mandant
from apps.stammdaten.models import Anrede, Ansprechpartner, Lieferant


@override_settings(SECURE_SSL_REDIRECT=False)
class LieferantAnsprechpartnerTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.m2 = Mandant.objects.create(kurzname="ZONE", firmenname="ZONE Handel")
        self.lf = Lieferant.objects.create(nummer="L1", name1="Kosatec Computer GmbH", kurzname="KOS")
        self.client.force_login(get_user_model().objects.create_superuser("chef", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()
        self.url = reverse("einkauf:lieferant", args=[self.lf.pk])

    def ap(self, nachname, mandant=None, haupt=False, **felder):
        return Ansprechpartner.objects.create(lieferant=self.lf, mandant=mandant or self.m, nachname=nachname, hauptkontakt=haupt, **felder)

    def test_hauptkontakt_zuerst_mit_daten_und_auswahl(self):
        frau = Anrede.objects.filter(bezeichnung="Frau").first() or Anrede.objects.create(nummer=93, bezeichnung="Frau")
        self.ap("Albers", vorname="Axel", telefon="0212 1")
        haupt = self.ap("Zeller", haupt=True, vorname="Petra", anrede=frau, telefon="0212 2", durchwahl="30",
                        email="petra@example.com", mobil="0170 1")
        seite = self.client.get(self.url)
        aps = seite.context["ansprechpartner"]
        self.assertEqual(aps[0], haupt)  # ohne Auswahl: Hauptkontakt
        html = seite.content.decode()
        self.assertIn('aria-label="Ansprechpartner wählen"', html)
        self.assertIn(f'<option value="{haupt.pk}" selected>Petra Zeller ★</option>', html)
        for text in ("Frau Petra Zeller", "0212 2-30", "mailto:petra@example.com", "Mobil", "0212 1"):
            self.assertIn(text, html)
        self.assertIn(f'data-ap="{aps[1].pk}" hidden', html)  # der zweite ist nur per Auswahl sichtbar

    def test_ohne_hauptkontakt_der_erste_und_ohne_auswahl_bei_einem(self):
        allein = self.ap("Berger", vorname="Bernd")
        html = self.client.get(self.url).content.decode()
        self.assertNotIn("Ansprechpartner wählen", html)  # nur einer: keine Auswahl nötig
        self.assertIn(f'data-ap="{allein.pk}">', html)
        self.assertIn("E-Mail –", html)

    def test_kunde_genauso(self):
        from apps.stammdaten.models import Kunde
        k = Kunde.objects.create(nummer="K1", name1="Muster GmbH")
        erster = Ansprechpartner.objects.create(kunde=k, nachname="Albers", vorname="Axel", telefon="0212 1")
        haupt = Ansprechpartner.objects.create(kunde=k, nachname="Zeller", vorname="Petra", email="p@example.com", hauptkontakt=True)
        seite = self.client.get(reverse("verkauf:kunde", args=[k.pk]))
        self.assertEqual(seite.context["ansprechpartner"], [haupt, erster])
        html = seite.content.decode()
        self.assertIn('aria-label="Ansprechpartner wählen"', html)
        self.assertIn('data-ap-schluessel="kunde-', html)
        self.assertIn("mailto:p@example.com", html)
        self.assertIn(f'data-ap="{erster.pk}" hidden', html)

    def test_beleg_erstellen_uebernimmt_gewaehlten_ansprechpartner(self):
        from apps.stammdaten.models import Kunde
        from apps.verkauf.models import Beleg
        k = Kunde.objects.create(nummer="K2", name1="Beispiel AG")
        a = Ansprechpartner.objects.create(kunde=k, nachname="Albers")
        z = Ansprechpartner.objects.create(kunde=k, nachname="Zeller")  # kein Hauptkontakt: der erste (Albers) ist Vorgabe
        fremd = Ansprechpartner.objects.create(kunde=Kunde.objects.create(nummer="K3", name1="Andere"), nachname="Fremd")
        html = self.client.get(reverse("verkauf:kunde", args=[k.pk])).content.decode()
        self.assertIn('name="ansprechpartner" form="beleg-neu"', html)
        self.assertIn('id="beleg-neu"', html)
        url = reverse("verkauf:beleg_neu", args=[k.pk])
        self.client.post(url, {"art": "AN", "ansprechpartner": z.pk})
        self.client.post(url, {"art": "AN"})
        self.client.post(url, {"art": "AN", "ansprechpartner": fremd.pk})  # fremder Ansprechpartner: Vorgabe statt fremd
        self.assertEqual([b.ansprechpartner for b in Beleg.objects.filter(kunde=k).order_by("pk")], [z, a, a])

    def test_nur_ansprechpartner_des_mandanten(self):
        eigener = self.ap("Eigen")
        self.ap("Fremd", mandant=self.m2)
        self.assertEqual(self.client.get(self.url).context["ansprechpartner"], [eigener])
