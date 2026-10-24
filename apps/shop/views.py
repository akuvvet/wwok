from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.verkauf.views import mit_mandant

from . import services
from .models import Shop, ShopBestellung
from .shopware import ShopFehler


@mit_mandant
def uebersicht(request):
    shops = Shop.objects.filter(mandant=request.mandant).prefetch_related("protokolle")
    return render(request, "shop/uebersicht.html", {
        "shops": shops, "bestellungen": ShopBestellung.objects.filter(shop__mandant=request.mandant).select_related("shop", "beleg", "beleg__kunde")[:30]})


@mit_mandant
@require_POST
def bestellungen(request, shop_id):
    shop = get_object_or_404(Shop, pk=shop_id, mandant=request.mandant, aktiv=True)
    try:
        erg = services.bestellungen_abrufen(shop)
        messages.success(request, f"{erg['neu']} neue Bestellung(en) als Auftrag importiert, {erg['bekannt']} waren schon bekannt.")
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
