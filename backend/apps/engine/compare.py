"""Holdout comparison of the grid configurations requested for tuning.

Every variant runs on the same untouched tail window. The window starts after
the warmup needed by the longest indicator, so no variant sees a different
sample or leaks data from before its own start.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any

from apps.engine.grid import GridParams, SimulationConfig, simulate_grid

COMPARISON_LEVELS = (10, 15, 20)
COMPARISON_ATR_MULTIPLIERS = (2.0, 3.0, 4.0)
COMPARISON_REBALANCE_MODES = ("fixed", "adaptive")
COMPARISON_RANGE_MODES = ("percentile", "price_centered", "atr_support")
COMPARISON_DRAWDOWN_LIMITS = (0.05, 0.10, 0.15)
MIN_HOLDOUT_DAYS = 30
WARMUP_DAYS = 32


@dataclass(frozen=True)
class Variant:
    group: str
    label: str
    params: GridParams


def comparison_variants(base: GridParams) -> list[Variant]:
    """The requested matrix: grid shape x ATR pad x rebalance mode, then
    range construction and portfolio drawdown limits as separate groups."""

    variants: list[Variant] = []
    for levels in COMPARISON_LEVELS:
        for atr_multiple in COMPARISON_ATR_MULTIPLIERS:
            for mode in COMPARISON_REBALANCE_MODES:
                variants.append(
                    Variant(
                        group="levels_atr_rebalance",
                        label=f"{levels} levels / ATR x{atr_multiple:g} / {mode}",
                        params=replace(
                            base,
                            grid_levels=levels,
                            atr_multiplier=atr_multiple,
                            rebalance_mode=mode,
                        ),
                    )
                )
    for mode in COMPARISON_RANGE_MODES:
        variants.append(
            Variant(
                group="range_mode",
                label=mode,
                params=replace(base, range_mode=mode),
            )
        )
    for limit in COMPARISON_DRAWDOWN_LIMITS:
        variants.append(
            Variant(
                group="drawdown_limit",
                label=f"{limit:.0%} portfolio drawdown stop",
                params=replace(base, max_total_drawdown=limit),
            )
        )
    return variants


def _row(result, capital: float) -> dict[str, Any]:
    metrics = result.metrics or {}
    return {
        "net_return": metrics.get("net_return"),
        "net_pnl": round(float(metrics.get("final_equity") or capital) - capital, 4),
        "max_drawdown": metrics.get("max_drawdown"),
        "calmar": metrics.get("calmar"),
        "profit_factor": metrics.get("profit_factor"),
        "trades": metrics.get("trades", 0),
        "closed_lots": metrics.get("closed_lots", 0),
        "fees_paid": metrics.get("fees_paid"),
        "funding_paid": metrics.get("funding_paid"),
        "time_in_range": metrics.get("time_in_range"),
        "time_outside_range": metrics.get("time_outside_range"),
        "rebalances": metrics.get("rebalances", 0),
        "adaptive_rebalances": metrics.get("adaptive_rebalances", 0),
        "stopped": metrics.get("stopped", False),
        "liquidated": metrics.get("liquidated", False),
        "stop_reason": metrics.get("stop_reason", ""),
    }


def compare_grid_variants(
    rows: list[dict[str, Any]],
    funding_rows: list[dict[str, Any]],
    *,
    config: SimulationConfig,
    base_params: GridParams,
    holdout_days: int = 90,
) -> dict[str, Any]:
    """Run the comparison matrix on the same holdout window."""

    rows = sorted(rows, key=lambda row: row["time"])
    if len(rows) < 200:
        return {"error": "insufficient candle history", "groups": {}, "rows": []}

    data_start = rows[0]["time"]
    data_end = rows[-1]["time"]
    earliest = data_start + timedelta(days=WARMUP_DAYS)
    holdout_start = max(earliest, data_end - timedelta(days=max(1, holdout_days)))
    window_days = (data_end - holdout_start).total_seconds() / 86400.0
    if window_days < MIN_HOLDOUT_DAYS:
        return {
            "error": f"only {window_days:.0f} holdout days; need at least {MIN_HOLDOUT_DAYS}",
            "groups": {},
            "rows": [],
        }

    results: list[dict[str, Any]] = []
    for variant in comparison_variants(base_params):
        result = simulate_grid(rows, funding_rows, holdout_start, variant.params, config)
        entry = {
            "group": variant.group,
            "label": variant.label,
            "params": variant.params.to_dict(),
            "error": result.error,
        }
        entry.update(_row(result, config.capital) if not result.error else {})
        results.append(entry)

    groups: dict[str, list[dict[str, Any]]] = {}
    for entry in results:
        if entry.get("error"):
            continue
        groups.setdefault(entry["group"], []).append(entry)
    for group_rows in groups.values():
        group_rows.sort(key=lambda item: item.get("net_return") or 0.0, reverse=True)
        for rank, entry in enumerate(group_rows, start=1):
            entry["rank"] = rank

    best = {
        group: {
            "label": group_rows[0]["label"],
            "net_return": group_rows[0]["net_return"],
            "max_drawdown": group_rows[0]["max_drawdown"],
        }
        for group, group_rows in groups.items()
        if group_rows
    }
    return {
        "error": "",
        "holdout_start": holdout_start.isoformat(),
        "holdout_end": data_end.isoformat(),
        "holdout_days": round(window_days, 1),
        "capital": config.capital,
        "groups": groups,
        "best": best,
        "rows": results,
    }
