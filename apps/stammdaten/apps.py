from django.apps import AppConfig


class StammdatenConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.stammdaten"
    verbose_name = "Stammdaten"

    def ready(self):
        from . import verwendung  # noqa: F401  Löschschutz für verwendete Artikel, Kunden, Lieferanten
