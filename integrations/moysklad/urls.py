from django.urls import path

from .webhooks import price_webhook


urlpatterns = [
    path("webhooks/prices/<str:secret>/", price_webhook, name="moysklad-price-webhook"),
]
