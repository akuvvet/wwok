from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import path

admin.site.site_header = "wwok Warenwirtschaft"
admin.site.site_title = "wwok"
admin.site.index_title = "Verwaltung"

urlpatterns = [
    path("verwaltung/", admin.site.urls),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
