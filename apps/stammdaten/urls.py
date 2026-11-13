from django.urls import path

from . import masken, radar_ansicht

app_name = "stammdaten"

urlpatterns = [
    path("kunde/neu/", masken.kunde_bearbeiten, name="kunde_neu"),
    path("kunde/<int:pk>/bearbeiten/", masken.kunde_bearbeiten, name="kunde_bearbeiten"),
    path("kunde/<int:pk>/ust-id-pruefen/", masken.kunde_ust_id_pruefen, name="kunde_ust_id_pruefen"),
    path("lieferant/neu/", masken.lieferant_bearbeiten, name="lieferant_neu"),
    path("lieferant/<int:pk>/bearbeiten/", masken.lieferant_bearbeiten, name="lieferant_bearbeiten"),
    path("lieferant/<int:pk>/ust-id-pruefen/", masken.lieferant_ust_id_pruefen, name="lieferant_ust_id_pruefen"),
    path("radar/", radar_ansicht.radar_liste, name="radar"),
    path("artikel/neu/", masken.artikel_bearbeiten, name="artikel_neu"),
    path("artikel/komponenten-suche/", masken.komponenten_suche, name="komponenten_suche"),
    path("artikel/tabelle/", masken.artikel_tabelle, name="artikel_tabelle"),
    path("artikel/<int:pk>/bearbeiten/", masken.artikel_bearbeiten, name="artikel_bearbeiten"),
]
