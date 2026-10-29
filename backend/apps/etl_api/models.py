import uuid

from django.db import models


def default_scopes() -> list[str]:
    return ["read"]


class ApiClient(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=128)
    key_prefix = models.CharField(max_length=12, unique=True)
    key_hash = models.CharField(max_length=64)
    scopes = models.JSONField(default=default_scopes)
    rate_limit_per_min = models.PositiveIntegerField(default=120)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    @property
    def is_authenticated(self) -> bool:
        return True

    def __str__(self) -> str:
        return f"{self.name} ({self.key_prefix})"


class RateWindow(models.Model):
    client = models.ForeignKey(ApiClient, on_delete=models.CASCADE, related_name="windows")
    window_start = models.DateTimeField()
    count = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["client", "window_start"], name="uniq_ratewindow_client_start"
            )
        ]
