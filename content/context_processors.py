from .models import SiteLink, SiteSettings


def site_settings_context(request):
    return {
        "site_settings": SiteSettings.objects.filter(pk=1).first(),
        "site_links": SiteLink.objects.filter(settings_id=1).exclude(url=""),
    }
