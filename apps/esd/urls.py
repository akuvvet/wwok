from django.urls import path

from . import views

app_name = "esd"

urlpatterns = [
    path("", views.uebersicht, name="uebersicht"),
    path("suche/", views.suche, name="suche"),
    path("artikel/<int:artikel_id>/", views.artikel, name="artikel"),
    path("artikel/<int:artikel_id>/aktivieren/", views.aktivieren, name="aktivieren"),
    path("key/<int:key_id>/status/", views.key_status, name="key_status"),
    path("beleg/<int:beleg_id>/", views.beleg, name="beleg"),
    path("beleg/<int:beleg_id>/zuordnen/", views.zuordnen, name="zuordnen"),
    path("beleg/<int:beleg_id>/mail/", views.mail, name="mail"),
    path("vergabe/<int:vergabe_id>/rueckgabe/", views.rueckgabe, name="rueckgabe"),
]
