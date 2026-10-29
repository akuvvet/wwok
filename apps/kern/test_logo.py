import shutil
import tempfile
from decimal import Decimal
from io import BytesIO

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from PIL import Image

from apps.stammdaten.models import Kunde
from apps.verkauf.models import Beleg, Belegart, Position
from apps.verkauf.pdf import beleg_pdf

from .admin import MandantForm
from .models import Mandant

MEDIEN = tempfile.mkdtemp()


def png(breite=40, hoehe=20) -> bytes:
    puffer = BytesIO()
    Image.new("RGB", (breite, hoehe), (200, 30, 30)).save(puffer, "PNG")
    return puffer.getvalue()


@override_settings(MEDIA_ROOT=MEDIEN, SECURE_SSL_REDIRECT=False)
class MandantLogoTest(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIEN, ignore_errors=True)

    def setUp(self):
        self.m = Mandant.objects.create(kurzname="OKAY", firmenname="OKAY Computer")

    def test_logo_speichern_und_im_pdf(self):
        self.m.logo.save("logo.png", SimpleUploadedFile("logo.png", png()))
        k = Kunde.objects.create(nummer="1", name1="Muster")
        b = Beleg.objects.create(mandant=self.m, art=Belegart.ANGEBOT, kunde=k)
        Position.objects.create(beleg=b, bezeichnung="Dienst", einzelpreis=Decimal("10"))
        mit = beleg_pdf(b)
        self.assertTrue(mit.startswith(b"%PDF"))
        self.m.logo.delete(save=True)
        ohne = beleg_pdf(Beleg.objects.get(pk=b.pk))
        self.assertGreater(len(mit), len(ohne))  # Bild ist eingebettet

    def test_fehlende_logodatei_bricht_pdf_nicht(self):
        self.m.logo.save("weg.png", SimpleUploadedFile("weg.png", png()))
        self.m.logo.storage.delete(self.m.logo.name)
        k = Kunde.objects.create(nummer="1", name1="Muster")
        b = Beleg.objects.create(mandant=self.m, art=Belegart.ANGEBOT, kunde=k)
        Position.objects.create(beleg=b, bezeichnung="Dienst", einzelpreis=Decimal("10"))
        self.assertTrue(beleg_pdf(b).startswith(b"%PDF"))

    def test_zu_grosses_oder_kein_bild_wird_abgelehnt(self):
        daten = {"kurzname": "X", "firmenname": "X GmbH", "land": "DE", "smtp_port": 587, "smtp_sicherheit": "starttls",
                 "konto_erloes_19": "8400", "konto_erloes_7": "8300", "konto_erloes_0": "8200", "konto_bank": "1200",
                 "konto_kasse": "1000", "aktiv": "on"}
        gross = SimpleUploadedFile("g.png", png() + b"0" * (2 * 1024 * 1024 + 1))
        form = MandantForm(daten, {"logo": gross})
        self.assertIn("logo", form.errors)
        form = MandantForm(daten, {"logo": SimpleUploadedFile("t.png", b"kein bild")})
        self.assertIn("logo", form.errors)
        self.assertTrue(MandantForm(daten, {"logo": SimpleUploadedFile("k.png", png())}).is_valid())

    def test_admin_seite_laedt_mit_vorschau(self):
        self.client.force_login(get_user_model().objects.create_superuser("a", "a@a.de", "x" * 14))
        self.m.logo.save("logo.png", SimpleUploadedFile("logo.png", png()))
        r = self.client.get(reverse("admin:kern_mandant_change", args=[self.m.pk]))
        self.assertContains(r, "Vorschau")
        self.assertContains(r, "logos/logo")
