from django.urls import path

from . import views

app_name = "werkstatt"

urlpatterns = [
    path("", views.liste, name="liste"),
    path("annahme/", views.annahme, name="annahme"),
    path("<int:reparatur_id>/", views.detail, name="detail"),
    path("<int:reparatur_id>/status/", views.status, name="status"),
    path("<int:reparatur_id>/position/", views.position, name="position"),
    path("<int:reparatur_id>/position/<int:position_id>/loeschen/", views.position_loeschen, name="position_loeschen"),
    path("<int:reparatur_id>/rechnung/", views.rechnung, name="rechnung"),
    path("<int:reparatur_id>/garantie-buchen/", views.garantie_buchen, name="garantie_buchen"),
    path("<int:reparatur_id>/notiz/", views.notiz, name="notiz"),
    path("<int:reparatur_id>/schein/<str:art>/", views.schein, name="schein"),
]
