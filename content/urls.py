from django.urls import path

from . import views


app_name = "content"
urlpatterns = [
    path("about/", views.about, name="about"),
    path("delivery/", views.delivery, name="delivery"),
    path("contacts/", views.contacts, name="contacts"),
    path("privacy/", views.privacy, name="privacy"),
]

