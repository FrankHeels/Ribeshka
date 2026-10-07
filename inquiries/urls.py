from django.urls import path

from . import views


app_name = "inquiries"
urlpatterns = [
    path("success/<uuid:token>/", views.success, name="success"),
    path("", views.wholesale, name="wholesale"),
]
