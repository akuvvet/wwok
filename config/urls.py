from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

from apps.kern import views as kern_views

admin.site.site_header = "wwok Warenwirtschaft"
admin.site.site_title = "wwok"
admin.site.index_title = "Verwaltung"

# Kein Passwort-Reset per Mail: er wäre ohne Anmeldung erreichbar und liefe nicht über den Mailserver einer Firma.
# Vergessene Passwörter setzt ein Admin in der Verwaltung neu.
konto = [
    path("login/", auth_views.LoginView.as_view(), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("password_change/", auth_views.PasswordChangeView.as_view(), name="password_change"),
    path("password_change/done/", auth_views.PasswordChangeDoneView.as_view(), name="password_change_done"),
]

urlpatterns = [
    path("verwaltung/", admin.site.urls),
    path("konto/", include(konto)),
    path("verkauf/", include("apps.verkauf.urls")),
    path("lager/", include("apps.lager.urls")),
    path("finanzen/", include("apps.finanzen.urls")),
    path("werkstatt/", include("apps.werkstatt.urls")),
    path("einkauf/", include("apps.einkauf.urls")),
    path("esd/", include("apps.esd.urls")),
    path("shop/", include("apps.shop.urls")),
    path("stammdaten/", include("apps.stammdaten.urls")),
    path("favicon.ico", kern_views.favicon, name="favicon"),
    path("", kern_views.dashboard, name="dashboard"),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
