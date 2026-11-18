"""Seitenleiste unter „Lieferant“: + Bestellung und + Wareneingang für den gerade geöffneten Lieferanten (wie beim Kunden)."""
from django import template

from apps.einkauf.models import BestellStatus, Bestellung

register = template.Library()


@register.simple_tag(takes_context=True)
def offene_lieferantenbestellungen(context, lieferant):
    """Bestellt oder teilweise geliefert: dazu lässt sich ein Wareneingang erfassen (nur im aktiven Mandanten)."""
    mandant = getattr(context.get("request"), "mandant", None)
    if mandant is None or lieferant is None:
        return []
    return list(Bestellung.objects.filter(mandant=mandant, lieferant=lieferant,
                                          status__in=[BestellStatus.BESTELLT, BestellStatus.TEIL]).order_by("-datum", "-id")[:20])
