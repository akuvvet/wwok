from django.urls import path

from . import views

app_name = "verkauf"

urlpatterns = [
    path("", views.start, name="start"),
    path("kunde/<int:kunde_id>/", views.kunde_detail, name="kunde"),
    path("kunde/<int:kunde_id>/beleg-neu/", views.beleg_neu, name="beleg_neu"),
    path("beleg/<int:beleg_id>/", views.beleg_detail, name="beleg"),
    path("beleg/<int:beleg_id>/kopf/", views.beleg_kopf, name="beleg_kopf"),
    path("beleg/<int:beleg_id>/position/", views.position_neu, name="position_neu"),
    path("beleg/<int:beleg_id>/position/<int:position_id>/loeschen/", views.position_loeschen, name="position_loeschen"),
    path("beleg/<int:beleg_id>/abschliessen/", views.beleg_abschliessen, name="beleg_abschliessen"),
    path("beleg/<int:beleg_id>/folgebeleg/", views.beleg_folgebeleg, name="beleg_folgebeleg"),
    path("api/artikel/", views.api_artikel, name="api_artikel"),
]
