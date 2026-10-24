from django.apps import AppConfig


class KernConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.kern"
    verbose_name = "Kern"

    def ready(self):
        from . import signals  # noqa: F401
