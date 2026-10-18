from django.apps import AppConfig
from django.contrib.admin.apps import AdminConfig


class BackofficeAdminConfig(AdminConfig):
    default_site = "fishsite.backoffice.sites.FishSiteAdminSite"


class BackofficeConfig(AppConfig):
    name = "fishsite.backoffice"
    verbose_name = "Кабинет владельца"
