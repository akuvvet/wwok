"""Wunsch des Nutzers 19.11.2026: Eine Lieferantenbestellung ohne Position wird nicht gespeichert – wie die leeren Entwürfe
im Verkauf werden leere Entwürfe nach ein paar Minuten beim Öffnen der Listen entfernt."""
from datetime import timedelta
from decimal import Decimal as D

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.kern.models import Mandant
from apps.stammdaten.models import Artikel, Lieferant, Steuersatz
from apps.verkauf.models import Nummernkreis

from . import services
from .models import BestellStatus, Bestellung


class LeereBestellungTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.m2 = Mandant.objects.create(kurzname="ZONE", firmenname="ZONE Handel")
        self.lf = Lieferant.objects.create(nummer="L1", name1="Techdata GmbH", kurzname="TD")
        self.lf2 = Lieferant.objects.create(nummer="L2", name1="Wave AG", kurzname="WAVE")
        self.maus = Artikel.objects.create(nummer="M1", bezeichnung="Maus", steuersatz=Steuersatz.objects.get(nummer=1), vk1=D("15"))
        self.client.force_login(get_user_model().objects.create_superuser("chef", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    def alt(self, b, minuten=5):
        Bestellung.objects.filter(pk=b.pk).update(geaendert_am=timezone.now() - timedelta(minutes=minuten))

    def test_leerer_entwurf_wird_entfernt_nummer_wieder_frei(self):
        b = services.bestellung_anlegen(self.m, self.lf)
        nummer = b.nummer
        self.alt(b)
        self.assertEqual(services.leere_bestellungen_entfernen(self.m), 1)
        self.assertFalse(Bestellung.objects.filter(pk=b.pk).exists())
        self.assertEqual(services.bestellung_anlegen(self.m, self.lf).nummer, nummer)  # keine Lücke im Nummernkreis

    def test_bleibt_frisch_mit_position_bestellt_oder_fremder_mandant(self):
        frisch = services.bestellung_anlegen(self.m, self.lf)  # gerade angelegt: der Benutzer arbeitet noch daran
        mit = services.bestellung_anlegen(self.m, self.lf)
        services.bestellposition(mit, self.maus, D("1"), ek=D("5"))
        fremd = services.bestellung_anlegen(self.m2, self.lf)
        for b in (mit, fremd):
            self.alt(b)
        self.assertEqual(services.leere_bestellungen_entfernen(self.m), 0)
        self.assertEqual(Bestellung.objects.count(), 3)

    def test_letzte_position_geloescht_wird_spaeter_entfernt(self):
        b = services.bestellung_anlegen(self.m, self.lf)
        services.bestellposition(b, self.maus, D("1"), ek=D("5"))
        b.positionen.all().delete()
        self.alt(b)
        self.assertEqual(services.leere_bestellungen_entfernen(self.m), 1)

    def test_nicht_entwurf_bleibt(self):
        b = services.bestellung_anlegen(self.m, self.lf)
        Bestellung.objects.filter(pk=b.pk).update(status=BestellStatus.STORNIERT)
        self.alt(b)
        self.assertEqual(services.leere_bestellungen_entfernen(self.m), 0)

    def test_mit_wareneingang_bleibt(self):
        b = services.bestellung_anlegen(self.m, self.lf)
        services.wareneingang_anlegen(self.m, self.lf, b)
        self.alt(b)
        self.assertEqual(services.leere_bestellungen_entfernen(self.m), 0)

    def test_listen_raeumen_auf(self):
        b = services.bestellung_anlegen(self.m, self.lf)
        anderer = services.bestellung_anlegen(self.m, self.lf2)
        self.alt(b)
        self.alt(anderer)
        self.client.get(reverse("einkauf:lieferant", args=[self.lf.pk]))  # Lieferantenseite: nur dieser Lieferant
        self.assertEqual(list(Bestellung.objects.values_list("pk", flat=True)), [anderer.pk])
        r = self.client.get(reverse("einkauf:bestellungen"))
        self.assertEqual(r.status_code, 200)
        self.assertFalse(Bestellung.objects.exists())

    def test_hinweis_in_der_leeren_bestellung(self):
        b = services.bestellung_anlegen(self.m, self.lf)
        r = self.client.get(reverse("einkauf:bestellung", args=[b.pk]))
        self.assertContains(r, "Ohne Position wird die Bestellung nicht gespeichert")
        services.bestellposition(b, self.maus, D("1"), ek=D("5"))
        self.assertNotContains(self.client.get(reverse("einkauf:bestellung", args=[b.pk])), "Ohne Position wird die Bestellung nicht gespeichert")

    def test_nummernkreis_nur_freigegeben_wenn_letzte(self):
        b1 = services.bestellung_anlegen(self.m, self.lf)
        b2 = services.bestellung_anlegen(self.m, self.lf)
        services.bestellposition(b2, self.maus, D("1"), ek=D("5"))
        self.alt(b1)
        services.leere_bestellungen_entfernen(self.m)
        self.assertNotEqual(services.bestellung_anlegen(self.m, self.lf).nummer, b1.nummer)  # b2 liegt dahinter: Lücke bleibt
        self.assertTrue(Nummernkreis.objects.exists())
