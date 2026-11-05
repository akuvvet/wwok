from django.contrib import messages
from django.db.models import F, Prefetch, Window
from django.db.models.functions import RowNumber
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.kern.rechte import nur_admin
from apps.verkauf.views import mit_mandant

from . import services
from .models import Shop, ShopBestellung, SyncProtokoll
from .shopware import ShopFehler

ZEITLIMIT_MASKE = 12  # Sekunden; der Webserver bricht nach 30 Sekunden ab, eine Shop-Anfrage darf ebenso lange dauern


@mit_mandant
def uebersicht(request):
    # nur die letzten 3 Protokollzeilen je Shop, in einer Abfrage
    letzte = SyncProtokoll.objects.annotate(
        rang=Window(RowNumber(), partition_by=F("shop_id"), order_by=[F("zeitpunkt").desc(), F("id").desc()])).filter(rang__lte=3)
    shops = Shop.objects.filter(mandant=request.mandant).prefetch_related(
        Prefetch("protokolle", queryset=SyncProtokoll.objects.filter(pk__in=letzte.values("pk")), to_attr="letzte_protokolle"))
    return render(request, "shop/uebersicht.html", {
        "shops": shops, "bestellungen": ShopBestellung.objects.filter(shop__mandant=request.mandant).select_related("shop", "beleg", "beleg__kunde")[:30]})


@mit_mandant
@require_POST
def bestellungen(request, shop_id):
    shop = get_object_or_404(Shop, pk=shop_id, mandant=request.mandant, aktiv=True)
    try:
        erg = services.bestellungen_abrufen(shop)
        messages.success(request, f"{erg['neu']} neue Bestellung(en) importiert, {erg['bekannt']} waren schon bekannt.")
        for fehler in erg["fehler"]:
            messages.error(request, fehler)
    except ShopFehler as fehler:
        messages.error(request, str(fehler))
    return redirect("shop:uebersicht")


@mit_mandant
@require_POST
def abgleich(request, shop_id):
    shop = get_object_or_404(Shop, pk=shop_id, mandant=request.mandant, aktiv=True)
    try:
        erg = services.abgleichen(shop, alle=bool(request.POST.get("alle")))
        messages.success(request, f"{erg['gesendet']} Artikel an den Shop gesendet ({erg['nicht_im_shop']} nicht im Shop gefunden, {erg['geprueft']} geprüft).")
    except ShopFehler as fehler:
        messages.error(request, str(fehler))
    return redirect("shop:uebersicht")


@mit_mandant
@require_POST
@nur_admin
def artikeldaten(request, shop_id):
    shop = get_object_or_404(Shop, pk=shop_id, mandant=request.mandant, aktiv=True)
    try:
        erg = services.artikeldaten_senden(shop, alle=bool(request.POST.get("alle")), nummer=request.POST.get("nummer", "").strip(),
                                           zeitlimit=ZEITLIMIT_MASKE)
        (messages.warning if erg["fehler"] else messages.success)(request, services.artikeldaten_meldung(erg))
    except ShopFehler as fehler:
        messages.error(request, str(fehler))
    return redirect("shop:uebersicht")
