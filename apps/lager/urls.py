from django.urls import path

from . import views

app_name = "lager"

urlpatterns = [
    path("", views.uebersicht, name="uebersicht"),
    path("artikel/<int:artikel_id>/", views.artikel_detail, name="artikel"),
    path("artikel/<int:artikel_id>/buchen/", views.buchen, name="buchen"),
    path("artikel/<int:artikel_id>/inventur/", views.inventur_buchen, name="inventur"),
    path("artikel/<int:artikel_id>/umbuchung/", views.umbuchung, name="umbuchung"),
    path("seriennummern/", views.seriennummer_suche, name="seriennummern"),
    path("position/<int:position_id>/seriennummern/", views.position_seriennummern, name="position_sn"),
    path("gutschrift/<int:beleg_id>/retoure/", views.gutschrift_retoure, name="retoure"),
    path("artikel/<int:artikel_id>/reparatur/", views.reparatur_buchen, name="reparatur"),
    path("seriennummer/<int:seriennummer_id>/einlagern/", views.seriennummer_einlagern, name="seriennummer_einlagern"),
    path("inventur/", views.inventur_liste, name="inventur_liste"),
    path("artikel/<int:artikel_id>/stamm/", views.stamm, name="stamm"),
]
