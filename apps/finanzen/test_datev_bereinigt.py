"""DATEV-Export (Entscheidung des Nutzers 25.11.2026): Standard nur Rechnungen/Gutschriften (der Steuerberater bucht die Bank
aus den Kontoauszügen); eingeschaltet werden Zahlung und Storno im selben Zeitraum weggelassen und Anzahlungen tragen die
Rechnungsnummer."""
from datetime import date, timedelta

from django.urls import reverse

from . import anzahlung, datev, services
from .models import Zahlart
from .test_proforma import E, ProformaBasis

HEUTE = date.today()


def buchungen(mandant, von=HEUTE - timedelta(days=40), bis=HEUTE) -> list:
    zeilen = datev.export(mandant, von, bis).decode("cp1252").splitlines()[2:]
    return [z.split(";") for z in zeilen]


class DatevBereinigtTest(ProformaBasis):
    def rechnung(self):
        ab = self.bestellung()
        re = ab.kopieren_als("RE")
        re.abschliessen()
        re.refresh_from_db()
        return re

    def test_standard_ohne_zahlungen(self):
        re = self.rechnung()
        self.zahlen(re, "238.00")
        zeilen = buchungen(self.m)
        self.assertEqual([z[10] for z in zeilen], [re.nummer])  # nur die Rechnung
        self.assertEqual(zeilen[0][7], self.m.konto_erloes_19)

    def test_zahlung_und_storno_im_zeitraum_entfallen(self):
        self.m.datev_zahlungen = True
        re = self.rechnung()
        falsch = self.zahlen(re, "45.01")
        services.zahlung_stornieren(falsch, "falsch gebucht", self.user)
        self.zahlen(re, "238.00")
        zahlungen = [z for z in buchungen(self.m) if z[6] == self.m.konto_bank]
        self.assertEqual([(z[0], z[1]) for z in zahlungen], [("238,00", "S")])

    def test_storno_in_spaeterem_zeitraum_bleibt(self):
        self.m.datev_zahlungen = True
        re = self.rechnung()
        z = self.zahlen(re, "45.01")
        services.zahlung_stornieren(z, "falsch gebucht", self.user)
        z.__class__.objects.filter(pk=z.pk).update(datum=HEUTE - timedelta(days=35))  # Original im Vormonat exportiert
        zahlungen = [(x[0], x[1]) for x in buchungen(self.m, HEUTE - timedelta(days=5), HEUTE) if x[6] == self.m.konto_bank]
        self.assertEqual(zahlungen, [("45,01", "H")])  # die Gegenbuchung muss zum Steuerberater

    def test_anzahlung_traegt_rechnungsnummer_sobald_verrechnet(self):
        self.m.datev_zahlungen = True
        _, pf = self.proforma()
        self.zahlen(pf, "238.00")
        vorher = [z for z in buchungen(self.m) if z[6] == self.m.konto_bank]
        self.assertEqual((vorher[0][10], vorher[0][11]), (pf.nummer, ""))  # noch keine Rechnung: Proformanummer
        re = anzahlung.proforma_umwandeln(pf, self.user)
        nachher = [z for z in buchungen(self.m) if z[6] == self.m.konto_bank]
        self.assertEqual((nachher[0][10], nachher[0][11]), (re.nummer, pf.nummer))

    def test_seite_warnt_ohne_berater_und_nennt_standard(self):
        seite = self.client.get(reverse("finanzen:datev"))
        self.assertContains(seite, "Beraternummer oder Mandantennummer fehlt")
        self.assertContains(seite, "Zahlungen sind nicht enthalten")
        self.m.datev_berater, self.m.datev_mandant, self.m.datev_zahlungen = "12345", "1", True
        self.m.save()
        seite = self.client.get(reverse("finanzen:datev"))
        self.assertNotContains(seite, "Beraternummer oder Mandantennummer fehlt")
        self.assertContains(seite, "Zahlungen sind eingeschaltet")
