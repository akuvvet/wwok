from django.urls import path

from . import views

app_name = "shop"

urlpatterns = [
    path("", views.uebersicht, name="uebersicht"),
    path("<int:shop_id>/bestellungen/", views.bestellungen, name="bestellungen"),
    path("<int:shop_id>/abgleich/", views.abgleich, name="abgleich"),
]
