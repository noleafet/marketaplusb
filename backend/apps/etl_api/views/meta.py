from django.db import connection
from django.utils import timezone

from apps.engine.models import AnalysisRun
from apps.engine.versioning import resolve_strategy_version
from apps.trading.models import PaperSession

from ..serializers import iso
from .base import V1View, envelope


def _stamp(now, latest):
    lag = None if latest is None else max(0.0, (now - latest).total_seconds())
    return {"latest": iso(latest), "lag_seconds": lag}


class HealthView(V1View):
    def get(self, request):
        now = timezone.now()
        with connection.cursor() as cur:
            cur.execute("SELECT interval, MAX(time) FROM market_candles GROUP BY interval")
            candles = {iv: _stamp(now, t) for iv, t in cur.fetchall()}
            cur.execute("SELECT MAX(time) FROM funding_rates")
            funding = cur.fetchone()[0]
        run = (
            AnalysisRun.objects.exclude(finished_at__isnull=True)
            .order_by("-finished_at")
            .values_list("finished_at", flat=True)
            .first()
        )
        tick = (
            PaperSession.objects.exclude(last_tick_at__isnull=True)
            .order_by("-last_tick_at")
            .values_list("last_tick_at", flat=True)
            .first()
        )
        data = {
            "market_candles": candles,
            "funding_rates": _stamp(now, funding),
            "analysis_run": _stamp(now, run),
            "paper_session_tick": _stamp(now, tick),
            "current_strategy_version": resolve_strategy_version().id,
        }
        return envelope(data)
