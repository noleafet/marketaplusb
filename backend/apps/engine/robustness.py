"""Regime, cost-stress, sensitivity, and correlation diagnostics.

These helpers describe *why* an out-of-sample result should or should not be
trusted. They deliberately do not pick parameters: parameter selection stays in
the walk-forward module and only sees each fold's training window.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from apps.engine.features import adx, range_containment, trend_fit
from apps.engine.grid import BARS_PER_DAY

REGIMES = ("trending", "ranging", "high_volatility", "mixed", "unknown")
HIGH_VOL_ANNUAL = 1.2
TREND_ADX = 25.0
TREND_R2 = 0.65
TREND_SLOPE = 0.5
RANGING_ADX = 20.0
RANGING_CONTAINMENT = 0.55


def classify_regime(
    rows: list[dict[str, Any]],
    *,
    interval: str = "4h",
    high_vol_annual: float = HIGH_VOL_ANNUAL,
) -> dict[str, Any]:
    """Label a window using only the observations inside that window."""

    if len(rows) < 60:
        return {
            "label": "unknown",
            "bars": len(rows),
            "reason": "not enough bars to classify",
        }
    bpd = BARS_PER_DAY.get(interval, 6)
    h = np.array([row["high"] for row in rows], dtype=float)
    low = np.array([row["low"] for row in rows], dtype=float)
    c = np.array([row["close"] for row in rows], dtype=float)
    log_ret = np.diff(np.log(np.maximum(c, 1e-12)))
    annual_vol = (
        float(np.std(log_ret, ddof=1) * math.sqrt(bpd * 365))
        if log_ret.size > 2
        else None
    )
    adx_value = adx(h, low, c)
    slope, r2 = trend_fit(c, min(c.size, 90 * bpd))
    annual_slope = (
        (math.exp(slope * bpd * 365) - 1.0)
        if slope is not None and abs(slope) < 5
        else None
    )
    containment = range_containment(
        h, low, c, window=max(3, 5 * bpd), step=max(1, 2 * bpd)
    )

    trending = bool(
        (adx_value is not None and adx_value >= TREND_ADX)
        or (
            r2 is not None
            and annual_slope is not None
            and r2 >= TREND_R2
            and abs(annual_slope) >= TREND_SLOPE
        )
    )
    if trending:
        label = "trending"
    elif annual_vol is not None and annual_vol >= high_vol_annual:
        label = "high_volatility"
    elif (
        containment is not None
        and containment >= RANGING_CONTAINMENT
        and (adx_value is None or adx_value < RANGING_ADX)
    ):
        label = "ranging"
    else:
        label = "mixed"

    return {
        "label": label,
        "bars": len(rows),
        "annual_vol": round(annual_vol, 4) if annual_vol is not None else None,
        "adx": round(adx_value, 2) if adx_value is not None else None,
        "trend_r2": round(r2, 4) if r2 is not None else None,
        "annual_slope": round(annual_slope, 4) if annual_slope is not None else None,
        "range_containment": (
            round(containment, 4) if containment is not None else None
        ),
    }


def regime_summary(folds: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate per-fold results by regime and score regime robustness."""

    buckets: dict[str, dict[str, Any]] = {
        label: {"folds": 0, "returns": [], "drawdowns": []} for label in REGIMES
    }
    for fold in folds:
        label = (fold.get("regime") or {}).get("label") or "unknown"
        bucket = buckets.setdefault(
            label, {"folds": 0, "returns": [], "drawdowns": []}
        )
        metrics = fold.get("test_metrics") or {}
        bucket["folds"] += 1
        bucket["returns"].append(float(metrics.get("net_return") or 0.0))
        bucket["drawdowns"].append(float(metrics.get("max_drawdown") or 0.0))

    tested = {
        label: bucket
        for label, bucket in buckets.items()
        if label != "unknown" and bucket["folds"] > 0
    }
    for label, bucket in buckets.items():
        returns = bucket["returns"]
        bucket["mean_return"] = round(float(np.mean(returns)), 6) if returns else 0.0
        bucket["mean_drawdown"] = (
            round(float(np.mean(bucket["drawdowns"])), 6)
            if bucket["drawdowns"]
            else 0.0
        )
        bucket["profitable"] = (
            sum(1 for value in returns if value > 0) / len(returns)
            if returns
            else 0.0
        )

    positive_contributions = {
        label: sum(max(0.0, value) for value in bucket["returns"])
        for label, bucket in tested.items()
    }
    positive_total = sum(positive_contributions.values())
    dominant_regime = (
        max(positive_contributions, key=positive_contributions.get)
        if positive_total > 0
        else (next(iter(tested)) if tested else "unknown")
    )
    dominant_share = (
        positive_contributions.get(dominant_regime, 0.0) / positive_total
        if positive_total > 0
        else 0.0
    )

    profitable_regimes = sum(
        1 for bucket in tested.values() if bucket["mean_return"] > 0
    )
    profitable_share = profitable_regimes / len(tested) if tested else 0.0
    coverage = min(1.0, len(tested) / 3.0)
    dependence_penalty = 15.0 if dominant_share >= 0.9 and len(tested) > 1 else 0.0
    robustness = max(
        0.0,
        min(
            100.0,
            45.0 * profitable_share + 35.0 * coverage + 20.0 * (1.0 - dominant_share)
            - dependence_penalty,
        ),
    )
    if not tested:
        robustness = 0.0

    return {
        "regimes": tested,
        "regimes_covered": len(tested),
        "labels": sorted(tested),
        "profitable_share": round(profitable_share, 4),
        "dominant_regime": dominant_regime,
        "dominant_share": round(dominant_share, 4),
        "dependence_penalty": dependence_penalty,
        "robustness_score": round(robustness, 2),
        "single_regime": len(tested) < 2,
    }


def daily_returns(rows: list[dict[str, Any]]) -> dict[str, float]:
    """Day-close to day-close returns keyed by ISO date."""

    day_close: dict[str, float] = {}
    for row in sorted(rows, key=lambda row: row["time"]):
        day_close[row["time"].date().isoformat()] = float(row["close"])
    days = sorted(day_close)
    return {
        days[index]: day_close[days[index]] / day_close[days[index - 1]] - 1.0
        for index in range(1, len(days))
        if day_close[days[index - 1]] > 0
    }


def _pair_correlation(
    left: dict[str, float], right: dict[str, float], lookback: int
) -> float | None:
    dates = sorted(set(left) & set(right))[-lookback:]
    if len(dates) < 10:
        return None
    x = np.array([left[day] for day in dates], dtype=float)
    y = np.array([right[day] for day in dates], dtype=float)
    if np.std(x, ddof=1) <= 1e-12 or np.std(y, ddof=1) <= 1e-12:
        return None
    return float(np.corrcoef(x, y)[0, 1])


def correlation_diagnostics(
    results: list[dict[str, Any]],
    *,
    lookback_days: int = 60,
    max_corr: float = 0.85,
) -> dict[str, Any]:
    """Attach rolling-return correlation diagnostics to each result.

    Correlations use daily closes aligned on common dates. Only eligible coins
    contribute, so an ineligible coin cannot change the ranking penalty.
    """

    pool = [
        item
        for item in results
        if item.get("eligible", True) and item.get("_daily_returns")
    ]
    for item in results:
        item["_correlation"] = {}
    pairs: list[dict[str, Any]] = []
    for index, left in enumerate(pool):
        left_returns = left["_daily_returns"]
        peers: dict[str, float] = {}
        for right in pool[index + 1 :]:
            corr = _pair_correlation(
                left_returns, right["_daily_returns"], lookback_days
            )
            if corr is None:
                continue
            left_symbol = left["coin"].symbol
            right_symbol = right["coin"].symbol
            peers[right_symbol] = corr
            right["_correlation"].setdefault("peer_map", {})[left_symbol] = corr
            pairs.append(
                {
                    "a": left_symbol,
                    "b": right_symbol,
                    "correlation": round(corr, 4),
                }
            )
        if peers:
            left["_correlation"]["peer_map"] = peers

    for item in pool:
        peer_map = item["_correlation"].get("peer_map") or {}
        values = list(peer_map.values())
        top = sorted(peer_map.items(), key=lambda pair: pair[1], reverse=True)[:5]
        item["_correlation"].update(
            {
                "avg_correlation": (
                    round(float(np.mean(values)), 4) if values else None
                ),
                "max_correlation": round(max(values), 4) if values else None,
                "peers": [
                    {"symbol": symbol, "correlation": round(value, 4)}
                    for symbol, value in top
                ],
                "exceeds_limit": bool(values and max(values) >= max_corr),
            }
        )

    pairs.sort(key=lambda pair: pair["correlation"], reverse=True)
    averages = [
        item["_correlation"]["avg_correlation"]
        for item in pool
        if item["_correlation"].get("avg_correlation") is not None
    ]
    return {
        "coins": len(pool),
        "lookback_days": lookback_days,
        "threshold": max_corr,
        "avg_pair_correlation": (
            round(float(np.mean(averages)), 4) if averages else None
        ),
        "max_pair_correlation": pairs[0]["correlation"] if pairs else None,
        "high_pairs": [pair for pair in pairs if pair["correlation"] >= max_corr][
            :20
        ],
        "pairs": pairs[:50],
    }


def correlation_penalty(item: dict[str, Any], *, max_corr: float) -> tuple[float, str]:
    """Score points removed for a coin that duplicates existing exposure."""

    stats = item.get("_correlation") or {}
    avg = stats.get("avg_correlation")
    peak = stats.get("max_correlation")
    if peak is None or peak <= max_corr:
        return 0.0, ""
    # A coin that moves almost perfectly with the rest of the book adds little
    # diversification, even when its own grid result is strong.
    severity = min(1.0, (peak - max_corr) / max(1e-6, 1.0 - max_corr))
    avg_component = 0.0
    if avg is not None and avg > max_corr * 0.75:
        avg_component = min(1.0, (avg - max_corr * 0.75) / max_corr)
    points = 2.0 + 8.0 * severity + 4.0 * avg_component
    return round(points, 2), f"high correlation (peak {peak:.2f}, avg {avg or 0:.2f})"


def select_diversified(
    items: list[dict[str, Any]], *, max_corr: float, limit: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Greedy selection that refuses a coin highly correlated with one already picked."""

    selected: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for item in items:
        if len(selected) >= limit:
            break
        peers = (item.get("_correlation") or {}).get("peer_map") or {}
        conflict = next(
            (
                existing["coin"].symbol
                for existing in selected
                if peers.get(existing["coin"].symbol, 0.0) >= max_corr
            ),
            None,
        )
        if conflict:
            skipped.append(
                {
                    "symbol": item["coin"].symbol,
                    "correlated_with": conflict,
                    "correlation": round(peers[conflict], 4),
                }
            )
            continue
        selected.append(item)
    return selected, skipped
