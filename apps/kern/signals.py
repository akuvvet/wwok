"""Schreibt bei jeder Änderung an Stammdaten automatisch einen Protokolleintrag."""
from django.core.serializers.json import DjangoJSONEncoder
from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver
from django.forms.models import model_to_dict
import json

from .middleware import aktueller_benutzer

PROTOKOLLIERTE_APPS = {"kern", "stammdaten", "verkauf", "lager", "finanzen", "werkstatt", "einkauf"}
AUSGENOMMEN = {"AenderungsProtokoll"}


def _relevant(sender) -> bool:
    return (
        sender._meta.app_label in PROTOKOLLIERTE_APPS
        and sender.__name__ not in AUSGENOMMEN
    )


def _als_dict(obj):
    return json.loads(json.dumps(model_to_dict(obj), cls=DjangoJSONEncoder, default=str))


def _benutzer():
    b = aktueller_benutzer()
    return b if b is not None and b.is_authenticated else None


@receiver(pre_save)
def _merke_vorher(sender, instance, **kwargs):
    if not _relevant(sender) or not instance.pk:
        return
    try:
        instance._protokoll_vorher = _als_dict(sender.objects.get(pk=instance.pk))
    except sender.DoesNotExist:
        instance._protokoll_vorher = None


@receiver(post_save)
def _protokolliere_speichern(sender, instance, created, **kwargs):
    if not _relevant(sender):
        return
    from .models import AenderungsProtokoll

    AenderungsProtokoll.objects.create(
        benutzer=_benutzer(),
        tabelle=sender._meta.label,
        datensatz_id=str(instance.pk),
        aktion="anlegen" if created else "aendern",
        vorher=None if created else getattr(instance, "_protokoll_vorher", None),
        nachher=_als_dict(instance),
    )


@receiver(post_delete)
def _protokolliere_loeschen(sender, instance, **kwargs):
    if not _relevant(sender):
        return
    from .models import AenderungsProtokoll

    AenderungsProtokoll.objects.create(
        benutzer=_benutzer(),
        tabelle=sender._meta.label,
        datensatz_id=str(instance.pk),
        aktion="loeschen",
        vorher=_als_dict(instance),
    )
