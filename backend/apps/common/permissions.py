import secrets

from django.conf import settings
from rest_framework.permissions import BasePermission, SAFE_METHODS


class IsEngineTokenOrReadOnly(BasePermission):
    """Token gate for the API.

    Reads are public only when ENGINE_ALLOW_PUBLIC_READS is true; otherwise they require the
    same token as mutations. Mutations always require the token.
    """

    message = "A valid X-Engine-Token header is required."

    def has_permission(self, request, view) -> bool:
        expected = settings.ENGINE_API_TOKEN
        supplied = request.headers.get("X-Engine-Token", "")
        token_ok = (
            bool(expected)
            and bool(supplied)
            and secrets.compare_digest(expected, supplied)
        )
        if request.method in SAFE_METHODS:
            return token_ok or bool(settings.ENGINE_ALLOW_PUBLIC_READS)
        return token_ok
