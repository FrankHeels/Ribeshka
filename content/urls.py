from django.urls import path

from . import views


app_name = "content"
urlpatterns = [
    path("about/", views.about, name="about"),
    path("delivery/", views.delivery, name="delivery"),
    path("contacts/", views.contacts, name="contacts"),
    path("privacy/", views.privacy, name="privacy"),
    path("personal-data-consent/", views.personal_data_consent, name="personal_data_consent"),
    path(
        "personal-data-consent/revisions/<str:revision_id>/",
        views.consent_revision,
        name="consent_revision",
    ),
    path("yandex-metrica-consent/", views.yandex_metrica_consent, name="yandex_metrica_consent"),
]

