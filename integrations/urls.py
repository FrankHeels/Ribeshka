from django.urls import include, path


app_name = "integrations"

urlpatterns = [path("moysklad/", include("integrations.moysklad.urls"))]
