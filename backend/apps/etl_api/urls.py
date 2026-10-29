from django.urls import path, re_path

from .views.base import NotFoundView
from .views.meta import HealthView

urlpatterns = [
    path("meta/health/", HealthView.as_view(), name="etl-health"),
    re_path(r"^.*$", NotFoundView.as_view(), name="etl-not-found"),
]
