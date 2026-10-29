import secrets
from datetime import timedelta

from django.utils import timezone
from rest_framework.authentication import BaseAuthentication
from rest_framework.permissions import BasePermission

from .errors import InsufficientScope, RevokedKey, Unauthenticated
from .keys import hash_key
from .models import ApiClient

LAST_USED_GRANULARITY = timedelta(seconds=60)


class ApiKeyAuthentication(BaseAuthentication):
    def authenticate(self, request):
        header = request.headers.get("Authorization")
        if not header:
            return None
        scheme, _, token = header.partition(" ")
        token = token.strip()
        if scheme.lower() != "bearer" or not token.startswith("mpb_"):
            raise Unauthenticated("Malformed or unknown API key.")
        client = ApiClient.objects.filter(key_prefix=token[:12]).first()
        if client is None or not secrets.compare_digest(
            client.key_hash, hash_key(token)
        ):
            raise Unauthenticated("Malformed or unknown API key.")
        if client.revoked_at is not None:
            raise RevokedKey("This API key has been revoked.")
        now = timezone.now()
        if client.last_used_at is None or now - client.last_used_at > LAST_USED_GRANULARITY:
            ApiClient.objects.filter(pk=client.pk).update(last_used_at=now)
            client.last_used_at = now
        return (client, client)

    def authenticate_header(self, request):
        return "Bearer"


class HasReadScope(BasePermission):
    def has_permission(self, request, view) -> bool:
        client = request.user
        if not isinstance(client, ApiClient):
            raise Unauthenticated("A valid API key is required.")
        if "read" not in (client.scopes or []):
            raise InsufficientScope("The API key lacks the read scope.")
        return True
