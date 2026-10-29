from datetime import datetime, timedelta

from django.utils import timezone

from apps.common.binance import INTERVAL_MS
from apps.engine import timescale
from apps.engine.models import CoinAnalysis

from ..errors import InvalidParameter, WindowTooLarge
from ..pagination import page_meta, paginate_queryset, paginate_rows
from ..params import HOLD_BACK, parse_query
from ..serializers import candle_out, features_out, funding_out, iso
from .base import V1View, envelope

MAX_WINDOW_DAYS = 400


def _single_symbol(query):
    if len(query.symbols) != 1:
        raise InvalidParameter(
            "Exactly one 'symbol' is required.",
            errors=[{"field": "symbol", "code": "single_value_required"}],
        )
    return query.symbols[0]


def _check_open_window(query, now):
    """parse_query checks start..end; with only start, the window runs to now."""
    if query.start and not query.end and now - query.start > timedelta(days=MAX_WINDOW_DAYS):
        raise WindowTooLarge(
            f"The start-end window may not exceed {MAX_WINDOW_DAYS} days.",
            errors=[{"field": "end", "code": "window_too_large"}],
        )


def _lower(query):
    bounds = [b for b in (query.start, query.updated_since) if b is not None]
    return max(bounds) if bounds else None


class CandlesView(V1View):
    def get(self, request):
        query = parse_query(
            request,
            required=("symbol", "timeframe"),
            max_page_size=5000,
            max_window_days=MAX_WINDOW_DAYS,
        )
        symbol = _single_symbol(query)
        timeframe = query.timeframe
        if timeframe not in INTERVAL_MS:
            raise InvalidParameter(
                "'timeframe' must be one of: " + ", ".join(INTERVAL_MS) + ".",
                errors=[{"field": "timeframe", "code": "invalid_choice"}],
            )
        now = timezone.now()
        _check_open_window(query, now)
        interval_ms = INTERVAL_MS[timeframe]
        interval = timedelta(milliseconds=interval_ms)
        # A candle is served only once fully closed by min(end, now - HOLD_BACK).
        try:
            upper = min(query.end or now, now - HOLD_BACK) - interval
        except OverflowError:
            raise InvalidParameter(
                "'end' is out of range.", errors=[{"field": "end", "code": "out_of_range"}]
            )
        lower = _lower(query)

        def fetch(after_key, after_id, limit):
            return timescale.page_candles(symbol, timeframe, lower, upper, after_id, limit)

        page = paginate_rows(
            fetch,
            endpoint="market-candles",
            key="time",
            id_key="time",
            query=query,
            id_type=datetime,
        )
        times = [r["time"] for r in page.rows]
        gaps = [
            {"from": iso(prev + interval), "to": iso(nxt)}
            for prev, nxt in zip(times, times[1:])
            if nxt - prev > interval
        ]
        expected = (times[-1] - times[0]) // interval + 1 if times else 0
        return envelope(
            [candle_out(r, symbol, timeframe, interval_ms) for r in page.rows],
            meta=page_meta(page, query),
            completeness={"expected_bars": expected, "returned_bars": len(times), "gaps": gaps},
        )


class FundingView(V1View):
    def get(self, request):
        query = parse_query(request, required=("symbol",), max_window_days=MAX_WINDOW_DAYS)
        symbol = _single_symbol(query)
        now = timezone.now()
        _check_open_window(query, now)
        upper = min(query.end or now, now - HOLD_BACK)
        lower = _lower(query)

        def fetch(after_key, after_id, limit):
            return timescale.page_funding(symbol, lower, upper, after_id, limit)

        page = paginate_rows(
            fetch,
            endpoint="market-funding",
            key="time",
            id_key="time",
            query=query,
            id_type=datetime,
        )
        return envelope([funding_out(r, symbol) for r in page.rows], meta=page_meta(page, query))


class FeaturesView(V1View):
    def get(self, request):
        query = parse_query(request)
        qs = CoinAnalysis.objects.filter(
            analysis__status="succeeded",
            analysis__data_cutoff__isnull=False,
            analysis__finished_at__isnull=False,
        ).select_related("analysis", "coin")
        if query.symbols:
            qs = qs.filter(coin__binance_symbol__in=query.symbols)
        if query.timeframe:
            qs = qs.filter(analysis__params__interval=query.timeframe)
        page = paginate_queryset(
            qs, endpoint="market-features", key="analysis__finished_at", query=query
        )
        return envelope([features_out(r) for r in page.rows], meta=page_meta(page, query))
