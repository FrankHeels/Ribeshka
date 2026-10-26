from django.urls import reverse

from .models import SiteLink, SiteSettings


CANONICAL_ROUTES = frozenset({
    "home",
    "inquiries:wholesale",
    "content:about",
    "content:delivery",
    "content:contacts",
    "catalog:category",
    "catalog:product",
})


def seo_context(request):
    match = request.resolver_match
    if match is None or match.view_name not in CANONICAL_ROUTES:
        return {}
    path = reverse(match.view_name, args=match.args, kwargs=match.kwargs)
    return {"canonical_url": request.build_absolute_uri(path)}


def site_settings_context(request):
    return {
        "site_settings": SiteSettings.objects.filter(pk=1).first(),
        "site_links": SiteLink.objects.filter(settings_id=1).exclude(url=""),
    }
