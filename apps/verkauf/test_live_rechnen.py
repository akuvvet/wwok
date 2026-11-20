"""Live-Rechnen im Verkaufsbeleg (Wunsch des Nutzers 20.11.2026): Die Ausdrücke aus apps/verkauf/rechnen.py müssen genau
das ergeben, was Beleg.berechne() nach dem Speichern rechnet – auch mit mehreren Steuersätzen, Belegrabatt und Nebenkosten.
Die Ausdruckssprache (Zahlen, Namen, + - * / ( ), runde()) ist gültiges Python; der Test wertet sie daher direkt aus."""
from decimal import ROUND_HALF_UP, Decimal as D

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.kern.models import Mandant
from apps.stammdaten.models import Kunde

from .models import Beleg, Position
from .rechnen import summen_ausdruecke


def runde(x, n=2):
    return D(str(x)).quantize(D(1).scaleb(-n), rounding=ROUND_HALF_UP)


def auswerten(ausdruck, werte):
    return runde(eval(ausdruck, {"__builtins__": {}, "runde": runde}, {k: D(str(v)) for k, v in werte.items()}))


@override_settings(SECURE_SSL_REDIRECT=False)
class LiveRechnenBelegTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.k = Kunde.objects.create(nummer="K1", name1="Muster GmbH")
        self.client.force_login(get_user_model().objects.create_superuser("chef", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    def beleg(self):
        b = Beleg.objects.create(mandant=self.m, art="AN", kunde=self.k)
        Position.objects.create(beleg=b, bezeichnung="Dienst", menge=D("3"), einzelpreis=D("33.33"), steuer_prozent=D("19"))
        Position.objects.create(beleg=b, bezeichnung="Buch", menge=D("1"), einzelpreis=D("17.99"), steuer_prozent=D("7"), rabatt_prozent=D("5"))
        return b

    def test_ausdruecke_gleich_wie_berechne(self):
        b = self.beleg()
        for rabatt, versand, verpackung, versicherung in (("0", "0", "0", "0"), ("10", "5.90", "1.50", "0"), ("3.5", "0", "0", "2.33")):
            b.rabatt_prozent, b.versandkosten, b.verpackung, b.versicherung = D(rabatt), D(versand), D(verpackung), D(versicherung)
            b.save()
            b.berechne()
            a = summen_ausdruecke(b, list(b.positionen.all()))
            werte = dict(a["konstanten"], belegrabatt=rabatt, versand=versand, verpackung=verpackung, versicherung=versicherung)
            self.assertEqual((auswerten(a["netto"], werte), auswerten(a["steuer"], werte), auswerten(a["brutto"], werte)),
                             (b.summe_netto, b.summe_steuer, b.summe_brutto), (rabatt, versand))

    def test_seite_mit_live_feldern_gesperrt_ohne(self):
        b = self.beleg()
        b.berechne()
        seite = self.client.get(reverse("verkauf:beleg", args=[b.pk]))
        for text in ('data-wert="belegrabatt"', 'data-wert="versand"', 'data-wert="satz0_netto"', "data-ergibt=\"runde(", "window.wwokRechnen"):
            self.assertContains(seite, text)
        Beleg.objects.filter(pk=b.pk).update(art="RE", abgeschlossen=True)  # abgeschlossene Rechnung ist gesperrt
        self.assertIsNone(self.client.get(reverse("verkauf:beleg", args=[b.pk])).context["rechnen"])
