"""Hilfen für Tests: ein Mandanten-Logo in einem temporären Medienordner."""
import tempfile
from contextlib import contextmanager
from io import BytesIO

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from PIL import Image


@contextmanager
def mit_logo(mandant):
    puffer = BytesIO()
    Image.new("RGB", (60, 20), (200, 30, 30)).save(puffer, "PNG")
    with tempfile.TemporaryDirectory() as ordner, override_settings(MEDIA_ROOT=ordner):
        mandant.logo.save("logo.png", SimpleUploadedFile("logo.png", puffer.getvalue()))
        try:
            yield
        finally:
            mandant.logo.delete(save=True)


# Mandantenmaske in der Verwaltung: leere Unterbereiche (Zahlungs- und Plattformkonten) für POST-Daten
MANDANT_INLINES_LEER = {f"{p}-{k}": v for p in ("zahlungskonten", "plattformkonten")
                        for k, v in (("TOTAL_FORMS", "0"), ("INITIAL_FORMS", "0"), ("MIN_NUM_FORMS", "0"), ("MAX_NUM_FORMS", "1000"))}
