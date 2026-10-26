import secrets

from django.conf import settings
from rest_framework.permissions import BasePermission, SAFE_METHODS


class IsEngineTokenOrReadOnly(BasePermission):
    """Token gate for mutating API calls.

    Reads can be opened for local dashboards with ENGINE_ALLOW_PUBLIC_READS.
    Mutations always require the engine token to be configured and supplied.
    """

    message = "A valid X-Engine-Token header is required."

    def has_permission(self, request, view) -> bool:
        if request.method in SAFE_METHODS:
            return bool(settings.ENGINE_ALLOW_PUBLIC_READS)
        expected = settings.ENGINE_API_TOKEN
        supplied = request.headers.get("X-Engine-Token", "")
        if not expected or not supplied:
            return False
        return secrets.compare_digest(expected, supplied)
