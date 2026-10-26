"""Cross-sectional scoring and ranking for grid suitability."""

from __future__ import annotations

from typing import Any, Iterable

import numpy as np

from apps.engine.models import CoinAnalysis


def _numeric(value: Any) -> float | None:
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)) and np.isfinite(value):
        return float(value)
    return None


def _percentiles(items: list[dict[str, Any]], key: str, higher_better: bool = True) -> dict[int, float]:
    """Return robust percentile ranks (0-100) keyed by result index."""

    values: list[tuple[int, float]] = []
    for idx, item in enumerate(items):
        value = _numeric(item.get("_score_source", {}).get(key))
        if value is not None:
            values.append((idx, value))
    if not values:
        return {}
    ordered = sorted(values, key=lambda pair: pair[1])
    count = len(ordered)
    result: dict[int, float] = {}
    position = 0
    while position < count:
        end = position
        while end + 1 < count and ordered[end + 1][1] == ordered[position][1]:
            end += 1
        average_rank = (position + end) / 2.0
        pct = 100.0 * average_rank / max(1, count - 1)
        if not higher_better:
            pct = 100.0 - pct
        for k in range(position, end + 1):
            result[ordered[k][0]] = pct
        position = end + 1
    return result


def _weighted(parts: Iterable[tuple[float | None, float]], default: float = 40.0) -> float:
    total_weight = 0.0
    total = 0.0
    for value, weight in parts:
        if value is None:
            continue
        total += value * weight
        total_weight += weight
    if total_weight <= 0:
        return default
    return total / total_weight


def score_results(
    results: list[dict[str, Any]],
    *,
    min_adv_usd: float,
    min_trades: int,
    max_drawdown: float,
) -> list[dict[str, Any]]:
    """Assign component scores, eligibility gates, and ranks in place."""

    for item in results:
        oos = item.get("oos_metrics") or {}
        features = item.get("features") or {}
        folds = item.get("folds") or []
        item["_score_source"] = {
            **features,
            **oos,
            "fold_consistency": oos.get("fold_consistency"),
            "worst_fold_return": oos.get("worst_fold_return"),
            "parameter_stability": oos.get("parameter_stability"),
            "no_stop": 0.0 if oos.get("stopped") or oos.get("liquidated") else 1.0,
            "funding_long_cost": features.get("funding_long_annual_cost"),
            "cost_to_edge": features.get("cost_to_edge"),
            "adv_usd_median": features.get("adv_usd_median"),
            "volume_cv": features.get("volume_cv"),
            "max_drawdown": oos.get("max_drawdown"),
            "net_return": oos.get("net_return"),
            "calmar": oos.get("calmar"),
            "profit_factor": oos.get("profit_factor"),
            "win_rate": oos.get("win_rate"),
            "time_in_range": oos.get("time_in_range"),
            "trades": oos.get("trades"),
            "mean_reversion_score": features.get("mean_reversion_score"),
            "range_stability_score": features.get("range_stability_score"),
        }

    percentiles = {
        "net_return": _percentiles(results, "net_return", True),
        "calmar": _percentiles(results, "calmar", True),
        "profit_factor": _percentiles(results, "profit_factor", True),
        "win_rate": _percentiles(results, "win_rate", True),
        "max_drawdown": _percentiles(results, "max_drawdown", False),
        "fold_consistency": _percentiles(results, "fold_consistency", True),
        "worst_fold_return": _percentiles(results, "worst_fold_return", True),
        "no_stop": _percentiles(results, "no_stop", True),
        "mean_reversion_score": _percentiles(results, "mean_reversion_score", True),
        "range_stability_score": _percentiles(results, "range_stability_score", True),
        "parameter_stability": _percentiles(results, "parameter_stability", True),
        "time_in_range": _percentiles(results, "time_in_range", True),
        "adv_usd_median": _percentiles(results, "adv_usd_median", True),
        "cost_to_edge": _percentiles(results, "cost_to_edge", False),
        "funding_long_cost": _percentiles(results, "funding_long_cost", False),
        "volume_cv": _percentiles(results, "volume_cv", False),
    }

    for idx, item in enumerate(results):
        oos = item.get("oos_metrics") or {}
        features = item.get("features") or {}
        folds = item.get("folds") or []
        source = item["_score_source"]

        def p(name: str) -> float | None:
            return percentiles.get(name, {}).get(idx)

        profitability = _weighted(
            [
                (p("net_return"), 0.45),
                (p("calmar"), 0.30),
                (p("profit_factor"), 0.15),
                (p("win_rate"), 0.10),
            ]
        )
        survivability = _weighted(
            [
                (p("max_drawdown"), 0.35),
                (p("fold_consistency"), 0.25),
                (p("worst_fold_return"), 0.15),
                (p("no_stop"), 0.15),
                (p("funding_long_cost"), 0.10),
            ]
        )
        stability = _weighted(
            [
                (p("mean_reversion_score"), 0.40),
                (p("range_stability_score"), 0.30),
                (p("parameter_stability"), 0.20),
                (p("time_in_range"), 0.10),
            ]
        )
        execution = _weighted(
            [
                (p("adv_usd_median"), 0.40),
                (p("cost_to_edge"), 0.30),
                (p("funding_long_cost"), 0.15),
                (p("volume_cv"), 0.15),
            ]
        )
        score = (
            0.35 * profitability
            + 0.30 * survivability
            + 0.20 * stability
            + 0.15 * execution
        )

        reasons: list[str] = []
        if not item.get("eligible", True) and item.get("exclusion_reason"):
            reasons.append(item["exclusion_reason"])
        if not features or features.get("insufficient_data"):
            reasons.append("insufficient candle history")
        if not folds:
            reasons.append("no completed walk-forward folds")
        adv = _numeric(features.get("adv_usd_median")) or 0.0
        if adv < min_adv_usd:
            reasons.append(f"24h liquidity below ${min_adv_usd:,.0f}")
        trades = int(_numeric(oos.get("trades")) or 0)
        if trades < min_trades:
            reasons.append(f"only {trades} OOS fills (minimum {min_trades})")
        net = _numeric(oos.get("net_return"))
        if net is None or net <= 0:
            reasons.append("out-of-sample net return is not positive")
        dd = _numeric(oos.get("max_drawdown"))
        if dd is None or dd > max_drawdown:
            reasons.append(f"OOS drawdown exceeds {max_drawdown:.0%}")
        consistency = _numeric(oos.get("fold_consistency"))
        if consistency is None or consistency < 0.5:
            reasons.append("less than half of OOS folds were profitable")
        if oos.get("liquidated") or oos.get("stopped"):
            reasons.append("stop-out or liquidation occurred out of sample")
        if features.get("data_days") is not None and features["data_days"] < 120:
            reasons.append("less than 120 days of history")

        item["eligible"] = len(reasons) == 0
        item["eligibility_reasons"] = reasons
        item["exclusion_reason"] = "; ".join(reasons[:2]) if reasons else ""

        if oos.get("liquidated") or oos.get("stopped"):
            score -= 15.0
        if consistency is not None and consistency < 0.5:
            score -= 8.0
        if trades < min_trades:
            score -= 5.0
        score = max(0.0, min(100.0, score))

        item["component_scores"] = {
            "profitability": round(profitability, 2),
            "survivability": round(survivability, 2),
            "stability": round(stability, 2),
            "execution": round(execution, 2),
        }
        item["score"] = round(score, 2)
        if not item["eligible"]:
            item["suitability"] = CoinAnalysis.Suitability.INELIGIBLE
        elif score >= 75:
            item["suitability"] = CoinAnalysis.Suitability.PRIME
        elif score >= 60:
            item["suitability"] = CoinAnalysis.Suitability.VIABLE
        elif score >= 45:
            item["suitability"] = CoinAnalysis.Suitability.WATCH
        else:
            item["suitability"] = CoinAnalysis.Suitability.AVOID
        item["confidence"] = round(
            min(1.0, 0.25 + 0.15 * len(folds) + min(trades, 60) / 150.0), 3
        )

    ranked = sorted(
        results,
        key=lambda x: (
            1 if x.get("eligible") else 0,
            x.get("score", 0.0),
            x.get("oos_metrics", {}).get("net_return", 0.0),
        ),
        reverse=True,
    )
    for rank, item in enumerate(ranked, start=1):
        item["rank"] = rank
    return ranked


def ranking_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    eligible = [r for r in results if r.get("eligible")]
    return {
        "universe": len(results),
        "eligible": len(eligible),
        "prime": sum(1 for r in results if r.get("suitability") == "prime"),
        "viable": sum(1 for r in results if r.get("suitability") == "viable"),
        "watch": sum(1 for r in results if r.get("suitability") == "watch"),
        "avoid": sum(1 for r in results if r.get("suitability") == "avoid"),
        "ineligible": sum(1 for r in results if r.get("suitability") == "ineligible"),
        "top": [
            {
                "rank": r.get("rank"),
                "symbol": r["coin"].symbol,
                "score": r.get("score"),
                "suitability": r.get("suitability"),
            }
            for r in results[:10]
        ],
    }
