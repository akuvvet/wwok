from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase


class BaseTemplateTests(SimpleTestCase):
    def setUp(self):
        self.html = (Path(settings.BASE_DIR) / 'templates' / 'base.html').read_text(encoding='utf-8')

    def test_color_scheme(self):
        self.assertIn('<meta name="color-scheme" content="light dark">', self.html)
        self.assertIn('color-scheme: light dark;', self.html)

    def test_hinweis_variable(self):
        self.assertNotIn('color:#b7791f', self.html)
        self.assertIn('var(--hinweis)', self.html)
        self.assertEqual(self.html.count('--hinweis:'), 2)

    def test_fehler_und_warnung_bleiben(self):
        self.assertIn("contains('error')", self.html)
        self.assertIn("contains('warning')", self.html)
        self.assertIn('var DAUER = 3000;', self.html)
