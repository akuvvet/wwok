from django.urls import path

from . import views

app_name = "verkauf"

urlpatterns = [
    path("", views.start, name="start"),
    path("belege/", views.belege, name="belege"),
    path("kunde/<int:kunde_id>/wiedervorlage/", views.wiedervorlage_neu, name="wiedervorlage_neu"),
    path("wiedervorlage/<int:wiedervorlage_id>/erledigt/", views.wiedervorlage_erledigt, name="wiedervorlage_erledigt"),
    path("kunde/<int:kunde_id>/", views.kunde_detail, name="kunde"),
    path("kunde/<int:kunde_id>/lieferscheine-abrechnen/", views.lieferscheine_abrechnen, name="lieferscheine_abrechnen"),
    path("kunde/<int:kunde_id>/beleg-neu/", views.beleg_neu, name="beleg_neu"),
    path("beleg/<int:beleg_id>/", views.beleg_detail, name="beleg"),
    path("beleg/<int:beleg_id>/kopf/", views.beleg_kopf, name="beleg_kopf"),
    path("beleg/<int:beleg_id>/position/", views.position_neu, name="position_neu"),
    path("beleg/<int:beleg_id>/position/<int:position_id>/aendern/", views.position_aendern, name="position_aendern"),
    path("beleg/<int:beleg_id>/position/<int:position_id>/loeschen/", views.position_loeschen, name="position_loeschen"),
    path("beleg/<int:beleg_id>/position/<int:position_id>/gruppenpreis/", views.gruppenpreis, name="gruppenpreis"),
    path("beleg/<int:beleg_id>/loeschen/", views.beleg_loeschen, name="beleg_loeschen"),
    path("beleg/<int:beleg_id>/abschliessen/", views.beleg_abschliessen, name="beleg_abschliessen"),
    path("beleg/<int:beleg_id>/teillieferung/", views.beleg_teillieferung, name="beleg_teillieferung"),
    path("beleg/<int:beleg_id>/kopieren/", views.beleg_kopieren, name="beleg_kopieren"),
    path("beleg/<int:beleg_id>/folgebeleg/", views.beleg_folgebeleg, name="beleg_folgebeleg"),
    path("beleg/<int:beleg_id>/gutschrift/", views.beleg_gutschrift, name="beleg_gutschrift"),
    path("beleg/<int:beleg_id>/pdf/", views.beleg_pdf_ansicht, name="beleg_pdf"),
    path("beleg/<int:beleg_id>/xrechnung/", views.beleg_xrechnung, name="beleg_xrechnung"),
    path("beleg/<int:beleg_id>/versand/", views.beleg_versand, name="beleg_versand"),
    path("beleg/<int:beleg_id>/versand/<int:versand_id>/aufkleber/", views.versand_aufkleber, name="versand_aufkleber"),
    path("abos/", views.abos, name="abos"),
    path("abos/<int:abo_id>/", views.abo_aktion, name="abo_aktion"),
    path("beleg/<int:beleg_id>/abo/", views.abo_aus_rechnung, name="abo_aus_rechnung"),
    path("beleg/<int:beleg_id>/mail/", views.beleg_mail, name="beleg_mail"),
    path("api/artikel/", views.api_artikel, name="api_artikel"),
]
