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
    path("beleg/<int:beleg_id>/position/<int:position_id>/gruppenpreis/", views.gruppenpreis, name="gruppenpreis"),
    path("beleg/<int:beleg_id>/abschliessen/", views.beleg_abschliessen, name="beleg_abschliessen"),
    path("beleg/<int:beleg_id>/folgebeleg/", views.beleg_folgebeleg, name="beleg_folgebeleg"),
    path("beleg/<int:beleg_id>/gutschrift/", views.beleg_gutschrift, name="beleg_gutschrift"),
    path("beleg/<int:beleg_id>/pdf/", views.beleg_pdf_ansicht, name="beleg_pdf"),
    path("beleg/<int:beleg_id>/mail/", views.beleg_mail, name="beleg_mail"),
    path("api/artikel/", views.api_artikel, name="api_artikel"),
]
