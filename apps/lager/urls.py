from django.urls import path

from . import views

app_name = "lager"

urlpatterns = [
    path("", views.uebersicht, name="uebersicht"),
    path("artikel/<int:artikel_id>/", views.artikel_detail, name="artikel"),
    path("artikel/<int:artikel_id>/buchen/", views.buchen, name="buchen"),
    path("artikel/<int:artikel_id>/inventur/", views.inventur_buchen, name="inventur"),
    path("artikel/<int:artikel_id>/stamm/", views.stamm, name="stamm"),
]
