"""Walk-forward optimization with untouched out-of-sample test windows."""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone as dt_timezone
from typing import Any

import numpy as np

from apps.engine.grid import (
    GridParams,
    PreparedMarket,
    SimulationConfig,
    SimulationResult,
    prepare_market,
    simulate_grid,
)
from apps.engine.hybrid_gate import Gate
from apps.engine.robustness import classify_regime, regime_summary

MAX_FOLDS = 3
TRAIN_FRACTION = 0.45
TEST_FRACTION = 0.18
MIN_TRAIN_DAYS = 90
MIN_TEST_DAYS = 30
STOP_ATR_MULTIPLES = (1.5, 2.5)
TP_ATR_MULTIPLES = (1.5, 3.0)
REBALANCE_MODES = ("fixed", "adaptive")
DEFAULT_TRADE_THROUGH_BPS = 2.0
MIN_HOLDOUT_DAYS = 14
ADAPTIVE_KEYS = ("stop_atr_multiple", "tp_atr_multiple")


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
    diagnostics: dict[str, Any] = field(default_factory=dict)
    # Untouched final window evaluated once with the deployed parameters.
    holdout_metrics: dict[str, Any] = field(default_factory=dict)
    # Daily strategy returns (OOS folds + holdout) for strategy correlation.
    strategy_returns: dict[str, float] = field(default_factory=dict)


def parameter_grid(base: GridParams) -> list[GridParams]:
    """Core grid shapes crossed with adaptive stop / take-profit distances.

    The stop distance and trailing take-profit multiple are treated as search
    dimensions, not fixed assumptions, so each fold selects them on its own
    training window and the untouched test window validates the choice. Fixed
    and event-driven rebalancing are searched the same way; the selected mode
    is then carried into the paper session.
    """

    candidates: list[GridParams] = []
    for levels in (10, 20, 30):
        for lookback in (14, 30):
            for rebalance in (7, 14):
                for stop_multiple in STOP_ATR_MULTIPLES:
                    for tp_multiple in TP_ATR_MULTIPLES:
                        for mode in REBALANCE_MODES:
                            candidates.append(
                                replace(
                                    base,
                                    grid_levels=levels,
                                    range_lookback_days=lookback,
                                    rebalance_days=rebalance,
                                    stop_atr_multiple=stop_multiple,
                                    tp_atr_multiple=tp_multiple,
                                    rebalance_mode=mode,
                                )
                            )
    return candidates


def _train_objective(metrics: dict[str, Any], min_trades: int) -> float:
    """Rank candidates on the training window.

    A stop-out is a hard risk failure, so it outweighs any return advantage.
    The stop and take-profit multiples are selected by this same objective on
    train-only data; the untouched test window then scores the selection.
    """

    if not metrics:
        return -100.0
    trades = int(metrics.get("trades", 0))
    if trades <= 0:
        return -50.0
    ret = float(metrics.get("net_return", 0.0))
    dd = float(metrics.get("max_drawdown", 1.0))
    pf = min(3.0, float(metrics.get("profit_factor", 0.0)))
    activity = min(1.0, trades / max(1, min_trades))
    stop_penalty = -1.0 if metrics.get("stopped") else 0.0
    return ret - 1.5 * dd + 0.05 * pf + 0.05 * activity + stop_penalty


def _slice_until(rows: list[dict[str, Any]], end: datetime) -> list[dict[str, Any]]:
    return [row for row in rows if row["time"] <= end]


def _stressed_config(config: SimulationConfig, multiplier: float) -> SimulationConfig:
    """Double fees and adverse slippage for the cost-stress validation run."""

    multiplier = max(1.0, float(multiplier))
    return replace(
        config,
        maker_fee_rate=config.maker_fee_rate * multiplier,
        taker_fee_rate=config.taker_fee_rate * multiplier,
        slippage_bps=config.slippage_bps * multiplier,
    )


def _sensitivity_variants(params: GridParams) -> list[tuple[str, GridParams]]:
    """Small perturbations around the selected parameters.

    A strategy that only works at one exact ladder shape is overfit even if the
    untouched test fold looks profitable.
    """

    variants: list[tuple[str, GridParams]] = []
    if params.grid_levels > 8:
        variants.append(
            ("fewer_levels", replace(params, grid_levels=max(3, params.grid_levels - 5)))
        )
    variants.append(("more_levels", replace(params, grid_levels=params.grid_levels + 5)))
    variants.append(
        (
            "shorter_range",
            replace(
                params,
                range_lookback_days=max(7, params.range_lookback_days - 7),
            ),
        )
    )
    variants.append(
        (
            "alternate_spacing",
            replace(
                params,
                spacing_mode=(
                    "arithmetic" if params.spacing_mode != "arithmetic" else "geometric"
                ),
            ),
        )
    )
    return variants


def _alternate_fill_config(config: SimulationConfig) -> tuple[str, SimulationConfig]:
    """The other fill model, so every result shows how much it depends on fills."""

    if config.trade_through_bps > 0:
        return "touch", replace(config, trade_through_bps=0.0)
    return "trade_through", replace(config, trade_through_bps=DEFAULT_TRADE_THROUGH_BPS)


def daily_strategy_returns(
    daily_equity: dict[str, float],
    starting_equity: float,
    end: datetime | None = None,
) -> dict[str, float]:
    """Day-over-day returns of one simulation's equity, keyed by ISO date.

    A stopped simulation is flat afterwards, so the days up to ``end`` are
    filled with zero returns instead of being dropped; dropping them would
    make a stopped strategy look correlated only with its losing days.
    """

    returns: dict[str, float] = {}
    previous = starting_equity
    days = sorted(daily_equity)
    for day in days:
        value = float(daily_equity[day])
        if previous > 0:
            returns[day] = value / previous - 1.0
        previous = value
    if days and end is not None:
        cursor = datetime.fromisoformat(days[-1]).date() + timedelta(days=1)
        while cursor <= end.date():
            returns[cursor.isoformat()] = 0.0
            cursor += timedelta(days=1)
    return returns


def _fold_metrics_subset(metrics: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "net_return",
        "max_drawdown",
        "calmar",
        "sharpe",
        "profit_factor",
        "gross_profit",
        "gross_loss",
        "win_rate",
        "trades",
        "closed_lots",
        "fees_paid",
        "funding_paid",
        "slippage_paid",
        "exposure",
        "avg_utilization",
        "avg_exposure_ratio",
        "max_exposure_ratio",
        "max_unrealized_loss",
        "max_unrealized_loss_pct",
        "max_position_pct",
        "buy_fills",
        "sell_fills",
        "cycle_completion_ratio",
        "top_trade_share",
        "top5_trade_share",
        "recovery_days",
        "max_drawdown_duration_days",
        "recovered",
        "time_in_range",
        "time_outside_range",
        "stopped",
        "liquidated",
        "stop_reason",
        "trail_up_shifts",
        "trailing_tp_exits",
        "funding_trims",
        "funding_z_max",
        "buy_blocks",
        "hybrid_blocked_bars",
        "hybrid_exits",
        "rebalances",
        "adaptive_rebalances",
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
                    "segment": "oos",
                    "t": point["t"],
                    "equity": round(equity, 4),
                    "price": point.get("price"),
                    "low": point.get("low"),
                    "high": point.get("high"),
                    "dd": round(dd, 6),
                    "fold": fold["fold"],
                }
            )
        # A stopped fold is flat until its window ends; draw that explicitly
        # so the gap is not mistaken for missing data or the next segment.
        test_end = fold.get("test_end")
        if (
            result["metrics"].get("stopped")
            and test_end
            and combined
            and combined[-1]["fold"] == fold["fold"]
            and combined[-1]["t"] < test_end
        ):
            combined.append(
                {
                    **combined[-1],
                    "t": test_end,
                    "dd": round(1.0 - combined[-1]["equity"] / peak, 6) if peak > 0 else 0.0,
                    "flat": True,
                }
            )
        fold_return = float(result["metrics"].get("net_return", 0.0))
        cumulative *= 1.0 + fold_return

    if len(combined) > max_points:
        indices = set(np.linspace(0, len(combined) - 1, max_points).astype(int).tolist())
        # Keep every fold's final point so stops and fold ends survive
        # sampling, and the stop point in front of a flat extension, or the
        # drop would be smeared across the flat stretch.
        for index in range(len(combined)):
            if index == len(combined) - 1 or combined[index + 1]["fold"] != combined[index]["fold"]:
                indices.add(index)
            if combined[index].get("flat") and index > 0:
                indices.add(index - 1)
        combined = [combined[i] for i in sorted(indices)]
    return combined, max_dd


def run_walk_forward(
    rows: list[dict[str, Any]],
    funding_rows: list[dict[str, Any]],
    *,
    config: SimulationConfig,
    base_params: GridParams,
    lookback_days: int,
    min_trades: int,
    cost_stress_multiplier: float = 2.0,
    sensitivity: bool = True,
    gate: Gate | None = None,
    holdout_days: int = 0,
) -> WalkForwardResult:
    if len(rows) < 200:
        return WalkForwardResult(
            {}, {}, [], {}, {}, [], None, None, error="insufficient candle history"
        )

    all_rows = sorted(rows, key=lambda r: r["time"])
    data_start = all_rows[0]["time"]
    full_end = all_rows[-1]["time"]
    # The final ``holdout_days`` are never seen by parameter search, fold
    # scoring, or ranking. They are simulated once, with the parameters that
    # would be deployed, after everything else is fixed.
    holdout_start: datetime | None = None
    if holdout_days >= MIN_HOLDOUT_DAYS:
        candidate = full_end - timedelta(days=holdout_days)
        if (candidate - data_start).days >= 90 + 32:
            holdout_start = candidate
    rows = (
        [row for row in all_rows if row["time"] < holdout_start]
        if holdout_start is not None
        else all_rows
    )
    prepared = prepare_market(
        all_rows, funding_rows, base_params, config.bars_per_day
    )
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
    alt_fill_name, alt_fill_config = _alternate_fill_config(config)

    def run(
        sim_rows: list[dict[str, Any]],
        start: datetime,
        candidate: GridParams,
        sim_config: SimulationConfig = config,
    ) -> SimulationResult:
        return simulate_grid(
            sim_rows,
            funding_rows,
            start,
            candidate,
            sim_config,
            gate=gate,
            prepared=prepared,
        )

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
            result = run(train_rows, train_start, candidate)
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
        test_result = run(test_rows, test_start, best_params)
        if test_result.error:
            continue

        test_window = [
            row for row in rows if test_start <= row["time"] <= test_end
        ]
        regime = classify_regime(test_window, interval=config.interval)
        stress_result = run(
            test_rows,
            test_start,
            best_params,
            _stressed_config(config, cost_stress_multiplier),
        )
        alt_fill_result = run(test_rows, test_start, best_params, alt_fill_config)
        stress = (
            {
                "net_return": stress_result.metrics.get("net_return"),
                "max_drawdown": stress_result.metrics.get("max_drawdown"),
                "profit_factor": stress_result.metrics.get("profit_factor"),
                "trades": stress_result.metrics.get("trades"),
            }
            if not stress_result.error
            else {}
        )
        sensitivity_runs: list[dict[str, Any]] = []
        if sensitivity:
            for label, variant in _sensitivity_variants(best_params):
                variant_result = run(test_rows, test_start, variant)
                if variant_result.error:
                    continue
                sensitivity_runs.append(
                    {
                        "variant": label,
                        "net_return": round(
                            float(variant_result.metrics.get("net_return") or 0.0), 6
                        ),
                        "max_drawdown": round(
                            float(variant_result.metrics.get("max_drawdown") or 0.0), 6
                        ),
                    }
                )

        completed.append(
            (
                {"fold": fold_no + 1, "test_end": test_end.isoformat()},
                {
                    "metrics": test_result.metrics,
                    "equity_curve": test_result.equity_curve,
                    "trades": test_result.trades,
                    "grid_levels": test_result.grid_levels,
                    "daily_equity": test_result.daily_equity,
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
                "regime": regime,
                "stress": stress,
                "alt_fill": {
                    "model": alt_fill_name,
                    "net_return": (
                        round(float(alt_fill_result.metrics.get("net_return") or 0.0), 6)
                        if not alt_fill_result.error
                        else None
                    ),
                },
                "sensitivity": sensitivity_runs,
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
    fold_drawdowns = [
        float(f["test_metrics"].get("max_drawdown") or 0.0) for f in folds_out
    ]
    fold_return_std = (
        float(np.std(net_returns, ddof=1)) if len(net_returns) > 1 else 0.0
    )
    fold_drawdown_std = (
        float(np.std(fold_drawdowns, ddof=1)) if len(fold_drawdowns) > 1 else 0.0
    )
    worst_fold_drawdown = max(fold_drawdowns) if fold_drawdowns else 0.0

    stress_returns = [
        float(f["stress"].get("net_return") or 0.0)
        for f in folds_out
        if f.get("stress")
    ]
    stress_combined = (
        float(np.prod([1.0 + value for value in stress_returns]) - 1.0)
        if stress_returns
        else 0.0
    )
    stress_profitable = bool(stress_returns) and stress_combined > 0
    stress_drawdown = (
        max(
            float(f["stress"].get("max_drawdown") or 0.0)
            for f in folds_out
            if f.get("stress")
        )
        if stress_returns
        else 0.0
    )
    stress_ratio = (
        stress_combined / combined_return if combined_return > 0.0 else None
    )

    sensitivity_runs = [
        run for fold in folds_out for run in (fold.get("sensitivity") or [])
    ]
    neighbor_returns = [float(run.get("net_return") or 0.0) for run in sensitivity_runs]
    sensitivity_mean = (
        round(float(np.mean(neighbor_returns)), 6) if neighbor_returns else None
    )
    sensitivity_worst = (
        round(min(neighbor_returns), 6) if neighbor_returns else None
    )
    per_fold_degradation: list[float] = []
    for fold in folds_out:
        base = float(fold["test_metrics"].get("net_return") or 0.0)
        values = [
            float(run.get("net_return") or 0.0)
            for run in (fold.get("sensitivity") or [])
        ]
        if values:
            degradation = (base - float(np.mean(values))) / max(abs(base), 0.02)
            per_fold_degradation.append(max(0.0, min(2.0, degradation)))
    parameter_sensitivity = (
        round(float(np.mean(per_fold_degradation)), 6)
        if per_fold_degradation
        else None
    )

    def _max_metric(key: str) -> float:
        return max(
            float(f["test_metrics"].get(key) or 0.0) for f in folds_out
        )

    def _mean_metric(key: str) -> float:
        return float(
            np.mean([float(f["test_metrics"].get(key) or 0.0) for f in folds_out])
        )

    max_exposure_ratio = _max_metric("max_exposure_ratio")
    avg_exposure_ratio = _mean_metric("avg_exposure_ratio")
    max_unrealized_loss = _max_metric("max_unrealized_loss")
    max_unrealized_loss_pct = _max_metric("max_unrealized_loss_pct")
    max_recovery_days = _max_metric("recovery_days")
    max_drawdown_duration_days = _max_metric("max_drawdown_duration_days")
    top_trade_share = _max_metric("top_trade_share")
    top5_trade_share = _max_metric("top5_trade_share")
    buy_fills = sum(int(f["test_metrics"].get("buy_fills") or 0) for f in folds_out)
    sell_fills = sum(int(f["test_metrics"].get("sell_fills") or 0) for f in folds_out)
    slippage_paid = sum(
        float(f["test_metrics"].get("slippage_paid") or 0.0) for f in folds_out
    )
    cycle_completion_ratio = closed_lots / max(1, buy_fills)
    regime_analysis = regime_summary(folds_out)
    # Profit factor across all OOS trades. Averaging per-fold ratios lets a
    # single loss-free fold (capped at 99) dominate the result.
    gross_profit = sum(float(f["test_metrics"].get("gross_profit") or 0.0) for f in folds_out)
    gross_loss = sum(float(f["test_metrics"].get("gross_loss") or 0.0) for f in folds_out)
    if gross_loss > 1e-9:
        aggregate_pf = gross_profit / gross_loss
    else:
        aggregate_pf = 99.0 if gross_profit > 0 else 0.0
    alt_returns = [
        float(f["alt_fill"]["net_return"])
        for f in folds_out
        if (f.get("alt_fill") or {}).get("net_return") is not None
    ]
    alt_combined = (
        float(np.prod([1.0 + value for value in alt_returns]) - 1.0) if alt_returns else None
    )

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
    _, mode_count = param_counter.most_common(1)[0]
    # Deploy the parameters chosen on the most recent training window. The
    # modal set breaks ties by insertion order, which picked the oldest fold.
    recommended = dict(selected_param_dicts[-1])
    parameter_stability = mode_count / len(selected_param_dicts)

    # Explicit record of how the adaptive stop / take-profit distances were
    # chosen per fold, so the out-of-sample result can be audited against the
    # train-only selection that produced it.
    adaptive_selection = [
        {
            "fold": fold["fold"],
            "stop_atr_multiple": fold["params"].get("stop_atr_multiple"),
            "tp_atr_multiple": fold["params"].get("tp_atr_multiple"),
            "rebalance_mode": fold["params"].get("rebalance_mode"),
            "test_stop": fold["test_metrics"].get("stopped"),
            "test_tp_exits": fold["test_metrics"].get("trailing_tp_exits"),
        }
        for fold in folds_out
    ]
    adaptive_counter = Counter(
        (
            item["stop_atr_multiple"],
            item["tp_atr_multiple"],
        )
        for item in adaptive_selection
    )
    (mode_stop, mode_tp), mode_adaptive_count = adaptive_counter.most_common(1)[0]

    oos_metrics = {
        "net_return": round(combined_return, 6),
        "annualized_return": round(annual_return, 6),
        "max_drawdown": round(max_dd, 6),
        "calmar": round(calmar, 4),
        "sharpe": round(sharpe, 4),
        "sortino": round(sortino, 4),
        "profit_factor": round(aggregate_pf, 4),
        "gross_profit": round(gross_profit, 6),
        "gross_loss": round(gross_loss, 6),
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
        "time_outside_range": round(
            float(
                np.mean(
                    [f["test_metrics"].get("time_outside_range") or 0.0 for f in folds_out]
                )
            ),
            6,
        ),
        "rebalances": sum(int(f["test_metrics"].get("rebalances") or 0) for f in folds_out),
        "adaptive_rebalances": sum(
            int(f["test_metrics"].get("adaptive_rebalances") or 0) for f in folds_out
        ),
        "trail_up_shifts": sum(
            int(f["test_metrics"].get("trail_up_shifts") or 0) for f in folds_out
        ),
        "trailing_tp_exits": sum(
            int(f["test_metrics"].get("trailing_tp_exits") or 0) for f in folds_out
        ),
        "funding_trims": sum(
            int(f["test_metrics"].get("funding_trims") or 0) for f in folds_out
        ),
        "buy_blocks": sum(int(f["test_metrics"].get("buy_blocks") or 0) for f in folds_out),
        "hybrid_blocked_bars": sum(
            int(f["test_metrics"].get("hybrid_blocked_bars") or 0) for f in folds_out
        ),
        "hybrid_exits": sum(
            int(f["test_metrics"].get("hybrid_exits") or 0) for f in folds_out
        ),
        "funding_z_max": round(
            max(float(f["test_metrics"].get("funding_z_max") or 0.0) for f in folds_out),
            4,
        ),
        "fold_consistency": round(consistency, 4),
        "fold_returns": [round(r, 6) for r in net_returns],
        "worst_fold_return": round(min(net_returns), 6),
        "best_fold_return": round(max(net_returns), 6),
        "parameter_stability": round(parameter_stability, 4),
        "return_to_drawdown": round(combined_return / max(max_dd, 1e-6), 4),
        "fold_return_std": round(fold_return_std, 6),
        "fold_drawdown_mean": round(float(np.mean(fold_drawdowns)), 6),
        "fold_drawdown_std": round(fold_drawdown_std, 6),
        "worst_fold_drawdown": round(worst_fold_drawdown, 6),
        "cost_stress_return": round(stress_combined, 6),
        "cost_stress_max_drawdown": round(stress_drawdown, 6),
        "cost_stress_profitable": stress_profitable,
        "cost_stress_delta": round(stress_combined - combined_return, 6),
        "cost_stress_ratio": round(stress_ratio, 4) if stress_ratio is not None else None,
        "fill_model": "trade_through" if config.trade_through_bps > 0 else "touch",
        "alt_fill_model": alt_fill_name,
        "alt_fill_return": round(alt_combined, 6) if alt_combined is not None else None,
        "fill_model_delta": (
            round(alt_combined - combined_return, 6) if alt_combined is not None else None
        ),
        "parameter_sensitivity": parameter_sensitivity,
        "sensitivity_mean_return": sensitivity_mean,
        "sensitivity_worst_return": sensitivity_worst,
        "regime_robustness": regime_analysis["robustness_score"],
        "regimes_covered": regime_analysis["regimes_covered"],
        "dominant_regime": regime_analysis["dominant_regime"],
        "dominant_regime_share": regime_analysis["dominant_share"],
        "single_regime": regime_analysis["single_regime"],
        "max_exposure_ratio": round(max_exposure_ratio, 6),
        "avg_exposure_ratio": round(avg_exposure_ratio, 6),
        "max_unrealized_loss": round(max_unrealized_loss, 4),
        "max_unrealized_loss_pct": round(max_unrealized_loss_pct, 6),
        "recovery_days": round(max_recovery_days, 2),
        "max_drawdown_duration_days": round(max_drawdown_duration_days, 2),
        "top_trade_share": round(top_trade_share, 6),
        "top5_trade_share": round(top5_trade_share, 6),
        "slippage_paid": round(slippage_paid, 4),
        "buy_fills": buy_fills,
        "sell_fills": sell_fills,
        "cycle_completion_ratio": round(cycle_completion_ratio, 6),
        "folds": len(folds_out),
        "stopped": stopped,
        "liquidated": liquidated,
        "stop_reason": next(
            (f["test_metrics"].get("stop_reason") for f in folds_out if f["test_metrics"].get("stopped")),
            "",
        ),
        "test_days": round((test_end_last - test_start_first).total_seconds() / 86400, 1),
        "adaptive_selection": adaptive_selection,
        "recommended_stop_atr_multiple": mode_stop,
        "recommended_tp_atr_multiple": mode_tp,
        "recommended_rebalance_mode": recommended.get("rebalance_mode", "fixed"),
        "adaptive_stability": round(mode_adaptive_count / len(adaptive_selection), 4),
    }
    in_sample_metrics = {
        "avg_return": round(float(np.mean(is_returns)), 6) if is_returns else 0.0,
        "avg_max_drawdown": round(float(np.mean(is_drawdowns)), 6) if is_drawdowns else 0.0,
        "folds": len(is_returns),
        "selection_note": (
            "Grid shape, stop distance, trailing take-profit multiple, and "
            "fixed/adaptive rebalancing are optimized only on each fold's "
            "training window. Test windows are never used for parameter selection."
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
                "regime": f.get("regime") or {},
                "stress": f.get("stress") or {},
                "sensitivity": f.get("sensitivity") or [],
            }
            for f in folds_out
        ],
        "recommended_params": recommended,
        "adaptive_selection": adaptive_selection,
        "regime_analysis": regime_analysis,
    }
    diagnostics = {
        "regime": regime_analysis,
        "cost_stress": {
            "multiplier": cost_stress_multiplier,
            "stress_return": round(stress_combined, 6),
            "stress_max_drawdown": round(stress_drawdown, 6),
            "stress_delta": round(stress_combined - combined_return, 6),
            "profitable": stress_profitable,
        },
        "sensitivity": {
            "parameter_sensitivity": parameter_sensitivity,
            "mean_neighbor_return": sensitivity_mean,
            "worst_neighbor_return": sensitivity_worst,
            "runs": sensitivity_runs,
        },
        "fold_returns": [round(r, 6) for r in net_returns],
        "fold_drawdowns": [round(value, 6) for value in fold_drawdowns],
        "recovery_days": round(max_recovery_days, 2),
        "max_drawdown_duration_days": round(max_drawdown_duration_days, 2),
        "exposure": {
            "max_exposure_ratio": round(max_exposure_ratio, 6),
            "avg_exposure_ratio": round(avg_exposure_ratio, 6),
            "max_unrealized_loss": round(max_unrealized_loss, 4),
            "max_unrealized_loss_pct": round(max_unrealized_loss_pct, 6),
        },
        "execution": {
            "slippage_paid": round(slippage_paid, 4),
            "buy_fills": buy_fills,
            "sell_fills": sell_fills,
            "cycle_completion_ratio": round(cycle_completion_ratio, 6),
        },
        "return_stability": {
            "top_trade_share": round(top_trade_share, 6),
            "top5_trade_share": round(top5_trade_share, 6),
            "fold_return_std": round(fold_return_std, 6),
        },
    }
    strategy_returns: dict[str, float] = {}
    for fold, result in completed:
        fold_end = next(
            (
                datetime.fromisoformat(item["test_end"])
                for item in folds_out
                if item["fold"] == fold["fold"]
            ),
            None,
        )
        strategy_returns.update(
            daily_strategy_returns(
                result.get("daily_equity") or {}, config.capital, fold_end
            )
        )

    holdout_metrics: dict[str, Any] = {}
    if holdout_start is not None:
        deployed = GridParams.from_dict(recommended)
        holdout_result = simulate_grid(
            all_rows,
            funding_rows,
            holdout_start,
            deployed,
            config,
            gate=gate,
            prepared=prepared,
        )
        if not holdout_result.error:
            alt_holdout = simulate_grid(
                all_rows,
                funding_rows,
                holdout_start,
                deployed,
                alt_fill_config,
                gate=gate,
                prepared=prepared,
            )
            holdout_metrics = {
                **_fold_metrics_subset(holdout_result.metrics),
                "start": holdout_start.isoformat(),
                "end": full_end.isoformat(),
                "days": round((full_end - holdout_start).total_seconds() / 86400.0, 1),
                "alt_fill_model": alt_fill_name,
                "alt_fill_return": (
                    round(float(alt_holdout.metrics.get("net_return") or 0.0), 6)
                    if not alt_holdout.error
                    else None
                ),
                "params": recommended,
            }
            strategy_returns.update(
                daily_strategy_returns(holdout_result.daily_equity, config.capital, full_end)
            )
            # Draw the holdout after the stitched folds, continuing from their
            # final equity and tagged so the UI can set it apart. OOS metrics
            # stay fold-only; the holdout keeps its own metrics below.
            stitched_end = float(curve[-1]["equity"]) if curve else config.capital
            holdout_curve = holdout_result.equity_curve or []
            holdout_base = float(holdout_curve[0]["equity"]) if holdout_curve else 0.0
            if holdout_base > 0:
                chart["equity_curve"] = curve + [
                    {
                        "t": point["t"],
                        "equity": round(stitched_end * float(point["equity"]) / holdout_base, 4),
                        "price": point.get("price"),
                        "low": point.get("low"),
                        "high": point.get("high"),
                        "dd": point.get("dd"),
                        "segment": "holdout",
                    }
                    for point in holdout_curve
                ]
                if holdout_result.stopped and chart["equity_curve"][-1]["t"] < full_end.isoformat():
                    # A stopped strategy sits flat until the data ends.
                    chart["equity_curve"].append(
                        {**chart["equity_curve"][-1], "t": full_end.isoformat(), "dd": None}
                    )
            chart["holdout"] = {
                key: holdout_metrics.get(key)
                for key in (
                    "start",
                    "end",
                    "days",
                    "net_return",
                    "max_drawdown",
                    "profit_factor",
                    "trades",
                    "alt_fill_model",
                    "alt_fill_return",
                    "stopped",
                    "stop_reason",
                )
            }
    oos_metrics["holdout_return"] = holdout_metrics.get("net_return")
    oos_metrics["holdout_max_drawdown"] = holdout_metrics.get("max_drawdown")
    oos_metrics["holdout_days"] = holdout_metrics.get("days")
    diagnostics["holdout"] = holdout_metrics

    return WalkForwardResult(
        oos_metrics=oos_metrics,
        in_sample_metrics=in_sample_metrics,
        folds=folds_out,
        params=recommended,
        chart=chart,
        trade_sample=trade_sample,
        data_start=data_start,
        # The true end of the data, holdout included; ``data_end`` above is
        # only where the walk-forward folds stop.
        data_end=full_end,
        diagnostics=diagnostics,
        holdout_metrics=holdout_metrics,
        strategy_returns=strategy_returns,
    )


def walk_forward_job(kwargs: dict[str, Any]) -> WalkForwardResult:
    """Process-pool entry point.

    Lives here, not in ``analysis``, because worker processes import the
    function's module: this one is pure numpy and needs no Django setup.
    """

    return run_walk_forward(**kwargs)
