from django.urls import path

from .views.meta import HealthView

urlpatterns = [
    path("meta/health/", HealthView.as_view(), name="etl-health"),
]
