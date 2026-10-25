from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

from content.views import home, not_found, sitemap
from fishsite.health import ready


urlpatterns = [
    path("healthz/", ready, name="healthz"),
    path("two_factor/", include("fishsite.backoffice.auth")),
    path("admin/", admin.site.urls),
    path("integrations/", include("integrations.urls")),
    path("", home, name="home"),
    path("catalog/", include(("catalog.urls", "catalog"), namespace="catalog")),
    path("", include(("orders.urls", "orders"), namespace="orders")),
    path("wholesale/", include(("inquiries.urls", "inquiries"), namespace="inquiries")),
    path("", include(("content.urls", "content"), namespace="content")),
    path("sitemap.xml", sitemap, name="sitemap"),
]

handler404 = not_found

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
