"""Prüfbericht C, ESD: Rückgabe eines Keys wirkt nur einmal, auch wenn ein zweiter Tab mit veraltetem Stand sendet."""
from decimal import Decimal

from django.test import TestCase

from apps.kern.models import Mandant
from apps.lager.models import Bewegungsart
from apps.lager.services import buche_manuell
from apps.stammdaten.models import Artikel, Kunde, Steuersatz
from apps.verkauf.models import Beleg, Belegart, Position

from . import services
from .models import ESDKey, ESDVergabe, VergabeStatus


class RueckgabeDoppeltTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        kunde = Kunde.objects.create(nummer="1", name1="Kunde")
        st = Steuersatz.objects.create(bezeichnung="19", prozent=Decimal("19"))
        win = Artikel.objects.create(nummer="WIN11", bezeichnung="Windows", steuersatz=st, vk1=Decimal("120"), esd_key=True)
        buche_manuell(self.m, win, Bewegungsart.WARENEINGANG, Decimal("5"))
        services.importieren(self.m, win, "KEY-A")
        b = Beleg.objects.create(mandant=self.m, art=Belegart.RECHNUNG, kunde=kunde)
        Position.objects.create(beleg=b, artikel=win, bezeichnung="Windows", menge=Decimal("1"), einzelpreis=Decimal("120"))
        b.abschliessen()
        self.vergabe = b.esd_vergaben.get()

    def test_zweiter_tab_mit_altem_stand_zaehlt_nicht_doppelt(self):
        tab1 = ESDVergabe.objects.get(pk=self.vergabe.pk)
        tab2 = ESDVergabe.objects.get(pk=self.vergabe.pk)  # beide haben „aktiv“ geladen
        self.assertEqual(ESDKey.objects.get().vergeben, 1)
        services.rueckgabe(tab1, "freigeben")
        self.assertEqual(ESDKey.objects.get().vergeben, 0)
        with self.assertRaises(ValueError):
            services.rueckgabe(tab2, "sperren")  # Objekt zeigt noch AKTIV, in der Datenbank ist es zurückgenommen
        key = ESDKey.objects.get()
        self.assertEqual(key.vergeben, 0)
        self.assertNotEqual(key.status, "gesperrt")  # die zweite Entscheidung darf nichts mehr ändern
        self.assertEqual(ESDVergabe.objects.get(pk=self.vergabe.pk).status, VergabeStatus.FREIGEGEBEN)
