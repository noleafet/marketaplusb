from datetime import datetime

from django.utils import timezone

from apps.engine import timescale
from apps.trading.models import (
    GridEvent,
    LiveOrderAudit,
    PaperEvaluation,
    PaperFill,
    PaperSession,
    SessionSnapshot,
)

from ..errors import InvalidParameter, NotFoundProblem
from ..pagination import page_meta, paginate_queryset, paginate_rows
from ..params import HOLD_BACK, parse_query
from ..serializers import (
    equity_out,
    evaluation_out,
    event_out,
    fill_out,
    order_audit_out,
    session_out,
    snapshot_out,
)
from .base import V1View, envelope

NO_LIVE_RUNNER = {"is_partial": True, "note": "no live runner"}


def _empty(query):
    """An empty envelope for environments without sessions, or None to serve normally."""
    if query.environment in (None, "paper"):
        return None
    meta = {"next_cursor": None, "watermark": None, "page_size": query.page_size}
    if query.environment == "live":
        return envelope([], meta=meta, completeness=NO_LIVE_RUNNER)
    return envelope([], meta=meta)


def _require_session(pk):
    if not PaperSession.objects.filter(pk=pk).exists():
        raise NotFoundProblem("No such session.")


def _fill_filters(qs, query):
    if query.symbols:
        qs = qs.filter(session__coin__binance_symbol__in=query.symbols)
    if query.strategy_version:
        qs = qs.filter(strategy_version_id=query.strategy_version)
    if query.config_version:
        qs = qs.filter(config_version_id=query.config_version)
    return qs


class SessionListView(V1View):
    def get(self, request):
        query = parse_query(request)
        empty = _empty(query)
        if empty is not None:
            return empty
        qs = PaperSession.objects.select_related("coin")
        if query.symbols:
            qs = qs.filter(coin__binance_symbol__in=query.symbols)
        if query.strategy_version:
            qs = qs.filter(strategy_version_id=query.strategy_version)
        if query.config_version:
            qs = qs.filter(config_version_id=query.config_version)
        if query.session:
            qs = qs.filter(pk=query.session)
        page = paginate_queryset(qs, endpoint="sessions", key="updated_at", query=query)
        active = [str(s.pk) for s in page.rows if s.status == "active"]
        return envelope(
            [session_out(s) for s in page.rows],
            meta=page_meta(page, query),
            completeness={"active_ids": active},
        )


class SessionDetailView(V1View):
    def get(self, request, pk):
        try:
            s = PaperSession.objects.select_related("coin").get(pk=pk)
        except PaperSession.DoesNotExist:
            raise NotFoundProblem("No such session.")
        return envelope(session_out(s), completeness={"is_partial": s.status == "active"})


class SnapshotsView(V1View):
    def get(self, request, pk):
        query = parse_query(request)
        _require_session(pk)
        empty = _empty(query)
        if empty is not None:
            return empty
        qs = SessionSnapshot.objects.filter(session_id=pk)
        page = paginate_queryset(qs, endpoint="session-snapshots", key="time", query=query)
        return envelope([snapshot_out(r) for r in page.rows], meta=page_meta(page, query))


class EquityView(V1View):
    def get(self, request, pk):
        query = parse_query(request)
        _require_session(pk)
        empty = _empty(query)
        if empty is not None:
            return empty
        bounds = [b for b in (query.start, query.updated_since) if b is not None]
        lower = max(bounds) if bounds else None
        now = timezone.now()
        upper = min(query.end or now, now - HOLD_BACK)

        def fetch(after_key, after_id, limit):
            return timescale.page_equity(str(pk), lower, upper, after_id, limit)

        page = paginate_rows(
            fetch,
            endpoint="session-equity",
            key="time",
            id_key="time",
            query=query,
            id_type=datetime,
        )
        return envelope([equity_out(r) for r in page.rows], meta=page_meta(page, query))


class EvaluationsView(V1View):
    def get(self, request, pk):
        query = parse_query(request)
        _require_session(pk)
        empty = _empty(query)
        if empty is not None:
            return empty
        qs = PaperEvaluation.objects.filter(session_id=pk)
        page = paginate_queryset(
            qs, endpoint="session-evaluations", key="evaluated_at", query=query
        )
        return envelope([evaluation_out(r) for r in page.rows], meta=page_meta(page, query))


def _fills(request, endpoint, pk=None):
    query = parse_query(request)
    if pk is not None:
        _require_session(pk)
    empty = _empty(query)
    if empty is not None:
        return empty
    qs = _fill_filters(PaperFill.objects.select_related("session__coin"), query)
    if pk is not None:
        qs = qs.filter(session_id=pk)
    elif query.session:
        qs = qs.filter(session_id=query.session)
    # Page on insertion time: a fill's own `time` is the strategy bar time and may
    # be much older than the insert, which would let consumer watermarks skip rows.
    page = paginate_queryset(qs, endpoint=endpoint, key="created_at", query=query)
    return envelope([fill_out(r) for r in page.rows], meta=page_meta(page, query))


class SessionFillsView(V1View):
    def get(self, request, pk):
        return _fills(request, "session-fills", pk)


class FillsView(V1View):
    def get(self, request):
        return _fills(request, "fills")


class EventsView(V1View):
    def get(self, request):
        query = parse_query(request)
        types = request.query_params.getlist("type")
        valid = {t.value for t in GridEvent.Type}
        for t in types:
            if t not in valid:
                raise InvalidParameter(
                    "'type' must be one of: " + ", ".join(sorted(valid)) + ".",
                    errors=[{"field": "type", "code": "invalid"}],
                )
        qs = GridEvent.objects.all()
        if types:
            qs = qs.filter(type__in=types)
        if query.environment:
            qs = qs.filter(environment=query.environment)
        if query.symbols:
            qs = qs.filter(symbol__in=query.symbols)
        if query.session:
            qs = qs.filter(session_id=query.session)
        if query.strategy_version:
            qs = qs.filter(strategy_version_id=query.strategy_version)
        if query.config_version:
            qs = qs.filter(config_version_id=query.config_version)
        page = paginate_queryset(qs, endpoint="events", key="time", query=query)
        return envelope([event_out(r) for r in page.rows], meta=page_meta(page, query))


class OrderAuditView(V1View):
    def get(self, request):
        query = parse_query(request)
        qs = LiveOrderAudit.objects.all()
        if query.symbols:
            qs = qs.filter(symbol__in=query.symbols)
        page = paginate_queryset(qs, endpoint="live-order-audit", key="created_at", query=query)
        return envelope([order_audit_out(r) for r in page.rows], meta=page_meta(page, query))
