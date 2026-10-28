"""Cross-sectional scoring and ranking for grid suitability.

Hard eligibility gates are evaluated first. Only eligible coins receive a rank;
ineligible coins are still scored and returned so the dashboard can explain why
they were rejected. Ranking factors are deliberately grouped so correlated
measurements (for example return and return/drawdown) are not counted twice.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Iterable

import numpy as np

from apps.engine.models import CoinAnalysis
from apps.engine.robustness import correlation_diagnostics, correlation_penalty

FACTOR_WEIGHTS = {
    "profitability": 0.18,
    "risk_adjusted": 0.18,
    "drawdown_control": 0.15,
    "consistency": 0.14,
    "regime": 0.10,
    "stability": 0.10,
    "execution": 0.10,
    "diversification": 0.05,
}

METHODOLOGY_VERSION = 2


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


def _prepare_score_source(item: dict[str, Any]) -> None:
    oos = item.get("oos_metrics") or {}
    features = item.get("features") or {}
    correlation = item.get("_correlation") or {}
    net = _numeric(oos.get("net_return")) or 0.0
    dd = _numeric(oos.get("max_drawdown")) or 0.0
    item["_score_source"] = {
        **features,
        **oos,
        "no_stop": 0.0 if oos.get("stopped") or oos.get("liquidated") else 1.0,
        "funding_long_cost": features.get("funding_long_annual_cost"),
        "cost_to_edge": features.get("cost_to_edge"),
        "adv_usd_median": features.get("adv_usd_median"),
        "volume_cv": features.get("volume_cv"),
        "spread_bps": features.get("spread_bps"),
        "depth_cover_ratio": features.get("depth_cover_ratio"),
        "max_drawdown": oos.get("max_drawdown"),
        "net_return": oos.get("net_return"),
        "return_to_drawdown": oos.get("return_to_drawdown", net / max(dd, 1e-6)),
        "calmar": oos.get("calmar"),
        "profit_factor": oos.get("profit_factor"),
        "win_rate": oos.get("win_rate"),
        "fold_consistency": oos.get("fold_consistency"),
        "worst_fold_return": oos.get("worst_fold_return"),
        "fold_return_std": oos.get("fold_return_std"),
        "parameter_stability": oos.get("parameter_stability"),
        "parameter_sensitivity": oos.get("parameter_sensitivity"),
        "regime_robustness": oos.get("regime_robustness"),
        "time_in_range": oos.get("time_in_range"),
        "recovery_days": oos.get("recovery_days"),
        "max_unrealized_loss_pct": oos.get("max_unrealized_loss_pct"),
        "cost_stress_return": oos.get("cost_stress_return"),
        "top_trade_share": oos.get("top_trade_share"),
        "trades": oos.get("trades"),
        "mean_reversion_score": features.get("mean_reversion_score"),
        "range_stability_score": features.get("range_stability_score"),
        "avg_correlation": correlation.get("avg_correlation"),
    }


def _gate(
    reasons: list[str],
    *,
    name: str,
    label: str,
    passed: bool,
    value: Any,
    threshold: Any,
    reason: str = "",
) -> dict[str, Any]:
    if not passed and reason:
        reasons.append(reason)
    return {
        "name": name,
        "label": label,
        "passed": bool(passed),
        "value": value,
        "threshold": threshold,
        "reason": "" if passed else reason,
    }


def _pf_validation(
    oos: dict[str, Any],
    *,
    min_trades: int,
    pf_review: float,
    pf_validate: float,
) -> dict[str, Any]:
    pf = _numeric(oos.get("profit_factor")) or 0.0
    trades = int(_numeric(oos.get("trades")) or 0)
    stress_ok = oos.get("cost_stress_profitable")
    stress_checked = stress_ok is not None
    enough_trades = trades >= max(min_trades * 2, 24)
    no_stop = not (oos.get("stopped") or oos.get("liquidated"))
    validation_passed = enough_trades and no_stop and (
        bool(stress_ok) if stress_checked else True
    )
    return {
        "review_required": pf > pf_review,
        "validation_required": pf > pf_validate,
        "validation_passed": validation_passed,
        "enough_trades": enough_trades,
        "stress_checked": stress_checked,
        "stress_profitable": stress_ok,
        "no_stop": no_stop,
        "review_threshold": pf_review,
        "validation_threshold": pf_validate,
    }


def _evaluate_item_gates(
    item: dict[str, Any],
    *,
    min_adv_usd: float,
    min_trades: int,
    max_drawdown: float,
    min_atr_pct: float,
    max_atr_pct: float,
    pf_review: float,
    pf_validate: float,
    max_spread_bps: float,
    min_depth_cover: float,
    enforce_cost_stress: bool,
    min_stressed_return: float,
    min_data_days: int,
) -> None:
    """Evaluate hard eligibility gates and store them on the item.

    Gates only read raw metrics, so they can run before correlation (which in
    turn only considers coins that passed these gates).
    """

    oos = item.get("oos_metrics") or {}
    features = item.get("features") or {}
    folds = item.get("folds") or []
    reasons: list[str] = []
    gates: list[dict[str, Any]] = []

    if not item.get("eligible", True) and item.get("exclusion_reason"):
        reasons.append(item["exclusion_reason"])
    data_days = _numeric(features.get("data_days"))
    gates.append(
        _gate(
            reasons,
            name="history",
            label="History",
            passed=data_days is not None and data_days >= min_data_days,
            value=round(data_days, 1) if data_days is not None else None,
            threshold=min_data_days,
            reason=(
                "less than 120 days of history"
                if min_data_days == 120
                else f"less than {min_data_days} days of history"
            ),
        )
    )
    if not features or features.get("insufficient_data"):
        reasons.append("insufficient candle history")
    gates.append(
        _gate(
            reasons,
            name="folds",
            label="Walk-forward folds",
            passed=bool(folds),
            value=len(folds),
            threshold=1,
            reason="no completed walk-forward folds",
        )
    )
    adv = _numeric(features.get("adv_usd_median")) or 0.0
    gates.append(
        _gate(
            reasons,
            name="liquidity",
            label="Median 24h liquidity",
            passed=adv >= min_adv_usd,
            value=round(adv, 2),
            threshold=min_adv_usd,
            reason=f"24h liquidity below ${min_adv_usd:,.0f}",
        )
    )
    trades = int(_numeric(oos.get("trades")) or 0)
    gates.append(
        _gate(
            reasons,
            name="trades",
            label="OOS fills",
            passed=trades >= min_trades,
            value=trades,
            threshold=min_trades,
            reason=f"only {trades} OOS fills (minimum {min_trades})",
        )
    )
    net = _numeric(oos.get("net_return"))
    gates.append(
        _gate(
            reasons,
            name="positive_return",
            label="Positive OOS net return",
            passed=net is not None and net > 0,
            value=round(net, 6) if net is not None else None,
            threshold=0.0,
            reason="out-of-sample net return is not positive",
        )
    )
    dd = _numeric(oos.get("max_drawdown"))
    gates.append(
        _gate(
            reasons,
            name="drawdown",
            label="OOS max drawdown",
            passed=dd is not None and dd <= max_drawdown,
            value=round(dd, 6) if dd is not None else None,
            threshold=max_drawdown,
            reason=f"OOS drawdown exceeds {max_drawdown:.0%}",
        )
    )
    consistency = _numeric(oos.get("fold_consistency"))
    gates.append(
        _gate(
            reasons,
            name="fold_consistency",
            label="Profitable folds",
            passed=consistency is not None and consistency >= 0.5,
            value=round(consistency, 4) if consistency is not None else None,
            threshold=0.5,
            reason="less than half of OOS folds were profitable",
        )
    )
    stop_hit = bool(oos.get("liquidated") or oos.get("stopped"))
    gates.append(
        _gate(
            reasons,
            name="no_stop",
            label="No stop-out",
            passed=not stop_hit,
            value=not stop_hit,
            threshold=True,
            reason="stop-out or liquidation occurred out of sample",
        )
    )

    atr_pct = _numeric(features.get("atr_pct"))
    atr_ok = True
    atr_reason = ""
    if atr_pct is not None and max_atr_pct > 0 and atr_pct > max_atr_pct:
        atr_ok = False
        atr_reason = f"ATR% {atr_pct:.2%} above the {max_atr_pct:.2%} volatility ceiling"
    elif atr_pct is not None and min_atr_pct > 0 and atr_pct < min_atr_pct:
        atr_ok = False
        atr_reason = f"ATR% {atr_pct:.2%} below the {min_atr_pct:.2%} volatility floor"
    gates.append(
        _gate(
            reasons,
            name="atr",
            label="ATR% band",
            passed=atr_ok,
            value=round(atr_pct, 6) if atr_pct is not None else None,
            threshold={"min": min_atr_pct, "max": max_atr_pct},
            reason=atr_reason,
        )
    )

    spread = _numeric(features.get("spread_bps"))
    spread_ok = spread is None or max_spread_bps <= 0 or spread <= max_spread_bps
    gates.append(
        _gate(
            reasons,
            name="spread",
            label="Bid-ask spread",
            passed=spread_ok,
            value=round(spread, 4) if spread is not None else None,
            threshold=max_spread_bps,
            reason=(
                f"bid-ask spread {spread:.1f} bps above the "
                f"{max_spread_bps:.1f} bps ceiling"
                if spread is not None and not spread_ok
                else ""
            ),
        )
    )
    depth_cover = _numeric(features.get("depth_cover_ratio"))
    depth_ok = (
        depth_cover is None or min_depth_cover <= 0 or depth_cover >= min_depth_cover
    )
    gates.append(
        _gate(
            reasons,
            name="depth",
            label="Order-book depth",
            passed=depth_ok,
            value=round(depth_cover, 4) if depth_cover is not None else None,
            threshold=min_depth_cover,
            reason=(
                f"order-book depth covers only {depth_cover:.2f}x a grid slot "
                f"(minimum {min_depth_cover:.2f}x)"
                if depth_cover is not None and not depth_ok
                else ""
            ),
        )
    )
    stress_return = _numeric(oos.get("cost_stress_return"))
    stress_checked = oos.get("cost_stress_profitable") is not None
    stress_ok = (
        not enforce_cost_stress
        or not stress_checked
        or stress_return is None
        or stress_return > min_stressed_return
    )
    gates.append(
        _gate(
            reasons,
            name="cost_stress",
            label="Profitable under cost stress",
            passed=stress_ok,
            value=round(stress_return, 6) if stress_return is not None else None,
            threshold=min_stressed_return,
            reason=(
                f"unprofitable under stressed fees/slippage "
                f"(net {stress_return:.2%})"
                if not stress_ok and stress_return is not None
                else ""
            ),
        )
    )

    pf_state = _pf_validation(
        oos,
        min_trades=min_trades,
        pf_review=pf_review,
        pf_validate=pf_validate,
    )
    pf_value = _numeric(oos.get("profit_factor"))
    pf_gate_ok = not pf_state["validation_required"] or pf_state["validation_passed"]
    pf_reason = ""
    if pf_state["validation_required"] and not pf_state["validation_passed"]:
        missing = []
        if not pf_state["enough_trades"]:
            missing.append(f"needs ≥{max(min_trades * 2, 24)} fills")
        if not pf_state["no_stop"]:
            missing.append("stop-out observed")
        if pf_state["stress_checked"] and not pf_state["stress_profitable"]:
            missing.append("fails cost stress")
        pf_reason = (
            f"profit factor {pf_value:.1f} exceeds {pf_validate:.0f} and requires "
            f"additional validation ({', '.join(missing) or 'failed checks'})"
        )
    gates.append(
        _gate(
            reasons,
            name="profit_factor_validation",
            label="Profit-factor validation",
            passed=pf_gate_ok,
            value=round(pf_value, 4) if pf_value is not None else None,
            threshold=pf_validate,
            reason=pf_reason,
        )
    )

    item["hard_gates"] = gates
    item["eligible"] = len(reasons) == 0
    item["eligibility_reasons"] = list(dict.fromkeys(reasons))
    item["exclusion_reason"] = (
        "; ".join(item["eligibility_reasons"][:2]) if reasons else ""
    )
    item["_pf_state"] = pf_state


def score_results(
    results: list[dict[str, Any]],
    *,
    min_adv_usd: float,
    min_trades: int,
    max_drawdown: float,
    min_atr_pct: float = 0.0,
    max_atr_pct: float = 0.0,
    pf_review: float = 10.0,
    pf_validate: float = 30.0,
    max_spread_bps: float = 0.0,
    min_depth_cover: float = 0.0,
    enforce_cost_stress: bool = True,
    min_stressed_return: float = 0.0,
    max_correlation: float = 0.85,
    max_top_trade_share: float = 0.6,
    min_data_days: int = 120,
    corr_lookback_days: int = 60,
    correlation_out: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Assign hard gates, component scores, flags, and ranks.

    Gates run before ranking: an ineligible coin has ``rank=None``. Scores are
    still produced for ineligible coins so the dashboard can show how close a
    rejected candidate was to the ranking threshold.
    """

    for item in results:
        _prepare_score_source(item)
        item.setdefault("_correlation", {})
        _evaluate_item_gates(
            item,
            min_adv_usd=min_adv_usd,
            min_trades=min_trades,
            max_drawdown=max_drawdown,
            min_atr_pct=min_atr_pct,
            max_atr_pct=max_atr_pct,
            pf_review=pf_review,
            pf_validate=pf_validate,
            max_spread_bps=max_spread_bps,
            min_depth_cover=min_depth_cover,
            enforce_cost_stress=enforce_cost_stress,
            min_stressed_return=min_stressed_return,
            min_data_days=min_data_days,
        )

    # Correlation is measured only across coins that passed the hard gates,
    # then feeds the diversification factor below.
    correlation_info = correlation_diagnostics(
        results, lookback_days=corr_lookback_days, max_corr=max_correlation
    )
    if correlation_out is not None:
        correlation_out.update(correlation_info)
    for item in results:
        item["_score_source"]["avg_correlation"] = (
            item.get("_correlation") or {}
        ).get("avg_correlation")

    percentiles = {
        "net_return": _percentiles(results, "net_return", True),
        "return_to_drawdown": _percentiles(results, "return_to_drawdown", True),
        "profit_factor": _percentiles(results, "profit_factor", True),
        "win_rate": _percentiles(results, "win_rate", True),
        "max_drawdown": _percentiles(results, "max_drawdown", False),
        "recovery_days": _percentiles(results, "recovery_days", False),
        "no_stop": _percentiles(results, "no_stop", True),
        "max_unrealized_loss_pct": _percentiles(results, "max_unrealized_loss_pct", False),
        "tail_loss_1pct": _percentiles(results, "tail_loss_1pct", False),
        "fold_consistency": _percentiles(results, "fold_consistency", True),
        "worst_fold_return": _percentiles(results, "worst_fold_return", True),
        "fold_return_std": _percentiles(results, "fold_return_std", False),
        "parameter_stability": _percentiles(results, "parameter_stability", True),
        "parameter_sensitivity": _percentiles(results, "parameter_sensitivity", False),
        "regime_robustness": _percentiles(results, "regime_robustness", True),
        "time_in_range": _percentiles(results, "time_in_range", True),
        "mean_reversion_score": _percentiles(results, "mean_reversion_score", True),
        "range_stability_score": _percentiles(results, "range_stability_score", True),
        "adv_usd_median": _percentiles(results, "adv_usd_median", True),
        "cost_to_edge": _percentiles(results, "cost_to_edge", False),
        "spread_bps": _percentiles(results, "spread_bps", False),
        "depth_cover_ratio": _percentiles(results, "depth_cover_ratio", True),
        "funding_long_cost": _percentiles(results, "funding_long_cost", False),
        "volume_cv": _percentiles(results, "volume_cv", False),
        "cost_stress_return": _percentiles(results, "cost_stress_return", True),
        "avg_correlation": _percentiles(results, "avg_correlation", False),
    }
    # Baseline percentiles retain the previous ranking formula so the enhanced
    # ranking can be compared against it on the same cross-section.
    baseline_percentiles = {
        "net_return": percentiles["net_return"],
        "calmar": _percentiles(results, "calmar", True),
        "profit_factor": percentiles["profit_factor"],
        "win_rate": percentiles["win_rate"],
        "max_drawdown": percentiles["max_drawdown"],
        "fold_consistency": percentiles["fold_consistency"],
        "worst_fold_return": percentiles["worst_fold_return"],
        "no_stop": percentiles["no_stop"],
        "funding_long_cost": percentiles["funding_long_cost"],
        "mean_reversion_score": percentiles["mean_reversion_score"],
        "range_stability_score": percentiles["range_stability_score"],
        "parameter_stability": percentiles["parameter_stability"],
        "time_in_range": percentiles["time_in_range"],
        "adv_usd_median": percentiles["adv_usd_median"],
        "cost_to_edge": percentiles["cost_to_edge"],
        "volume_cv": percentiles["volume_cv"],
    }

    for idx, item in enumerate(results):
        oos = item.get("oos_metrics") or {}
        features = item.get("features") or {}
        folds = item.get("folds") or []

        def p(name: str) -> float | None:
            return percentiles.get(name, {}).get(idx)

        def bp(name: str) -> float | None:
            return baseline_percentiles.get(name, {}).get(idx)

        gates = item.get("hard_gates") or []
        pf_state = item.get("_pf_state") or {}
        stop_hit = bool(oos.get("liquidated") or oos.get("stopped"))
        consistency = _numeric(oos.get("fold_consistency"))
        trades = int(_numeric(oos.get("trades")) or 0)
        stress_return = _numeric(oos.get("cost_stress_return"))
        stress_checked = oos.get("cost_stress_profitable") is not None
        stress_ok = (
            not enforce_cost_stress
            or not stress_checked
            or stress_return is None
            or stress_return > min_stressed_return
        )
        pf_value = _numeric(oos.get("profit_factor"))

        flags: list[dict[str, Any]] = []

        def flag(code: str, severity: str, message: str) -> None:
            flags.append({"code": code, "severity": severity, "message": message})

        if pf_value is not None and pf_value > pf_validate:
            flag(
                "profit_factor_validation",
                "warning",
                f"Profit factor {pf_value:.1f} is above {pf_validate:.0f}; "
                "treat the result as unvalidated even when the extra checks pass.",
            )
        elif pf_value is not None and pf_value > pf_review:
            flag(
                "profit_factor_review",
                "warning",
                f"Profit factor {pf_value:.1f} is above {pf_review:.0f}; "
                "review trade accounting and fee assumptions.",
            )
        if stress_checked and not stress_ok:
            flag(
                "cost_stress_failed",
                "critical",
                f"Strategy turns unprofitable when fees and slippage are multiplied "
                f"({(stress_return or 0.0):.2%} stressed net return).",
            )
        if oos.get("single_regime"):
            flag(
                "single_regime",
                "warning",
                "Out-of-sample folds cover only one market regime; robustness is unproven.",
            )
        if (oos.get("dominant_regime_share") or 0.0) >= 0.9 and (
            oos.get("regimes_covered") or 0
        ) > 1:
            flag(
                "regime_dependent",
                "warning",
                f"{(oos.get('dominant_regime_share') or 0.0):.0%} of positive return "
                f"comes from the {oos.get('dominant_regime')} regime.",
            )
        sensitivity = _numeric(oos.get("parameter_sensitivity"))
        if sensitivity is not None and sensitivity > 0.5:
            flag(
                "parameter_sensitive",
                "warning",
                f"Small parameter changes reduce return by {sensitivity:.0%} on average.",
            )
        top_share = _numeric(oos.get("top_trade_share"))
        if top_share is not None and top_share > max_top_trade_share:
            flag(
                "concentrated_returns",
                "warning",
                f"The single best trade accounts for {top_share:.0%} of gross profit.",
            )
        correlation = item.get("_correlation") or {}
        if correlation.get("exceeds_limit"):
            flag(
                "high_correlation",
                "warning",
                "Daily returns are highly correlated with other eligible coins "
                f"(peak {correlation.get('max_correlation'):.2f}).",
            )
        recovery = _numeric(oos.get("recovery_days"))
        if recovery is not None and recovery > 30:
            flag(
                "slow_recovery",
                "info",
                f"Worst drawdown took {recovery:.0f} days to recover.",
            )
        exposure = _numeric(oos.get("max_exposure_ratio"))
        if exposure is not None and exposure > 1.5:
            flag(
                "high_inventory_exposure",
                "warning",
                f"Peak inventory exposure reached {exposure:.2f}x equity.",
            )
        cycle_ratio = _numeric(oos.get("cycle_completion_ratio"))
        if cycle_ratio is not None and trades >= min_trades and cycle_ratio < 0.5:
            flag(
                "low_cycle_completion",
                "info",
                f"Only {cycle_ratio:.0%} of buys completed against a sell rung.",
            )
        item["flags"] = flags

        profitability = _weighted(
            [(p("profit_factor"), 0.60), (p("win_rate"), 0.40)]
        )
        risk_adjusted = _weighted(
            [(p("net_return"), 0.45), (p("return_to_drawdown"), 0.55)]
        )
        drawdown_control = _weighted(
            [
                (p("max_drawdown"), 0.40),
                (p("recovery_days"), 0.20),
                (p("no_stop"), 0.20),
                (p("max_unrealized_loss_pct"), 0.12),
                (p("tail_loss_1pct"), 0.08),
            ]
        )
        consistency_factor = _weighted(
            [
                (p("fold_consistency"), 0.35),
                (p("worst_fold_return"), 0.25),
                (p("fold_return_std"), 0.20),
                (p("parameter_stability"), 0.20),
            ]
        )
        regime_factor = _weighted(
            [(p("regime_robustness"), 0.75), (p("time_in_range"), 0.25)]
        )
        stability_factor = _weighted(
            [
                (p("mean_reversion_score"), 0.35),
                (p("range_stability_score"), 0.30),
                (p("parameter_sensitivity"), 0.20),
                (p("parameter_stability"), 0.15),
            ]
        )
        execution_factor = _weighted(
            [
                (p("adv_usd_median"), 0.22),
                (p("cost_to_edge"), 0.18),
                (p("spread_bps"), 0.14),
                (p("depth_cover_ratio"), 0.16),
                (p("funding_long_cost"), 0.10),
                (p("cost_stress_return"), 0.20),
            ]
        )
        diversification_factor = _weighted([(p("avg_correlation"), 1.0)], default=50.0)

        factors = {
            "profitability": profitability,
            "risk_adjusted": risk_adjusted,
            "drawdown_control": drawdown_control,
            "consistency": consistency_factor,
            "regime": regime_factor,
            "stability": stability_factor,
            "execution": execution_factor,
            "diversification": diversification_factor,
        }
        score = sum(
            factors[name] * weight for name, weight in FACTOR_WEIGHTS.items()
        )

        penalties: list[dict[str, Any]] = []

        def penalize(code: str, points: float, reason: str) -> None:
            if points <= 0:
                return
            penalties.append({"code": code, "points": round(points, 2), "reason": reason})
            nonlocal score
            score -= points

        if stop_hit:
            penalize("stop_out", 15.0, "Stop-out or liquidation in the test window.")
        if consistency is not None and consistency < 0.5:
            penalize("fold_consistency", 8.0, "Fewer than half of folds were profitable.")
        if trades < min_trades:
            penalize("low_sample", 5.0, f"Only {trades} OOS fills.")
        if stress_checked and not stress_ok:
            penalize("cost_stress", 6.0, "Unprofitable under stressed costs.")
        if oos.get("single_regime"):
            penalize("single_regime", 3.0, "Only one market regime was tested.")
        if (oos.get("dominant_regime_share") or 0.0) >= 0.9 and (
            oos.get("regimes_covered") or 0
        ) > 1:
            penalize("regime_dependence", 4.0, "Returns depend on a single regime.")
        if sensitivity is not None and sensitivity > 0.5:
            penalize(
                "parameter_sensitivity",
                min(8.0, (sensitivity - 0.5) * 8.0),
                "Return deteriorates sharply with small parameter changes.",
            )
        if top_share is not None and top_share > max_top_trade_share:
            penalize(
                "trade_concentration",
                4.0,
                "Gross profit is dominated by a small number of trades.",
            )
        corr_points, corr_reason = correlation_penalty(item, max_corr=max_correlation)
        penalize("correlation", corr_points, corr_reason)
        if recovery is not None and recovery > 30:
            penalize("slow_recovery", min(4.0, (recovery - 30) / 30.0), "Slow drawdown recovery.")
        if exposure is not None and exposure > 1.5:
            penalize(
                "inventory_exposure",
                min(4.0, (exposure - 1.5) * 2.0),
                "Peak inventory exposure is high relative to equity.",
            )
        if cycle_ratio is not None and trades >= min_trades and cycle_ratio < 0.5:
            penalize(
                "cycle_completion",
                2.0,
                "Many buys never completed against a sell rung.",
            )

        score = max(0.0, min(100.0, score))
        item["score"] = round(score, 2)

        factor_inputs = {
            "profitability": {
                "profit_factor": p("profit_factor"),
                "win_rate": p("win_rate"),
            },
            "risk_adjusted": {
                "net_return": p("net_return"),
                "return_to_drawdown": p("return_to_drawdown"),
            },
            "drawdown_control": {
                "max_drawdown": p("max_drawdown"),
                "recovery_days": p("recovery_days"),
                "no_stop": p("no_stop"),
                "max_unrealized_loss_pct": p("max_unrealized_loss_pct"),
                "tail_loss_1pct": p("tail_loss_1pct"),
            },
            "consistency": {
                "fold_consistency": p("fold_consistency"),
                "worst_fold_return": p("worst_fold_return"),
                "fold_return_std": p("fold_return_std"),
                "parameter_stability": p("parameter_stability"),
            },
            "regime": {
                "regime_robustness": p("regime_robustness"),
                "time_in_range": p("time_in_range"),
            },
            "stability": {
                "mean_reversion_score": p("mean_reversion_score"),
                "range_stability_score": p("range_stability_score"),
                "parameter_sensitivity": p("parameter_sensitivity"),
                "parameter_stability": p("parameter_stability"),
            },
            "execution": {
                "adv_usd_median": p("adv_usd_median"),
                "cost_to_edge": p("cost_to_edge"),
                "spread_bps": p("spread_bps"),
                "depth_cover_ratio": p("depth_cover_ratio"),
                "funding_long_cost": p("funding_long_cost"),
                "cost_stress_return": p("cost_stress_return"),
            },
            "diversification": {"avg_correlation": p("avg_correlation")},
        }
        breakdown = {
            "factors": {
                name: {
                    "score": round(factors[name], 2),
                    "weight": FACTOR_WEIGHTS[name],
                    "contribution": round(factors[name] * FACTOR_WEIGHTS[name], 2),
                    "status": (
                        "strong"
                        if factors[name] >= 70
                        else "ok"
                        if factors[name] >= 55
                        else "watch"
                        if factors[name] >= 40
                        else "weak"
                    ),
                    "inputs": {
                        key: round(value, 2) if value is not None else None
                        for key, value in factor_inputs[name].items()
                    },
                }
                for name in FACTOR_WEIGHTS
            },
            "penalties": penalties,
            "base_score": round(
                sum(factors[name] * weight for name, weight in FACTOR_WEIGHTS.items()),
                2,
            ),
            "final_score": round(score, 2),
        }

        baseline_profitability = _weighted(
            [
                (bp("net_return"), 0.45),
                (bp("calmar"), 0.30),
                (bp("profit_factor"), 0.15),
                (bp("win_rate"), 0.10),
            ]
        )
        baseline_survivability = _weighted(
            [
                (bp("max_drawdown"), 0.35),
                (bp("fold_consistency"), 0.25),
                (bp("worst_fold_return"), 0.15),
                (bp("no_stop"), 0.15),
                (bp("funding_long_cost"), 0.10),
            ]
        )
        baseline_stability = _weighted(
            [
                (bp("mean_reversion_score"), 0.40),
                (bp("range_stability_score"), 0.30),
                (bp("parameter_stability"), 0.20),
                (bp("time_in_range"), 0.10),
            ]
        )
        baseline_execution = _weighted(
            [
                (bp("adv_usd_median"), 0.40),
                (bp("cost_to_edge"), 0.30),
                (bp("funding_long_cost"), 0.15),
                (bp("volume_cv"), 0.15),
            ]
        )
        baseline_score = (
            0.35 * baseline_profitability
            + 0.30 * baseline_survivability
            + 0.20 * baseline_stability
            + 0.15 * baseline_execution
        )
        if stop_hit:
            baseline_score -= 15.0
        if consistency is not None and consistency < 0.5:
            baseline_score -= 8.0
        if trades < min_trades:
            baseline_score -= 5.0
        baseline_score = max(0.0, min(100.0, baseline_score))
        item["baseline_score"] = round(baseline_score, 2)
        item["score_delta"] = round(item["score"] - baseline_score, 2)

        item["component_scores"] = {
            name: round(value, 2) for name, value in factors.items()
        }
        item["score_breakdown"] = breakdown

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

        wf_diagnostics = item.get("_wf_diagnostics") or {}
        item["diagnostics"] = {
            "methodology_version": METHODOLOGY_VERSION,
            "hard_gates": gates,
            "flags": flags,
            "score_breakdown": breakdown,
            "baseline_score": item["baseline_score"],
            "score_delta": item["score_delta"],
            "eligibility_reasons": item["eligibility_reasons"],
            "pf_validation": pf_state,
            "correlation": {
                key: value
                for key, value in (item.get("_correlation") or {}).items()
                if key != "peer_map"
            },
            "liquidity": {
                "adv_usd_median": features.get("adv_usd_median"),
                "adv_usd_min": features.get("adv_usd_min"),
                "spread_bps": features.get("spread_bps"),
                "depth_cover_ratio": features.get("depth_cover_ratio"),
                "depth_bid_usd": features.get("depth_bid_usd"),
                "depth_ask_usd": features.get("depth_ask_usd"),
            },
            **wf_diagnostics,
        }

    eligible = [item for item in results if item["eligible"]]
    ineligible = [item for item in results if not item["eligible"]]
    eligible.sort(
        key=lambda item: (
            item.get("score", 0.0),
            item.get("oos_metrics", {}).get("net_return", 0.0),
        ),
        reverse=True,
    )
    ineligible.sort(
        key=lambda item: (
            item.get("score", 0.0),
            item.get("oos_metrics", {}).get("net_return", 0.0),
        ),
        reverse=True,
    )
    for rank, item in enumerate(eligible, start=1):
        item["rank"] = rank
    for item in ineligible:
        item["rank"] = None
    return eligible + ineligible


def ranking_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    eligible = [r for r in results if r.get("eligible")]
    flag_counter: Counter[str] = Counter()
    gate_counter: Counter[str] = Counter()
    regime_counter: Counter[str] = Counter()
    correlations: list[float] = []
    for item in results:
        for flag in item.get("flags") or []:
            flag_counter[flag["code"]] += 1
        for gate in item.get("hard_gates") or []:
            if not gate.get("passed"):
                gate_counter[gate["name"]] += 1
        regime = (item.get("oos_metrics") or {}).get("dominant_regime")
        if regime:
            regime_counter[str(regime)] += 1
        correlation = (item.get("_correlation") or {}).get("avg_correlation")
        if correlation is not None:
            correlations.append(float(correlation))
    eligible_scores = [float(r.get("score") or 0.0) for r in eligible]
    baseline_scores = [float(r.get("baseline_score") or 0.0) for r in eligible]
    return {
        "universe": len(results),
        "eligible": len(eligible),
        "prime": sum(1 for r in results if r.get("suitability") == "prime"),
        "viable": sum(1 for r in results if r.get("suitability") == "viable"),
        "watch": sum(1 for r in results if r.get("suitability") == "watch"),
        "avoid": sum(1 for r in results if r.get("suitability") == "avoid"),
        "ineligible": sum(1 for r in results if r.get("suitability") == "ineligible"),
        "flagged": sum(1 for r in results if r.get("flags")),
        "flag_counts": dict(flag_counter),
        "gate_failures": dict(gate_counter),
        "regime_counts": dict(regime_counter),
        "avg_correlation": (
            round(float(np.mean(correlations)), 4) if correlations else None
        ),
        "avg_eligible_score": (
            round(float(np.mean(eligible_scores)), 2) if eligible_scores else None
        ),
        "avg_baseline_score": (
            round(float(np.mean(baseline_scores)), 2) if baseline_scores else None
        ),
        "methodology_version": METHODOLOGY_VERSION,
        "top": [
            {
                "rank": r.get("rank"),
                "symbol": r["coin"].symbol,
                "score": r.get("score"),
                "baseline_score": r.get("baseline_score"),
                "suitability": r.get("suitability"),
                "flags": [flag["code"] for flag in r.get("flags") or []],
            }
            for r in eligible[:10]
        ],
    }
