from datetime import date
from decimal import Decimal as D
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.einkauf.models import Bestellung
from apps.finanzen.mahntext import mail_fuer
from apps.finanzen.models import Mahnstufe, Mahnung
from apps.kern.models import AenderungsProtokoll, Mandant
from apps.stammdaten.models import Kunde, Lieferant
from apps.werkstatt import services as werkstatt
from apps.werkstatt.models import Status

from .mail import PLATZHALTER, STANDARD, ausfuellen, vorlage_fehler, vorlagen_anlegen, vorschlag
from .models import Beleg, Belegart, Mailart, Mailvorlage, Versand
from .versand import versandmail_text


class StandardTextTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.kunde = Kunde.objects.create(nummer="1", name1="Kunde GmbH", email="k@example.test")

    def test_jede_art_hat_standard_und_platzhalter(self):
        self.assertEqual({str(a) for a in Mailart}, {str(a) for a in STANDARD})
        self.assertEqual({str(a) for a in Mailart}, {str(a) for a in PLATZHALTER})
        for art, (betreff, text) in STANDARD.items():
            self.assertEqual(vorlage_fehler(art, betreff, text), "", art)

    def test_beleg_text_bleibt_wie_bisher(self):
        b = Beleg.objects.create(mandant=self.m, art=Belegart.RECHNUNG, kunde=self.kunde, nummer="RE2026-00001")
        an, betreff, text = vorschlag(b)
        self.assertEqual(betreff, "Rechnung RE2026-00001 von OKAY Computer")
        self.assertEqual(text, "Sehr geehrte Damen und Herren,\n\nanbei erhalten Sie die Rechnung RE2026-00001.\n\n"
                               "Bei Fragen melden Sie sich gerne.\n\nMit freundlichen Grüßen\nOKAY Computer")

    def test_versandtext_bleibt_wie_bisher(self):
        ls = Beleg.objects.create(mandant=self.m, art=Belegart.LIEFERSCHEIN, kunde=self.kunde, nummer="LS1")
        v = Versand.objects.create(beleg=ls, sendungsnummer="123")
        betreff, text = versandmail_text(v)
        self.assertEqual(betreff, "Ihre Bestellung wurde versendet – OKAY Computer")
        self.assertIn("mit DHL Paket versendet.\nSendungsnummer: 123\nSendungsverfolgung: ", text)
        self.assertTrue(text.endswith(f"{v.tracking_url}\n\nMit freundlichen Grüßen\nOKAY Computer"))

    def test_brief_ohne_sendungsnummer_hinterlaesst_keine_luecke(self):
        ls = Beleg.objects.create(mandant=self.m, art=Belegart.LIEFERSCHEIN, kunde=self.kunde, nummer="LS1")
        v = Versand.objects.create(beleg=ls, dienst="dp", sendungsnummer="MARKE")
        _, text = versandmail_text(v)
        self.assertNotIn("MARKE", text)
        self.assertIn("versendet.\n\nMit freundlichen", text)

    def test_mahnung_nutzt_text_der_mahnstufe(self):
        r = Beleg.objects.create(mandant=self.m, art=Belegart.RECHNUNG, kunde=self.kunde, nummer="RE1")
        Mahnstufe.objects.create(mandant=self.m, stufe=1, bezeichnung="Zahlungserinnerung", text="Bitte {offen} zahlen.")
        mahnung = Mahnung.objects.create(beleg=r, stufe=1, bezeichnung="Zahlungserinnerung", offener_betrag=D("10"),
                                         frist=date(2026, 11, 20))
        self.assertEqual(mail_fuer(mahnung), ("Zahlungserinnerung zu Rechnung RE1", "Bitte 10,00 zahlen."))
        Mailvorlage.objects.create(mandant=self.m, art=Mailart.MAHNUNG, betreff="{bezeichnung} {nummer}",
                                   text="Hallo {kunde},\r\n\r\n{mahntext}")
        self.assertEqual(mail_fuer(mahnung), ("Zahlungserinnerung RE1", "Hallo Kunde GmbH,\n\nBitte 10,00 zahlen."))

    def test_statusmail_nutzt_vorlage_des_mandanten(self):
        self.m.smtp_host, self.m.absender_email = "smtp.example.test", "a@okay.test"
        self.m.save()
        Mailvorlage.objects.create(mandant=self.m, art=Mailart.REPARATUR_FERTIG, betreff="Fertig: {geraet}",
                                   text="{anrede}, abholen bitte. {status}")
        r = werkstatt.annehmen(self.m, self.kunde, "Notebook", "defekt")
        with mock.patch("apps.verkauf.mail.verbindung", return_value=None):
            werkstatt.status_setzen(r, Status.FERTIG)
        self.assertEqual(mail.outbox[-1].subject, "Fertig: Notebook")
        self.assertEqual(mail.outbox[-1].body, "Sehr geehrte Damen und Herren, abholen bitte. fertig – abholbereit")

    def test_status_nutzt_die_passende_vorlage_und_standardtext(self):
        self.m.smtp_host, self.m.absender_email = "smtp.example.test", "a@okay.test"
        self.m.save()
        r = werkstatt.annehmen(self.m, self.kunde, "Notebook", "defekt")
        erwartet = {Status.ANGENOMMEN: "wir haben Ihr Gerät Notebook unter der Auftragsnummer", Status.IN_REPARATUR: "ist jetzt in Reparatur.",
                    Status.FERTIG: "ist fertig und kann bei uns abgeholt werden."}
        with mock.patch("apps.verkauf.mail.verbindung", return_value=None):
            for status, wortlaut in erwartet.items():
                werkstatt.status_setzen(r, status)
                self.assertIn(wortlaut, mail.outbox[-1].body)
                self.assertTrue(mail.outbox[-1].subject.startswith(f"Reparatur {r.nummer}: "))

    def test_alle_angebotenen_platzhalter_werden_gefuellt(self):
        def vorlage(art):
            alle = " ".join("{" + name + "}" for name in PLATZHALTER[art])
            Mailvorlage.objects.update_or_create(mandant=self.m, art=art, defaults={"betreff": alle[:200], "text": alle})

        r = Beleg.objects.create(mandant=self.m, art=Belegart.RECHNUNG, kunde=self.kunde, nummer="RE1")
        vorlage(Mailart.VERSAND)
        self.assertNotIn("{", versandmail_text(Versand.objects.create(beleg=r, sendungsnummer="1"))[1])
        vorlage(Mailart.MAHNUNG)
        mahnung = Mahnung.objects.create(beleg=r, stufe=1, bezeichnung="Mahnung", offener_betrag=D("10"), frist=date(2026, 11, 20))
        self.assertNotIn("{", mail_fuer(mahnung)[1])
        for art in Belegart:
            vorlage(art)
        self.assertNotIn("{", vorschlag(r)[2])
        for art in (Mailart.REPARATUR_ANGENOMMEN, Mailart.REPARATUR_LAEUFT, Mailart.REPARATUR_FERTIG):
            vorlage(art)
        self.m.smtp_host, self.m.absender_email = "smtp.example.test", "a@okay.test"
        self.m.save()
        rep = werkstatt.annehmen(self.m, self.kunde, "Notebook", "defekt")
        with mock.patch("apps.verkauf.mail.verbindung", return_value=None):
            for status in (Status.ANGENOMMEN, Status.IN_REPARATUR, Status.FERTIG):
                werkstatt.status_setzen(rep, status)
                self.assertNotIn("{", mail.outbox[-1].body)

    def test_versand_mit_eigener_vorlage(self):
        ls = Beleg.objects.create(mandant=self.m, art=Belegart.LIEFERSCHEIN, kunde=self.kunde, nummer="LS1")
        v = Versand.objects.create(beleg=ls, sendungsnummer="123", versendet_am=date(2026, 11, 2))
        Mailvorlage.objects.create(mandant=self.m, art=Mailart.VERSAND, betreff="Paket {nummer}", text="{versanddatum} {dienst}")
        self.assertEqual(versandmail_text(v), ("Paket LS1", "02.11.2026 DHL Paket"))

    def test_mahnstufe_leer_oder_fehlerhaft_faellt_auf_standard_zurueck(self):
        r = Beleg.objects.create(mandant=self.m, art=Belegart.RECHNUNG, kunde=self.kunde, nummer="RE1")
        mahnung = Mahnung.objects.create(beleg=r, stufe=1, bezeichnung="Mahnung", offener_betrag=D("10"), frist=date(2026, 11, 20))
        stufe = Mahnstufe.objects.create(mandant=self.m, stufe=1, bezeichnung="Mahnung", text="")
        self.assertIn("bitte begleichen Sie die Rechnung RE1", mail_fuer(mahnung)[1])
        stufe.text = "Bitte zahlen {"
        stufe.save()
        self.assertIn("bitte begleichen Sie die Rechnung RE1", mail_fuer(mahnung)[1])

    def test_fehlerhafte_vorlage_faellt_auf_standard_zurueck(self):
        Mailvorlage.objects.create(mandant=self.m, art=Mailart.REPARATUR_FERTIG, betreff="x {", text="y")
        betreff, text = ausfuellen(self.m, Mailart.REPARATUR_FERTIG, {"nummer": "WS1", "status": "fertig", "geraet": "PC",
                                                                      "anrede": "Hallo", "firma": "F"})
        self.assertEqual(betreff, "Reparatur WS1: fertig")
        self.assertIn("PC", text)

    def test_altes_format_mit_index_faellt_auf_standard_zurueck(self):
        Mailvorlage.objects.create(mandant=self.m, art=Mailart.REPARATUR_FERTIG, betreff="{status[x]}", text="y")
        self.assertEqual(ausfuellen(self.m, Mailart.REPARATUR_FERTIG, {"nummer": "WS1", "status": "fertig"})[0], "Reparatur WS1: fertig")

    def test_angelegte_vorlage_steht_im_aenderungsprotokoll(self):
        vorlagen_anlegen()
        self.assertEqual(Mailvorlage.objects.count(), len(Mailart))
        self.assertEqual(AenderungsProtokoll.objects.filter(tabelle__icontains="mailvorlage").count(), len(Mailart))

    def test_mandanten_getrennt(self):
        zone = Mandant.objects.create(kurzname="ZONE", firmenname="ZONE Handel")
        Mailvorlage.objects.create(mandant=zone, art=Mailart.VERSAND, betreff="ZONE", text="z")
        self.assertEqual(ausfuellen(zone, Mailart.VERSAND, {})[0], "ZONE")
        self.assertNotEqual(ausfuellen(self.m, Mailart.VERSAND, {"firma": "F"})[0], "ZONE")

    def test_betreff_einzeilig_und_begrenzt(self):
        Mailvorlage.objects.create(mandant=self.m, art=Mailart.REPARATUR_FERTIG, betreff="{geraet}", text="y")
        betreff, _ = ausfuellen(self.m, Mailart.REPARATUR_FERTIG, {"geraet": "A\nB " + "x" * 300})
        self.assertNotIn("\n", betreff)
        self.assertEqual(len(betreff), 200)

    def test_vorlage_fehler(self):
        self.assertIn("Klammern", vorlage_fehler(Mailart.RECHNUNG, "x {", ""))
        self.assertIn("{foo}", vorlage_fehler(Mailart.RECHNUNG, "{foo}", ""))
        self.assertIn("{kunde.pk}", vorlage_fehler(Mailart.RECHNUNG, "{kunde.pk}", ""))
        self.assertIn("{betrag:x}", vorlage_fehler(Mailart.RECHNUNG, "{betrag:x}", ""))
        self.assertIn("{keys}", vorlage_fehler(Mailart.ESD, "Betreff", "ohne Schlüssel"))
        self.assertIn("{keys}", vorlage_fehler(Mailart.ESD, "Keys: {keys}", "ohne Schlüssel"))
        self.assertIn("{geraet}", vorlage_fehler(Mailart.VERSAND, "", "{geraet}"))
        self.assertEqual(vorlage_fehler(Mailart.RECHNUNG, "{{frei}} {nummer}", ""), "")


@override_settings(SECURE_SSL_REDIRECT=False)
class MailvorlagenAdminTest(TestCase):
    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.zone = Mandant.objects.create(kurzname="ZONE", firmenname="ZONE Handel")
        self.client.force_login(get_user_model().objects.create_superuser("chef", password="x" * 14))
        self.liste = reverse("admin:verkauf_mailvorlage_changelist")

    def seite(self, vorlage):
        return reverse("admin:verkauf_mailvorlage_change", args=[vorlage.pk])

    def test_liste_zeigt_alle_vorlagen_beider_firmen_und_laesst_eigene_stehen(self):
        eigene = Mailvorlage.objects.create(mandant=self.m, art=Mailart.RECHNUNG, betreff="Eigen", text="Eigener Text")
        r = self.client.get(self.liste)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Mailvorlage.objects.count(), 2 * len(Mailart))
        eigene.refresh_from_db()
        self.assertEqual((eigene.betreff, eigene.text), ("Eigen", "Eigener Text"))
        self.client.get(self.liste)
        self.assertEqual(Mailvorlage.objects.count(), 2 * len(Mailart))

    def test_nicht_anlegbar_nicht_loeschbar_art_fest(self):
        vorlagen_anlegen()
        v = Mailvorlage.objects.get(mandant=self.m, art=Mailart.RECHNUNG)
        self.assertEqual(self.client.get(reverse("admin:verkauf_mailvorlage_add")).status_code, 403)
        self.assertEqual(self.client.post(reverse("admin:verkauf_mailvorlage_delete", args=[v.pk]), {"post": "yes"}).status_code, 403)
        self.client.post(self.seite(v), {"art": Mailart.MAHNUNG, "betreff": "Neu {nummer}", "text": "T"})
        v.refresh_from_db()
        self.assertEqual((v.art, v.betreff), (Mailart.RECHNUNG, "Neu {nummer}"))

    def test_seite_nennt_platzhalter_und_lehnt_unbekannte_ab(self):
        vorlagen_anlegen()
        v = Mailvorlage.objects.get(mandant=self.m, art=Mailart.ESD)
        self.assertContains(self.client.get(self.seite(v)), "{keys}")
        r = self.client.post(self.seite(v), {"betreff": "{nummer}", "text": "ohne Schlüssel"})
        self.assertContains(r, "muss im Text vorkommen")
        r = self.client.post(self.seite(v), {"betreff": "{foo}", "text": "{keys}"})
        self.assertContains(r, "Unbekannter Platzhalter")
        v.refresh_from_db()
        self.assertEqual(v.betreff, STANDARD[Mailart.ESD][0])

    def test_auf_standard_zuruecksetzen(self):
        v = Mailvorlage.objects.create(mandant=self.m, art=Mailart.VERSAND, betreff="Eigen", text="Eigener Text")
        self.client.post(self.liste, {"action": "standard_setzen", "_selected_action": [v.pk]})
        v.refresh_from_db()
        self.assertEqual((v.betreff, v.text), STANDARD[Mailart.VERSAND])

    def test_nur_lesen_legt_nichts_an_und_setzt_nichts_zurueck(self):
        v = Mailvorlage.objects.create(mandant=self.m, art=Mailart.VERSAND, betreff="Eigen", text="Eigener Text")
        leser = get_user_model().objects.create_user("l", password="x" * 14, is_staff=True)
        leser.user_permissions.add(Permission.objects.get(codename="view_mailvorlage"))
        self.client.force_login(leser)
        self.assertEqual(self.client.get(self.liste).status_code, 200)
        self.assertEqual(Mailvorlage.objects.count(), 1)
        self.client.post(self.liste, {"action": "standard_setzen", "_selected_action": [v.pk]})
        v.refresh_from_db()
        self.assertEqual(v.betreff, "Eigen")

    def test_ohne_recht_keine_liste(self):
        self.client.force_login(get_user_model().objects.create_user("t", password="x" * 14, is_staff=True))
        self.assertEqual(self.client.get(self.liste).status_code, 403)
        self.assertEqual(Mailvorlage.objects.count(), 0)


@override_settings(SECURE_SSL_REDIRECT=False)
class MailwegeTest(TestCase):
    """Die Masken mit Mailvorschau zeigen den Text aus der Vorlage des gewählten Mandanten."""

    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")
        self.zone = Mandant.objects.create(kurzname="ZONE", firmenname="ZONE Handel")
        self.kunde = Kunde.objects.create(nummer="1", name1="Kunde GmbH", email="k@example.test")
        self.client.force_login(get_user_model().objects.create_user("t", password="x" * 14))
        s = self.client.session
        s["mandant_id"] = self.m.pk
        s.save()

    def test_lizenzschluessel(self):
        b = Beleg.objects.create(mandant=self.m, art=Belegart.RECHNUNG, kunde=self.kunde, nummer="RE1")
        adresse = reverse("esd:mail", args=[b.pk])
        with mock.patch("apps.esd.views.services.keys_text", return_value=[("Windows", ["K-1", "K-2"])]):
            r = self.client.get(adresse)
            self.assertEqual(r.context["betreff"], "Ihre Lizenzschlüssel zu Rechnung RE1")
            self.assertEqual(r.context["text"], "Sehr geehrte Damen und Herren,\n\nanbei Ihre Lizenzschlüssel zu Rechnung RE1:\n\n"
                                                "Windows:\n  K-1\n  K-2\n\nMit freundlichen Grüßen\nOKAY Computer")
            Mailvorlage.objects.create(mandant=self.zone, art=Mailart.ESD, betreff="ZONE", text="{keys}")
            Mailvorlage.objects.create(mandant=self.m, art=Mailart.ESD, betreff="Keys {nummer}", text="Hier: {keys}")
            r = self.client.get(adresse)
        self.assertEqual((r.context["betreff"], r.context["text"]), ("Keys RE1", "Hier: Windows:\n  K-1\n  K-2"))

    def test_bestellung_an_lieferanten(self):
        lieferant = Lieferant.objects.create(nummer="L1", name1="Techdata GmbH")
        b = Bestellung.objects.create(mandant=self.m, lieferant=lieferant, nummer="BE1")
        adresse = reverse("einkauf:bestellung_mail", args=[b.pk])
        r = self.client.get(adresse)
        self.assertEqual(r.context["betreff"], "Bestellung BE1 von OKAY Computer")
        self.assertEqual(r.context["text"], "Sehr geehrte Damen und Herren,\n\nanbei unsere Bestellung BE1. Bitte bestätigen Sie den "
                                            "Eingang und den voraussichtlichen Liefertermin.\n\nMit freundlichen Grüßen\nOKAY Computer")
        Mailvorlage.objects.create(mandant=self.m, art=Mailart.LIEFERANT, betreff="An {lieferant}", text="{anrede}, {nummer}")
        r = self.client.get(adresse)
        self.assertEqual((r.context["betreff"], r.context["text"]),
                         ("An Techdata GmbH", "Sehr geehrte Damen und Herren, BE1"))
