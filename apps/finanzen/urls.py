from django.urls import path

from . import views

app_name = "finanzen"

urlpatterns = [
    path("", views.offene_posten, name="offene_posten"),
    path("beleg/<int:beleg_id>/", views.beleg_op, name="beleg"),
    path("beleg/<int:beleg_id>/zahlung/", views.zahlung_buchen, name="zahlung"),
    path("zahlung/<int:zahlung_id>/storno/", views.zahlung_storno, name="zahlung_storno"),
    path("kunde/<int:kunde_id>/konto/", views.kontoauszug, name="kontoauszug"),
    path("bank/", views.bank, name="bank"),
    path("bank/<int:umsatz_id>/", views.bank_zuordnen, name="bank_zuordnen"),
    path("kassenbuch/", views.kassenbuch, name="kassenbuch"),
    path("lastschrift/", views.lastschrift, name="lastschrift"),
    path("lastschrift/export/", views.lastschrift_export, name="lastschrift_export"),
    path("lastschrift/<int:ls_id>/aufheben/", views.lastschrift_aufheben, name="lastschrift_aufheben"),
    path("datev/", views.datev_export, name="datev"),
    path("mahnwesen/", views.mahnwesen, name="mahnwesen"),
    path("mahnwesen/erstellen/", views.mahnungen_erstellen, name="mahnungen_erstellen"),
    path("mahnung/<int:mahnung_id>/pdf/", views.mahnung_pdf_ansicht, name="mahnung_pdf"),
    path("mahnung/<int:mahnung_id>/mail/", views.mahnung_mail, name="mahnung_mail"),
]
