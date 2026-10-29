from datetime import datetime, timedelta, timezone as dt_timezone

from django.db import transaction
from django.db.models import F
from django.utils import timezone
from rest_framework.throttling import BaseThrottle

from .errors import RateLimited
from .models import RateWindow

WINDOW_SECONDS = 60
RETENTION = timedelta(hours=1)


def _now():
    return timezone.now()


class ClientRateThrottle(BaseThrottle):
    """Fixed 60 s window per client, counted in the database (no cache configured)."""

    def allow_request(self, request, view) -> bool:
        client = request.user
        limit = client.rate_limit_per_min
        now = _now()
        epoch = int(now.timestamp())
        start_epoch = epoch - epoch % WINDOW_SECONDS
        reset_epoch = start_epoch + WINDOW_SECONDS
        window_start = datetime.fromtimestamp(start_epoch, tz=dt_timezone.utc)
        with transaction.atomic():
            window, created = RateWindow.objects.get_or_create(
                client=client, window_start=window_start
            )
            if created:
                RateWindow.objects.filter(
                    client=client, window_start__lt=now - RETENTION
                ).delete()
            RateWindow.objects.filter(pk=window.pk).update(count=F("count") + 1)
            window.refresh_from_db(fields=["count"])
        count = window.count
        request.rate_limit = (limit, max(limit - count, 0), reset_epoch)
        if count > limit:
            raise RateLimited(retry_after=max(1, reset_epoch - epoch))
        return True
