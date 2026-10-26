"""URL configuration for the engine API."""

from django.urls import include, path
from django.views.decorators.csrf import csrf_exempt
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response


@api_view(["GET"])
@permission_classes([AllowAny])
def health(request):
    return Response({"status": "ok", "service": "grid-suitability-engine"})


urlpatterns = [
    path("api/health/", health, name="health"),
    path("api/", include("apps.coins.urls")),
    path("api/", include("apps.engine.urls")),
    path("api/", include("apps.trading.urls")),
    path("api/", include("apps.jobs.urls")),
]
