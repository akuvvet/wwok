from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path
from django.views.generic import RedirectView

admin.site.site_header = "wwok Warenwirtschaft"
admin.site.site_title = "wwok"
admin.site.index_title = "Verwaltung"

urlpatterns = [
    path("verwaltung/", admin.site.urls),
    path("konto/", include("django.contrib.auth.urls")),
    path("verkauf/", include("apps.verkauf.urls")),
    path("lager/", include("apps.lager.urls")),
    path("finanzen/", include("apps.finanzen.urls")),
    path("werkstatt/", include("apps.werkstatt.urls")),
    path("einkauf/", include("apps.einkauf.urls")),
    path("esd/", include("apps.esd.urls")),
    path("shop/", include("apps.shop.urls")),
    path("stammdaten/", include("apps.stammdaten.urls")),
    path("", RedirectView.as_view(pattern_name="verkauf:start", permanent=False)),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
