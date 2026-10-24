from django.urls import path

from . import views

app_name = "finanzen"

urlpatterns = [
    path("", views.offene_posten, name="offene_posten"),
    path("beleg/<int:beleg_id>/", views.beleg_op, name="beleg"),
    path("beleg/<int:beleg_id>/zahlung/", views.zahlung_buchen, name="zahlung"),
    path("kunde/<int:kunde_id>/konto/", views.kontoauszug, name="kontoauszug"),
    path("mahnwesen/", views.mahnwesen, name="mahnwesen"),
    path("mahnwesen/erstellen/", views.mahnungen_erstellen, name="mahnungen_erstellen"),
    path("mahnung/<int:mahnung_id>/pdf/", views.mahnung_pdf_ansicht, name="mahnung_pdf"),
    path("mahnung/<int:mahnung_id>/mail/", views.mahnung_mail, name="mahnung_mail"),
]
