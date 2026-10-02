from django.urls import path

from . import views


app_name = "catalog"
urlpatterns = [
    path("categories/<slug:slug>/", views.category, name="category"),
    path("products/<slug:slug>/", views.product, name="product"),
]
