"""Admin strategy settings and validation endpoints."""

from __future__ import annotations

from typing import Any

from rest_framework import serializers, status, viewsets
from rest_framework.response import Response

from apps.engine.analysis import latest_analysis
from apps.engine.models import AnalysisRun, CoinAnalysis
from apps.engine.robustness import _pair_correlation
from apps.engine.timescale import load_daily_equity
from apps.engine.versioning import diff_snapshots, resolve_strategy_version
from apps.engine.validation import pooled_ic
from apps.engine.scoring import FACTOR_WEIGHTS
from apps.jobs.runner import JobAlreadyActive, enqueue_job
from apps.jobs.models import JobRun
from apps.jobs.serializers import JobRunSerializer
from apps.trading.events import record_event
from apps.trading.models import GridEvent, MarketRegimeState, PaperSession, StrategySettings
from apps.trading.portfolio import open_sessions

HISTORY_RUNS = 12


class StrategySettingsSerializer(serializers.ModelSerializer):
    session_capital = serializers.FloatField(read_only=True)
    effective_trade_through_bps = serializers.FloatField(read_only=True)
    book_state = serializers.SerializerMethodField()

    class Meta:
        model = StrategySettings
        fields = [
            "signal_interval",
            "fill_model",
            "trade_through_bps",
            "overbought_guard_enabled",
            "holdout_days",
            "run_comparison_analysis",
            "book_capital",
            "max_sessions",
            "max_book_exposure",
            "daily_loss_limit_pct",
            "retire_after_failed_runs",
            "max_strategy_correlation",
            "btc_regime_enabled",
            "btc_trailing_stop_pct",
            "btc_trailing_window_days",
            "btc_oi_flush_pct",
            "btc_oi_near_high_pct",
            "btc_funding_z",
            "btc_min_bearish_hours",
            "session_capital",
            "effective_trade_through_bps",
            "book_state",
            "updated_at",
        ]
        read_only_fields = ["updated_at"]

    def get_book_state(self, obj: StrategySettings) -> dict[str, Any]:
        return {key: value for key, value in (obj.book_state or {}).items() if key != "start"}

    def _bounded(self, value: float, low: float, high: float, label: str) -> float:
        if value < low or value > high:
            raise serializers.ValidationError(f"{label} must be between {low:g} and {high:g}.")
        return value

    def validate_trade_through_bps(self, value: float) -> float:
        return self._bounded(value, 0.0, 50.0, "Trade-through")

    def validate_holdout_days(self, value: int) -> int:
        if value != 0 and not 14 <= value <= 180:
            raise serializers.ValidationError("Holdout must be 0 (off) or 14-180 days.")
        return value

    def validate_book_capital(self, value: float) -> float:
        return self._bounded(value, 100.0, 10_000_000.0, "Book capital")

    def validate_max_sessions(self, value: int) -> int:
        return int(self._bounded(value, 1, 50, "Max sessions"))

    def validate_max_book_exposure(self, value: float) -> float:
        return self._bounded(value, 0.0, 5.0, "Book exposure cap")

    def validate_daily_loss_limit_pct(self, value: float) -> float:
        return self._bounded(value, 0.0, 0.5, "Daily loss limit")

    def validate_retire_after_failed_runs(self, value: int) -> int:
        return int(self._bounded(value, 1, 30, "Retirement threshold"))

    def validate_max_strategy_correlation(self, value: float) -> float:
        return self._bounded(value, 0.1, 1.0, "Strategy correlation limit")

    def validate_btc_trailing_stop_pct(self, value: float) -> float:
        return self._bounded(value, 0.02, 0.30, "BTC trailing stop")

    def validate_btc_trailing_window_days(self, value: int) -> int:
        return int(self._bounded(value, 1, 30, "BTC trailing window"))

    def validate_btc_oi_flush_pct(self, value: float) -> float:
        return self._bounded(value, 0.01, 0.30, "BTC open-interest flush")

    def validate_btc_oi_near_high_pct(self, value: float) -> float:
        return self._bounded(value, 0.005, 0.10, "BTC near-high band")

    def validate_btc_funding_z(self, value: float) -> float:
        return self._bounded(value, 0.5, 5.0, "BTC funding z-score")

    def validate_btc_min_bearish_hours(self, value: float) -> float:
        return self._bounded(value, 0, 72, "Minimum bearish duration")


def _analysis_interval(run: AnalysisRun | None) -> str | None:
    if run is None:
        return None
    return (run.params or {}).get("interval") or (run.summary or {}).get("interval")


class StrategySettingsView(viewsets.ViewSet):
    """GET returns the settings; POST updates them (token required)."""

    def _payload(self, row: StrategySettings) -> dict[str, Any]:
        run = latest_analysis()
        interval = _analysis_interval(run)
        return {
            **StrategySettingsSerializer(row).data,
            "latest_analysis": {
                "id": str(run.id) if run else None,
                "interval": interval,
                "started_at": run.started_at if run else None,
            },
            "analysis_stale": bool(run and interval and interval != row.signal_interval),
        }

    def list(self, request):
        return Response(self._payload(StrategySettings.load()))

    def create(self, request):
        row = StrategySettings.load()
        serializer = StrategySettingsSerializer(row, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        old_version = resolve_strategy_version(row)
        serializer.save()
        new_version = resolve_strategy_version(row)
        if new_version.id != old_version.id:
            diff = diff_snapshots(old_version.snapshot, new_version.snapshot)
            diff.pop("code_sha", None)
            record_event(
                GridEvent.Type.PARAM_CHANGE,
                reason="settings_update",
                before={k: d["from"] for k, d in diff.items()},
                after={k: d["to"] for k, d in diff.items()},
                strategy_version=new_version,
            )
        payload = self._payload(row)
        if request.data.get("rerun_analysis") and payload["analysis_stale"]:
            try:
                job = enqueue_job(
                    "daily_analysis",
                    trigger=JobRun.Trigger.MANUAL,
                    params={"interval": row.signal_interval},
                )
                payload["job"] = JobRunSerializer(job).data
            except JobAlreadyActive as exc:
                # Settings are saved; only the re-run is refused.
                payload["job_conflict"] = exc.message
        return Response(payload, status=status.HTTP_200_OK)


REGIME_TRANSITIONS = 20


class MarketRegimeView(viewsets.ViewSet):
    """BTC market regime: phase, readings, recovery checklist, transitions."""

    def list(self, request):
        from apps.trading.regime import recovery_status, thresholds

        row = StrategySettings.load()
        state = MarketRegimeState.load()
        events = GridEvent.objects.filter(type=GridEvent.Type.REGIME).order_by("-time")[
            :REGIME_TRANSITIONS
        ]
        return Response(
            {
                "enabled": row.btc_regime_enabled,
                "phase": state.phase,
                "entered_at": state.entered_at,
                "reasons": list(state.reasons or []),
                "metrics": state.metrics or {},
                "thresholds": thresholds(row),
                "recovery": recovery_status(state),
                "last_evaluated_at": state.last_evaluated_at,
                "last_error": state.last_error,
                "transitions": [
                    {
                        "time": event.time,
                        "from": (event.before or {}).get("phase"),
                        "to": (event.after or {}).get("phase"),
                        "reasons": (event.after or {}).get("reasons") or [],
                    }
                    for event in events
                ],
            }
        )


def _daily_session_returns(session_id: str) -> dict[str, float]:
    closes = load_daily_equity(session_id, days=120)
    days = sorted(closes)
    return {
        days[i]: closes[days[i]] / closes[days[i - 1]] - 1.0
        for i in range(1, len(days))
        if closes[days[i - 1]] > 0
    }


def _paper_correlation(sessions: list[PaperSession]) -> dict[str, Any]:
    """Correlation of the live paper strategies' own daily returns."""

    returns = {session.coin.symbol: _daily_session_returns(str(session.id)) for session in sessions}
    symbols = sorted(returns)
    pairs = []
    for i, left in enumerate(symbols):
        for right in symbols[i + 1 :]:
            corr = _pair_correlation(returns[left], returns[right], 90)
            if corr is not None:
                pairs.append({"a": left, "b": right, "correlation": round(corr, 4)})
    pairs.sort(key=lambda pair: pair["correlation"], reverse=True)
    values = [pair["correlation"] for pair in pairs]
    return {
        "symbols": symbols,
        "pairs": pairs,
        "avg_pair": round(sum(values) / len(values), 4) if values else None,
        "max_pair": values[0] if values else None,
        "days": max((len(series) for series in returns.values()), default=0),
    }


class StrategyValidationView(viewsets.ViewSet):
    """Score validity, strategy correlation, and backtest-vs-paper drift."""

    def list(self, request):
        row = StrategySettings.load()
        run = latest_analysis()
        summary = (run.summary if run else None) or {}
        history = list(
            AnalysisRun.objects.filter(
                status=AnalysisRun.Status.SUCCEEDED,
                mode=run.mode if run else AnalysisRun.Mode.NORMAL,
                scope=AnalysisRun.Scope.UNIVERSE,
            )
            .order_by("-started_at")
            .values("id", "started_at", "summary")[:HISTORY_RUNS]
        )
        summaries = [item["summary"] or {} for item in history]
        pooled = {"score": pooled_ic(summaries, "score")}
        for name in FACTOR_WEIGHTS:
            pooled[name] = pooled_ic(summaries, name)
        ic_history = [
            {
                "analysis": str(item["id"]),
                "started_at": item["started_at"],
                "ic": ((item["summary"] or {}).get("score_validation") or {})
                .get("score", {})
                .get("ic"),
                "coins": ((item["summary"] or {}).get("score_validation") or {}).get("coins"),
            }
            for item in history
        ]

        sessions = list(open_sessions())
        holdout_by_coin: dict[int, dict[str, Any]] = {}
        if run is not None:
            for result in CoinAnalysis.objects.filter(
                analysis=run, coin_id__in=[s.coin_id for s in sessions]
            ).only("coin_id", "oos_metrics"):
                holdout_by_coin[result.coin_id] = result.oos_metrics or {}
        drift = []
        for session in sessions:
            days = max(
                1e-9,
                ((session.last_tick_at or session.started_at) - session.started_at).total_seconds()
                / 86400.0,
            )
            metrics = holdout_by_coin.get(session.coin_id) or {}
            holdout = metrics.get("holdout_return")
            holdout_days = metrics.get("holdout_days") or 0
            expected_daily = (
                float(holdout) / float(holdout_days)
                if holdout is not None and holdout_days
                else None
            )
            actual_daily = float(session.net_return) / days if days >= 1 else None
            drift.append(
                {
                    "symbol": session.coin.symbol,
                    "status": session.status,
                    "days": round(days, 2),
                    "capital": session.capital,
                    "equity": round(session.equity, 4),
                    "net_return": round(session.net_return, 6),
                    "exposure": round(
                        session.position_qty * (session.mark_price or 0.0)
                        / max(session.equity, 1e-9),
                        4,
                    ),
                    "expected_daily": round(expected_daily, 6) if expected_daily is not None else None,
                    "actual_daily": round(actual_daily, 6) if actual_daily is not None else None,
                    "failed_runs": session.failed_runs,
                }
            )

        return Response(
            {
                "analysis": (
                    {
                        "id": str(run.id),
                        "started_at": run.started_at,
                        "interval": _analysis_interval(run),
                        "fill_model": summary.get("fill_model"),
                        "holdout_days": summary.get("holdout_days"),
                        "universe": summary.get("universe"),
                        "eligible": summary.get("eligible"),
                        "paper_retired": summary.get("paper_retired") or [],
                        "paper_skipped_correlation": summary.get("paper_skipped_correlation")
                        or [],
                    }
                    if run
                    else None
                ),
                "score_validation": summary.get("score_validation"),
                "strategy_validation": summary.get("strategy_validation"),
                "findings": summary.get("validation_findings") or [],
                "pooled_ic": pooled,
                "ic_history": ic_history,
                "backtest_correlation": {
                    key: (
                        summary.get("correlation")
                        if (summary.get("correlation") or {}).get("pairs")
                        else summary.get("universe_correlation") or {}
                    ).get(key)
                    for key in ("basis", "avg_pair_correlation", "max_pair_correlation", "threshold", "pairs", "lookback_days")
                },
                "paper_correlation": _paper_correlation(sessions),
                "paper_drift": drift,
                "book": StrategySettingsSerializer(row).data["book_state"],
                "settings": StrategySettingsSerializer(row).data,
            }
        )
