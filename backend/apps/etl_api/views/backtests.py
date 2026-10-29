import dataclasses

from apps.engine.models import CoinAnalysis, log_decision

from ..errors import NotFoundProblem
from ..pagination import page_meta, paginate_queryset
from ..params import parse_query
from ..serializers import backtest_out, fold_out
from .base import V1View, envelope


def _get_or_404(pk):
    try:
        return CoinAnalysis.objects.select_related("analysis", "coin").get(pk=pk)
    except CoinAnalysis.DoesNotExist:
        raise NotFoundProblem("No such backtest.")


class BacktestListView(V1View):
    def get(self, request):
        query = parse_query(request)
        qs = CoinAnalysis.objects.select_related("analysis", "coin")
        if query.environment not in (None, "backtest"):
            qs = qs.none()
        if query.symbols:
            qs = qs.filter(coin__binance_symbol__in=query.symbols)
        if query.timeframe:
            qs = qs.filter(analysis__params__interval=query.timeframe)
        if query.strategy_version:
            qs = qs.filter(analysis__strategy_version_id=query.strategy_version)
        if query.config_version:
            qs = qs.filter(config_version_id=query.config_version)
        # start/end bound data_end, so apply them here and hand paginate_queryset a
        # query without them (it would otherwise also apply them to created_at).
        if query.start:
            qs = qs.filter(data_end__gte=query.start)
        if query.end:
            qs = qs.filter(data_end__lte=query.end)
        page = paginate_queryset(
            qs,
            endpoint="backtests",
            key="created_at",
            query=dataclasses.replace(query, start=None, end=None),
        )
        partial = sorted(
            {str(r.analysis_id) for r in page.rows if r.analysis.status == "running"}
        )
        return envelope(
            [backtest_out(r) for r in page.rows],
            meta=page_meta(page, query),
            completeness={"partial_run_ids": partial, "trade_sample_is_complete": False},
        )


class BacktestDetailView(V1View):
    def get(self, request, pk):
        ca = _get_or_404(pk)
        status = ca.analysis.status
        return envelope(
            backtest_out(ca),
            completeness={
                "is_partial": status == "running",
                "run_status": status,
                "trade_sample_is_complete": False,
            },
        )


class BacktestFoldsView(V1View):
    def get(self, request, pk):
        ca = _get_or_404(pk)
        raw = ca.folds if isinstance(ca.folds, list) else []
        kept = [f for f in (fold_out(f) for f in raw) if f is not None]
        dropped = len(raw) - len(kept)
        if dropped:
            log_decision(
                "etl_api",
                "Dropped walk-forward folds with overlapping or unparseable train/test windows.",
                severity="warning",
                payload={"coin_analysis_id": ca.pk, "dropped": dropped, "total": len(raw)},
            )
        return envelope(kept)
