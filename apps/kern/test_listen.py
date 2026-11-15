"""Flexible Listen: Ansicht je Benutzer speichern, lesen, zurücksetzen; Admin setzt für Benutzer zurück."""
import json

from django.contrib.admin.sites import site
from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from apps.kern.listen import bereinige
from apps.kern.models import Listenansicht, Mandant

SCHLUESSEL = "/verkauf/kunde/#/|1|abc"
EINSTELLUNG = {"dichte": "kompakt", "spalten": [{"i": 1, "t": "Name", "v": True, "b": 120}, {"i": 0, "t": "Nr", "v": False, "b": None}]}


def _senden(client, daten, **extra):
    return client.post(reverse("listenansicht"), data=json.dumps(daten), content_type="application/json", **extra)


@override_settings(SECURE_SSL_REDIRECT=False)
class ListenansichtTest(TestCase):
    def setUp(self):
        Benutzer = get_user_model()
        self.anna = Benutzer.objects.create_user("anna", password="x" * 14)
        self.ben = Benutzer.objects.create_user("ben", password="x" * 14)
        self.client.force_login(self.anna)

    def test_speichern_und_lesen(self):
        self.assertEqual(_senden(self.client, {"schluessel": SCHLUESSEL, "einstellung": EINSTELLUNG}).status_code, 200)
        antwort = self.client.get(reverse("listenansicht"), {"schluessel": SCHLUESSEL})
        self.assertEqual(antwort.json()["einstellung"]["dichte"], "kompakt")
        self.assertEqual(antwort.json()["einstellung"]["spalten"][0]["b"], 120)
        # nochmal speichern überschreibt, legt keine zweite Zeile an
        _senden(self.client, {"schluessel": SCHLUESSEL, "einstellung": {"dichte": "normal", "spalten": []}})
        self.assertEqual(Listenansicht.objects.filter(benutzer=self.anna).count(), 1)

    def test_ungespeichert_liefert_leer(self):
        self.assertEqual(self.client.get(reverse("listenansicht"), {"schluessel": "/x/|1|y"}).json(), {"einstellung": {}})

    def test_seitenabruf_liefert_alle_tabellen_der_seite(self):
        _senden(self.client, {"schluessel": "/verkauf/kunde/#/|1|a", "einstellung": EINSTELLUNG})
        _senden(self.client, {"schluessel": "/verkauf/kunde/#/|2|b", "einstellung": EINSTELLUNG})
        _senden(self.client, {"schluessel": "/verkauf/rechnung/|1|c", "einstellung": EINSTELLUNG})
        antwort = self.client.get(reverse("listenansicht"), {"seite": "/verkauf/kunde/#/"})
        self.assertEqual(sorted(antwort.json()["ansichten"]), ["/verkauf/kunde/#/|1|a", "/verkauf/kunde/#/|2|b"])

    def test_formularpost_wird_akzeptiert(self):
        antwort = self.client.post(reverse("listenansicht"), {"schluessel": SCHLUESSEL, "einstellung": json.dumps(EINSTELLUNG)})
        self.assertEqual(antwort.status_code, 200)
        self.assertTrue(Listenansicht.objects.filter(benutzer=self.anna, schluessel=SCHLUESSEL).exists())

    def test_benutzer_sind_getrennt(self):
        _senden(self.client, {"schluessel": SCHLUESSEL, "einstellung": EINSTELLUNG})
        fremd = Client()
        fremd.force_login(self.ben)
        self.assertEqual(fremd.get(reverse("listenansicht"), {"schluessel": SCHLUESSEL}).json(), {"einstellung": {}})
        self.assertEqual(fremd.get(reverse("listenansicht"), {"seite": "/verkauf/kunde/#/"}).json(), {"ansichten": {}})

    def test_zuruecksetzen_loescht_nur_die_eigene_ansicht(self):
        _senden(self.client, {"schluessel": SCHLUESSEL, "einstellung": EINSTELLUNG})
        Listenansicht.objects.create(benutzer=self.ben, schluessel=SCHLUESSEL, einstellung=EINSTELLUNG)
        self.assertEqual(_senden(self.client, {"schluessel": SCHLUESSEL, "zuruecksetzen": True}).status_code, 200)
        self.assertFalse(Listenansicht.objects.filter(benutzer=self.anna).exists())
        self.assertTrue(Listenansicht.objects.filter(benutzer=self.ben).exists())

    def test_ohne_anmeldung_kein_zugriff(self):
        anonym = Client()
        self.assertEqual(anonym.get(reverse("listenansicht"), {"schluessel": SCHLUESSEL}).status_code, 302)
        self.assertEqual(_senden(anonym, {"schluessel": SCHLUESSEL, "einstellung": EINSTELLUNG}).status_code, 302)
        self.assertFalse(Listenansicht.objects.exists())

    def test_csrf_wird_geprueft(self):
        streng = Client(enforce_csrf_checks=True)
        streng.force_login(self.anna)
        self.assertEqual(_senden(streng, {"schluessel": SCHLUESSEL, "einstellung": EINSTELLUNG}).status_code, 403)

    def test_zu_grosse_eingabe_wird_abgelehnt(self):
        zu_viele = {"dichte": "normal", "spalten": [{"i": i, "t": "x" * 80, "v": True, "b": None} for i in range(81)]}
        self.assertEqual(_senden(self.client, {"schluessel": SCHLUESSEL, "einstellung": zu_viele}).status_code, 400)
        self.assertEqual(_senden(self.client, {"schluessel": "x" * 201, "einstellung": EINSTELLUNG}).status_code, 400)
        riesig = {"schluessel": SCHLUESSEL, "einstellung": EINSTELLUNG, "mist": "x" * 20000}
        self.assertEqual(_senden(self.client, riesig).status_code, 400)
        self.assertFalse(Listenansicht.objects.exists())

    def test_ungueltige_eingabe_wird_abgelehnt(self):
        schlecht = [
            "kein objekt",
            {"dichte": "riesig", "spalten": []},
            {"spalten": "x"},
            {"spalten": [{"i": -1}]},
            {"spalten": [{"i": 1}, {"i": 1}]},
            {"spalten": [{"i": 1, "v": "ja"}]},
            {"spalten": [{"i": 1, "b": 5}]},
            {"spalten": [{"i": True}]},
        ]
        for einstellung in schlecht:
            with self.subTest(einstellung=einstellung):
                self.assertEqual(_senden(self.client, {"schluessel": SCHLUESSEL, "einstellung": einstellung}).status_code, 400)
        antwort = self.client.post(reverse("listenansicht"), data="{kaputt", content_type="application/json")
        self.assertEqual(antwort.status_code, 400)
        self.assertEqual(self.client.get(reverse("listenansicht")).status_code, 400)
        self.assertFalse(Listenansicht.objects.exists())

    def test_nur_erwartete_felder_werden_gespeichert(self):
        sauber = bereinige({"dichte": "normal", "boese": 1, "spalten": [{"i": 0, "x": "<script>"}]})
        self.assertEqual(sauber, {"dichte": "normal", "spalten": [{"i": 0, "t": "", "v": True, "b": None}]})

    def test_begrenzung_der_ansichten_je_benutzer(self):
        from apps.kern import listen
        Listenansicht.objects.bulk_create(
            [Listenansicht(benutzer=self.anna, schluessel=f"/s{i}/|1|a") for i in range(listen.ANSICHTEN_JE_BENUTZER_MAX)]
        )
        self.assertEqual(_senden(self.client, {"schluessel": SCHLUESSEL, "einstellung": EINSTELLUNG}).status_code, 400)

    def test_skript_ist_auf_einer_listenseite_eingebunden(self):
        Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        html = self.client.get(reverse("dashboard"), follow=True).content.decode()
        self.assertIn("Flexible Listen: Spalten ein-/ausblenden", html)
        self.assertIn(reverse("listenansicht"), html)
        self.assertNotIn("Flexible Listen: Spalten", Client().get(reverse("login")).content.decode())


@override_settings(SECURE_SSL_REDIRECT=False)
class ListenansichtAdminTest(TestCase):
    def setUp(self):
        Benutzer = get_user_model()
        self.chef = Benutzer.objects.create_superuser("chef", password="x" * 14)
        self.anna = Benutzer.objects.create_user("anna", password="x" * 14)
        self.ben = Benutzer.objects.create_user("ben", password="x" * 14)
        self.cem = Benutzer.objects.create_user("cem", password="x" * 14)
        for b in (self.anna, self.ben, self.cem):
            Listenansicht.objects.create(benutzer=b, schluessel="/a/|1|x", einstellung=EINSTELLUNG)
            Listenansicht.objects.create(benutzer=b, schluessel="/b/|1|x", einstellung=EINSTELLUNG)

    def _aktion(self, benutzer, auswahl):
        self.client.force_login(benutzer)
        return self.client.post(
            reverse("admin:auth_user_changelist"),
            {"action": "listenansichten_zuruecksetzen", "_selected_action": [b.pk for b in auswahl]},
            follow=True,
        )

    def test_aktion_loescht_nur_markierte_benutzer(self):
        antwort = self._aktion(self.chef, [self.anna, self.ben])
        self.assertContains(antwort, "4 gespeicherte Listenansicht(en) von 2 Benutzer(n) zurückgesetzt.")
        self.assertEqual(set(Listenansicht.objects.values_list("benutzer__username", flat=True)), {"cem"})

    def test_aktion_ohne_recht_nicht_moeglich(self):
        self.anna.is_staff = True
        self.anna.save()
        self._aktion(self.anna, [self.ben])
        self.assertEqual(Listenansicht.objects.count(), 6)

    def test_modell_und_benutzeradmin_sind_registriert(self):
        from django.contrib.auth.admin import UserAdmin
        self.assertIn(Listenansicht, site._registry)
        self.assertIsInstance(site._registry[get_user_model()], UserAdmin)

    def test_liste_in_der_verwaltung_erreichbar(self):
        self.client.force_login(self.chef)
        antwort = self.client.get(reverse("admin:kern_listenansicht_changelist"))
        self.assertContains(antwort, "/a/|1|x")
        self.assertFalse(site._registry[Listenansicht].has_add_permission(None))
