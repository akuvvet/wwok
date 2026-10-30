from django.urls import path

from . import masken

app_name = "stammdaten"

urlpatterns = [
    path("kunde/neu/", masken.kunde_bearbeiten, name="kunde_neu"),
    path("kunde/<int:pk>/bearbeiten/", masken.kunde_bearbeiten, name="kunde_bearbeiten"),
    path("lieferant/neu/", masken.lieferant_bearbeiten, name="lieferant_neu"),
    path("lieferant/<int:pk>/bearbeiten/", masken.lieferant_bearbeiten, name="lieferant_bearbeiten"),
    path("artikel/neu/", masken.artikel_bearbeiten, name="artikel_neu"),
    path("artikel/komponenten-suche/", masken.komponenten_suche, name="komponenten_suche"),
    path("artikel/<int:pk>/bearbeiten/", masken.artikel_bearbeiten, name="artikel_bearbeiten"),
]
