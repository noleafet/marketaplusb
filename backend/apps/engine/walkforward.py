"""Walk-forward optimization with untouched out-of-sample test windows."""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone as dt_timezone
from typing import Any

import numpy as np

from apps.engine.grid import GridParams, SimulationConfig, simulate_grid

MAX_FOLDS = 3
TRAIN_FRACTION = 0.45
TEST_FRACTION = 0.18
MIN_TRAIN_DAYS = 90
MIN_TEST_DAYS = 30


@dataclass
class WalkForwardResult:
    oos_metrics: dict[str, Any]
    in_sample_metrics: dict[str, Any]
    folds: list[dict[str, Any]]
    params: dict[str, Any]
    chart: dict[str, Any]
    trade_sample: list[dict[str, Any]]
    data_start: datetime | None
    data_end: datetime | None
    error: str = ""


def parameter_grid(base: GridParams) -> list[GridParams]:
    candidates: list[GridParams] = []
    for levels in (10, 20, 30):
        for lookback in (14, 30):
            for rebalance in (7, 14):
                candidates.append(
                    GridParams(
                        grid_levels=levels,
                        range_lookback_days=lookback,
                        rebalance_days=rebalance,
                        spacing_mode=base.spacing_mode,
                        atr_multiplier=base.atr_multiplier,
                        initial_inventory_pct=base.initial_inventory_pct,
                        stop_loss_pct=base.stop_loss_pct,
                        max_utilization=base.max_utilization,
                    )
                )
    return candidates


def _train_objective(metrics: dict[str, Any], min_trades: int) -> float:
    if not metrics:
        return -100.0
    trades = int(metrics.get("trades", 0))
    if trades <= 0:
        return -50.0
    ret = float(metrics.get("net_return", 0.0))
    dd = float(metrics.get("max_drawdown", 1.0))
    pf = min(3.0, float(metrics.get("profit_factor", 0.0)))
    activity = min(1.0, trades / max(1, min_trades))
    stop_penalty = -0.5 if metrics.get("stopped") else 0.0
    return ret - 1.5 * dd + 0.05 * pf + 0.05 * activity + stop_penalty


def _slice_until(rows: list[dict[str, Any]], end: datetime) -> list[dict[str, Any]]:
    return [row for row in rows if row["time"] <= end]


def _fold_metrics_subset(metrics: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "net_return",
        "max_drawdown",
        "calmar",
        "sharpe",
        "profit_factor",
        "win_rate",
        "trades",
        "closed_lots",
        "fees_paid",
        "funding_paid",
        "exposure",
        "time_in_range",
        "stopped",
        "liquidated",
        "stop_reason",
    ]
    return {key: metrics.get(key) for key in keys}


def _stitch_curves(
    fold_results: list[tuple[dict[str, Any], dict[str, Any]]],
    capital: float,
    max_points: int = 400,
) -> tuple[list[dict[str, Any]], float]:
    """Compound per-fold OOS equity curves into one normalized curve."""

    combined: list[dict[str, Any]] = []
    cumulative = 1.0
    peak = capital
    max_dd = 0.0
    for fold, result in fold_results:
        curve = result.get("equity_curve") or []
        if not curve:
            continue
        base_eq = float(curve[0]["equity"]) or capital
        for point in curve:
            scale = cumulative * (float(point["equity"]) / base_eq)
            equity = capital * scale
            peak = max(peak, equity)
            dd = 1.0 - equity / peak if peak > 0 else 0.0
            max_dd = max(max_dd, dd)
            combined.append(
                {
                    "t": point["t"],
                    "equity": round(equity, 4),
                    "price": point.get("price"),
                    "low": point.get("low"),
                    "high": point.get("high"),
                    "dd": round(dd, 6),
                    "fold": fold["fold"],
                }
            )
        fold_return = float(result["metrics"].get("net_return", 0.0))
        cumulative *= 1.0 + fold_return

    if len(combined) > max_points:
        indices = np.linspace(0, len(combined) - 1, max_points).astype(int)
        combined = [combined[i] for i in indices]
    return combined, max_dd


def run_walk_forward(
    rows: list[dict[str, Any]],
    funding_rows: list[dict[str, Any]],
    *,
    config: SimulationConfig,
    base_params: GridParams,
    lookback_days: int,
    min_trades: int,
) -> WalkForwardResult:
    if len(rows) < 200:
        return WalkForwardResult(
            {}, {}, [], {}, {}, [], None, None, error="insufficient candle history"
        )

    rows = sorted(rows, key=lambda r: r["time"])
    data_start = rows[0]["time"]
    data_end = rows[-1]["time"]
    analysis_start = max(data_start, data_end - timedelta(days=lookback_days))
    # Reserve warmup space for the largest range lookback in the grid.
    base = max(analysis_start, data_start + timedelta(days=32))
    available_days = (data_end - base).total_seconds() / 86400.0
    if available_days < 90:
        return WalkForwardResult(
            {}, {}, [], {}, {}, [], data_start, data_end,
            error=f"only {available_days:.0f} usable days; need at least 90",
        )

    train_days = max(MIN_TRAIN_DAYS, int(available_days * TRAIN_FRACTION))
    test_days = max(MIN_TEST_DAYS, int(available_days * TEST_FRACTION))
    step_days = test_days

    fold_specs: list[tuple[int, datetime, datetime, datetime, datetime]] = []
    for fold_no in range(MAX_FOLDS):
        train_start = base + timedelta(days=fold_no * step_days)
        train_end = train_start + timedelta(days=train_days)
        test_end = train_end + timedelta(days=test_days)
        if test_end > data_end:
            break
        fold_specs.append((fold_no, train_start, train_end, train_end, test_end))

    if not fold_specs:
        return WalkForwardResult(
            {}, {}, [], {}, {}, [], data_start, data_end,
            error="not enough history for a walk-forward fold",
        )

    params_candidates = parameter_grid(base_params)
    completed: list[tuple[dict[str, Any], dict[str, Any]]] = []
    folds_out: list[dict[str, Any]] = []
    selected_param_dicts: list[dict[str, Any]] = []
    is_returns: list[float] = []
    is_drawdowns: list[float] = []

    for fold_no, train_start, train_end, test_start, test_end in fold_specs:
        train_rows = _slice_until(rows, train_end)
        best_params: GridParams | None = None
        best_train_metrics: dict[str, Any] = {}
        best_objective = -1e9
        for candidate in params_candidates:
            result = simulate_grid(train_rows, funding_rows, train_start, candidate, config)
            if result.error:
                continue
            objective = _train_objective(result.metrics, min_trades)
            if objective > best_objective:
                best_objective = objective
                best_params = candidate
                best_train_metrics = result.metrics
        if best_params is None:
            continue

        test_rows = _slice_until(rows, test_end)
        test_result = simulate_grid(test_rows, funding_rows, test_start, best_params, config)
        if test_result.error:
            continue

        completed.append(
            (
                {"fold": fold_no + 1},
                {
                    "metrics": test_result.metrics,
                    "equity_curve": test_result.equity_curve,
                    "trades": test_result.trades,
                    "grid_levels": test_result.grid_levels,
                },
            )
        )
        selected_param_dicts.append(best_params.to_dict())
        is_returns.append(float(best_train_metrics.get("net_return", 0.0)))
        is_drawdowns.append(float(best_train_metrics.get("max_drawdown", 0.0)))
        folds_out.append(
            {
                "fold": fold_no + 1,
                "train_start": train_start.isoformat(),
                "train_end": train_end.isoformat(),
                "test_start": test_start.isoformat(),
                "test_end": test_end.isoformat(),
                "params": best_params.to_dict(),
                "train_objective": round(best_objective, 6),
                "train_metrics": _fold_metrics_subset(best_train_metrics),
                "test_metrics": _fold_metrics_subset(test_result.metrics),
            }
        )

    if not completed:
        return WalkForwardResult(
            {}, {}, [], {}, {}, [], data_start, data_end,
            error="no walk-forward fold completed successfully",
        )

    curve, stitched_dd = _stitch_curves(completed, config.capital)
    net_returns = [f["test_metrics"].get("net_return") or 0.0 for f in folds_out]
    combined_return = float(np.prod([1.0 + r for r in net_returns]) - 1.0)
    max_dd = max(
        [stitched_dd] + [float(f["test_metrics"].get("max_drawdown") or 0.0) for f in folds_out]
    )
    total_trades = sum(int(f["test_metrics"].get("trades") or 0) for f in folds_out)
    closed_lots = sum(int(f["test_metrics"].get("closed_lots") or 0) for f in folds_out)
    fees = sum(float(f["test_metrics"].get("fees_paid") or 0.0) for f in folds_out)
    funding = sum(float(f["test_metrics"].get("funding_paid") or 0.0) for f in folds_out)
    stopped = any(bool(f["test_metrics"].get("stopped")) for f in folds_out)
    liquidated = any(bool(f["test_metrics"].get("liquidated")) for f in folds_out)
    consistency = sum(1 for r in net_returns if r > 0) / len(net_returns)

    eq = np.array([point["equity"] for point in curve], dtype=float)
    if eq.size > 2:
        rets = np.diff(eq) / np.maximum(eq[:-1], 1e-9)
        std = float(np.std(rets, ddof=1))
        sharpe = float(np.mean(rets) / std * math.sqrt(config.periods_per_year)) if std > 1e-12 else 0.0
        downside = rets[rets < 0]
        sortino = (
            float(np.mean(rets) / np.std(downside, ddof=1) * math.sqrt(config.periods_per_year))
            if downside.size > 1 and np.std(downside, ddof=1) > 1e-12
            else 0.0
        )
    else:
        sharpe = sortino = 0.0

    test_start_first = datetime.fromisoformat(folds_out[0]["test_start"])
    test_end_last = datetime.fromisoformat(folds_out[-1]["test_end"])
    years = max((test_end_last - test_start_first).total_seconds() / (86400 * 365), 1e-9)
    annual_return = (1.0 + combined_return) ** (1.0 / years) - 1.0 if combined_return > -1 else -1.0
    calmar = annual_return / max_dd if max_dd > 1e-6 else 0.0

    param_counter = Counter(tuple(sorted(p.items())) for p in selected_param_dicts)
    mode_key, mode_count = param_counter.most_common(1)[0]
    recommended = dict(mode_key)
    parameter_stability = mode_count / len(selected_param_dicts)

    oos_metrics = {
        "net_return": round(combined_return, 6),
        "annualized_return": round(annual_return, 6),
        "max_drawdown": round(max_dd, 6),
        "calmar": round(calmar, 4),
        "sharpe": round(sharpe, 4),
        "sortino": round(sortino, 4),
        "profit_factor": round(
            float(
                np.mean([f["test_metrics"].get("profit_factor") or 0.0 for f in folds_out])
            ),
            4,
        ),
        "win_rate": round(
            float(np.mean([f["test_metrics"].get("win_rate") or 0.0 for f in folds_out])),
            6,
        ),
        "trades": total_trades,
        "closed_lots": closed_lots,
        "fees_paid": round(fees, 4),
        "funding_paid": round(funding, 4),
        "exposure": round(
            float(np.mean([f["test_metrics"].get("exposure") or 0.0 for f in folds_out])), 6
        ),
        "time_in_range": round(
            float(np.mean([f["test_metrics"].get("time_in_range") or 0.0 for f in folds_out])),
            6,
        ),
        "fold_consistency": round(consistency, 4),
        "fold_returns": [round(r, 6) for r in net_returns],
        "worst_fold_return": round(min(net_returns), 6),
        "best_fold_return": round(max(net_returns), 6),
        "parameter_stability": round(parameter_stability, 4),
        "folds": len(folds_out),
        "stopped": stopped,
        "liquidated": liquidated,
        "stop_reason": next(
            (f["test_metrics"].get("stop_reason") for f in folds_out if f["test_metrics"].get("stopped")),
            "",
        ),
        "test_days": round((test_end_last - test_start_first).total_seconds() / 86400, 1),
    }
    in_sample_metrics = {
        "avg_return": round(float(np.mean(is_returns)), 6) if is_returns else 0.0,
        "avg_max_drawdown": round(float(np.mean(is_drawdowns)), 6) if is_drawdowns else 0.0,
        "folds": len(is_returns),
        "selection_note": (
            "Parameters are optimized only on each fold's training window. "
            "Test windows are never used for parameter selection."
        ),
    }

    trade_sample: list[dict[str, Any]] = []
    for _, result in completed:
        trade_sample.extend(result["trades"])
    trade_sample = trade_sample[-200:]

    final_grid_levels = completed[-1][1].get("grid_levels") or []
    chart = {
        "equity_curve": curve,
        "grid_levels": final_grid_levels,
        "folds": [
            {
                "fold": f["fold"],
                "test_start": f["test_start"],
                "test_end": f["test_end"],
                "params": f["params"],
                "test_metrics": f["test_metrics"],
            }
            for f in folds_out
        ],
        "recommended_params": recommended,
    }
    return WalkForwardResult(
        oos_metrics=oos_metrics,
        in_sample_metrics=in_sample_metrics,
        folds=folds_out,
        params=recommended,
        chart=chart,
        trade_sample=trade_sample,
        data_start=data_start,
        data_end=data_end,
    )
