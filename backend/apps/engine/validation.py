"""Does the ranking predict anything, and how diversified is the book?

Two questions are answered from data, never from assumptions:

* **Score validity.** Every coin is scored on walk-forward folds only; the
  final holdout window is simulated afterwards with the deployed parameters.
  The rank correlation between the score (and each factor) and the holdout
  return measures whether the ranking carries information. A single run is
  noisy, so results are also pooled across recent runs.
* **Strategy correlation.** Grid strategies on different coins can be far
  more correlated than they look, because every long-only grid is long the
  market. Pairwise correlations of the strategies' own daily returns and their
  beta to BTC show how much of the book is one trade.

The findings are turned into strengths and weaknesses for the admin page.
"""

from __future__ import annotations

import math
from typing import Any, Iterable

import numpy as np

from apps.engine.scoring import FACTOR_WEIGHTS

MIN_IC_COINS = 8
# Book-level diagnostics on fewer eligible coins than this are anecdotes, so
# they are measured on every analysed coin instead.
MIN_SCOPE_COINS = 3
IC_SIGNIFICANT_T = 2.0


def _ranks(values: np.ndarray) -> np.ndarray:
    order = np.argsort(values, kind="mergesort")
    ranks = np.empty(values.size, dtype=float)
    sorted_values = values[order]
    position = 0
    while position < values.size:
        end = position
        while end + 1 < values.size and sorted_values[end + 1] == sorted_values[position]:
            end += 1
        ranks[order[position : end + 1]] = (position + end) / 2.0
        position = end + 1
    return ranks


def spearman(xs: Iterable[float], ys: Iterable[float]) -> float | None:
    x = np.asarray(list(xs), dtype=float)
    y = np.asarray(list(ys), dtype=float)
    if x.size != y.size or x.size < 3:
        return None
    rx, ry = _ranks(x), _ranks(y)
    if np.std(rx) <= 1e-12 or np.std(ry) <= 1e-12:
        return None
    return float(np.corrcoef(rx, ry)[0, 1])


def t_stat(r: float | None, n: int) -> float | None:
    if r is None or n < 3:
        return None
    denominator = max(1e-12, 1.0 - r * r)
    return float(r * math.sqrt((n - 2) / denominator))


def _ic(pairs: list[tuple[float, float]]) -> dict[str, Any]:
    n = len(pairs)
    r = spearman([a for a, _ in pairs], [b for _, b in pairs]) if n >= 3 else None
    t = t_stat(r, n)
    return {
        "n": n,
        "ic": round(r, 4) if r is not None else None,
        "t_stat": round(t, 3) if t is not None else None,
        "significant": bool(t is not None and n >= MIN_IC_COINS and abs(t) >= IC_SIGNIFICANT_T),
    }


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def score_validation(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Cross-sectional rank correlation of score/factors with holdout return."""

    rows = []
    for item in items:
        oos = item.get("oos_metrics") or {}
        holdout = _num(oos.get("holdout_return"))
        if holdout is None:
            continue
        rows.append(
            {
                "score": _num(item.get("score")),
                "oos_return": _num(oos.get("net_return")),
                "holdout": holdout,
                "eligible": bool(item.get("eligible")),
                "factors": item.get("component_scores") or {},
            }
        )

    def pairs(key: str, subset: list[dict[str, Any]]) -> list[tuple[float, float]]:
        return [(row[key], row["holdout"]) for row in subset if row[key] is not None]

    factors = {}
    for name in FACTOR_WEIGHTS:
        factor_pairs = [
            (float(row["factors"][name]), row["holdout"])
            for row in rows
            if _num(row["factors"].get(name)) is not None
        ]
        factors[name] = _ic(factor_pairs)

    eligible = [row["holdout"] for row in rows if row["eligible"]]
    ineligible = [row["holdout"] for row in rows if not row["eligible"]]
    ordered = sorted((row for row in rows if row["score"] is not None), key=lambda r: r["score"])
    tercile = max(1, len(ordered) // 3)
    return {
        "coins": len(rows),
        "score": _ic(pairs("score", rows)),
        "oos_return": _ic(pairs("oos_return", rows)),
        "factors": factors,
        "eligible_holdout_mean": round(float(np.mean(eligible)), 6) if eligible else None,
        "ineligible_holdout_mean": round(float(np.mean(ineligible)), 6) if ineligible else None,
        "eligible_holdout_positive": (
            round(sum(1 for value in eligible if value > 0) / len(eligible), 4)
            if eligible
            else None
        ),
        "top_tercile_holdout_mean": (
            round(float(np.mean([r["holdout"] for r in ordered[-tercile:]])), 6)
            if len(ordered) >= 3
            else None
        ),
        "bottom_tercile_holdout_mean": (
            round(float(np.mean([r["holdout"] for r in ordered[:tercile]])), 6)
            if len(ordered) >= 3
            else None
        ),
    }


def strategy_validation(
    items: list[dict[str, Any]],
    strategy_correlation: dict[str, Any],
    price_correlation: dict[str, Any],
    universe_correlation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Book-level diversification and robustness diagnostics.

    Measured on eligible coins when any exist. When none qualify the same
    diagnostics run on every analysed coin, so an empty book still explains
    why the strategy failed instead of showing blanks.
    """

    eligible_count = sum(1 for item in items if item.get("eligible"))
    scoped = eligible_count >= MIN_SCOPE_COINS
    scope = "eligible" if scoped else "universe"
    if not scoped:
        strategy_correlation = universe_correlation or strategy_correlation
    eligible = [
        item
        for item in items
        if (item.get("eligible") if scoped else bool(item.get("oos_metrics")))
    ]
    betas = [
        _num((item.get("_market") or {}).get("beta"))
        for item in eligible
    ]
    btc_corrs = [
        _num((item.get("_market") or {}).get("correlation"))
        for item in eligible
    ]
    betas = [value for value in betas if value is not None]
    btc_corrs = [value for value in btc_corrs if value is not None]

    oos_annual = []
    holdout_annual = []
    fill_deltas = []
    stress_pass = []
    for item in eligible:
        oos = item.get("oos_metrics") or {}
        test_days = _num(oos.get("test_days")) or 0.0
        holdout_days = _num(oos.get("holdout_days")) or 0.0
        net = _num(oos.get("net_return"))
        holdout = _num(oos.get("holdout_return"))
        if net is not None and test_days > 0 and holdout is not None and holdout_days > 0:
            oos_annual.append(net * 365.0 / test_days)
            holdout_annual.append(holdout * 365.0 / holdout_days)
        delta = _num(oos.get("fill_model_delta"))
        if delta is not None:
            fill_deltas.append(delta)
        if oos.get("cost_stress_profitable") is not None:
            stress_pass.append(bool(oos.get("cost_stress_profitable")))

    return {
        "eligible": eligible_count,
        "scope": scope,
        "coins": len(eligible),
        "strategy_correlation": {
            "avg_pair": strategy_correlation.get("avg_pair_correlation"),
            "max_pair": strategy_correlation.get("max_pair_correlation"),
            "threshold": strategy_correlation.get("threshold"),
            "high_pairs": (strategy_correlation.get("high_pairs") or [])[:10],
            "lookback_days": strategy_correlation.get("lookback_days"),
        },
        "price_correlation": {
            "avg_pair": price_correlation.get("avg_pair_correlation"),
            "max_pair": price_correlation.get("max_pair_correlation"),
        },
        "btc_beta_mean": round(float(np.mean(betas)), 4) if betas else None,
        "btc_correlation_mean": round(float(np.mean(btc_corrs)), 4) if btc_corrs else None,
        "oos_annualized_mean": round(float(np.mean(oos_annual)), 6) if oos_annual else None,
        "holdout_annualized_mean": (
            round(float(np.mean(holdout_annual)), 6) if holdout_annual else None
        ),
        "fill_model_delta_mean": round(float(np.mean(fill_deltas)), 6) if fill_deltas else None,
        "cost_stress_pass_rate": (
            round(sum(stress_pass) / len(stress_pass), 4) if stress_pass else None
        ),
    }


def pooled_ic(history: list[dict[str, Any]], key: str = "score") -> dict[str, Any]:
    """Average information coefficient across runs, with a t-stat on the mean."""

    values = []
    for summary in history:
        block = (summary or {}).get("score_validation") or {}
        entry = block.get(key) if key in ("score", "oos_return") else (
            (block.get("factors") or {}).get(key)
        )
        ic = _num((entry or {}).get("ic"))
        if ic is not None:
            values.append(ic)
    if not values:
        return {"runs": 0, "mean_ic": None, "t_stat": None}
    mean = float(np.mean(values))
    std = float(np.std(values, ddof=1)) if len(values) > 1 else 0.0
    t = mean / (std / math.sqrt(len(values))) if std > 1e-12 else None
    return {
        "runs": len(values),
        "mean_ic": round(mean, 4),
        "t_stat": round(t, 3) if t is not None else None,
        "positive_share": round(sum(1 for v in values if v > 0) / len(values), 4),
    }


def findings(
    scores: dict[str, Any],
    strategies: dict[str, Any],
    *,
    pooled: dict[str, Any] | None = None,
    max_strategy_correlation: float = 0.7,
) -> list[dict[str, Any]]:
    """Plain-language strengths and weaknesses derived from the numbers."""

    out: list[dict[str, Any]] = []

    def add(kind: str, code: str, title: str, detail: str) -> None:
        out.append({"kind": kind, "code": code, "title": title, "detail": detail})

    score = scores.get("score") or {}
    ic = _num(score.get("ic"))
    n = int(score.get("n") or 0)
    if n < MIN_IC_COINS:
        add(
            "weakness",
            "score_unvalidated",
            "Score not yet validated",
            f"Only {n} coins have a holdout result; at least {MIN_IC_COINS} are needed "
            "to test whether the score predicts anything.",
        )
    elif ic is not None and score.get("significant") and ic > 0:
        add(
            "strength",
            "score_predictive",
            "Score predicts holdout returns",
            f"Rank correlation {ic:+.2f} (t = {score.get('t_stat')}, n = {n}) between the "
            "score and the untouched holdout.",
        )
    elif ic is not None and ic <= 0:
        add(
            "weakness",
            "score_not_predictive",
            "Score does not predict holdout returns",
            f"Rank correlation {ic:+.2f} (n = {n}). Higher-ranked coins did not do better "
            "on unseen data in this run.",
        )
    else:
        add(
            "weakness",
            "score_weak",
            "Score signal is weak",
            f"Rank correlation {ic if ic is None else round(ic, 2)} is not statistically "
            f"significant (t = {score.get('t_stat')}, n = {n}).",
        )

    if pooled and pooled.get("runs", 0) >= 4 and pooled.get("mean_ic") is not None:
        mean_ic = float(pooled["mean_ic"])
        t = _num(pooled.get("t_stat"))
        if mean_ic > 0 and t is not None and t >= IC_SIGNIFICANT_T:
            add(
                "strength",
                "score_stable",
                "Score is predictive across runs",
                f"Mean IC {mean_ic:+.2f} over {pooled['runs']} runs (t = {t:.1f}).",
            )
        elif mean_ic <= 0:
            add(
                "weakness",
                "score_unstable",
                "Score fails across runs",
                f"Mean IC {mean_ic:+.2f} over {pooled['runs']} runs.",
            )

    for name, factor in (scores.get("factors") or {}).items():
        factor_ic = _num(factor.get("ic"))
        if factor_ic is None or not factor.get("significant"):
            continue
        label = name.replace("_", " ")
        if factor_ic > 0:
            add(
                "strength",
                f"factor_{name}",
                f"{label.capitalize()} factor is predictive",
                f"IC {factor_ic:+.2f} (t = {factor.get('t_stat')}).",
            )
        else:
            add(
                "weakness",
                f"factor_{name}",
                f"{label.capitalize()} factor points the wrong way",
                f"IC {factor_ic:+.2f} (t = {factor.get('t_stat')}): coins that score high on "
                "it did worse on the holdout. Consider lowering its weight.",
            )

    scope_note = (
        " (measured on every analysed coin: too few qualified)"
        if strategies.get("scope") == "universe"
        else ""
    )
    corr = strategies.get("strategy_correlation") or {}
    avg_pair = _num(corr.get("avg_pair"))
    max_pair = _num(corr.get("max_pair"))
    if avg_pair is not None:
        if avg_pair >= max_strategy_correlation * 0.75:
            add(
                "weakness",
                "book_concentrated",
                "Strategies move together",
                f"Average pairwise correlation of strategy returns is {avg_pair:.2f}; the "
                f"book behaves like one position{scope_note}.",
            )
        elif avg_pair <= 0.3:
            add(
                "strength",
                "book_diversified",
                "Strategies are diversified",
                f"Average pairwise correlation of strategy returns is only {avg_pair:.2f}{scope_note}.",
            )
    if max_pair is not None and max_pair >= max_strategy_correlation:
        pairs = corr.get("high_pairs") or []
        sample = ", ".join(f"{p['a']}/{p['b']} {p['correlation']:.2f}" for p in pairs[:3])
        add(
            "weakness",
            "correlated_pairs",
            "Highly correlated strategy pairs",
            f"Peak pair correlation {max_pair:.2f} ({sample}). Only one of each pair is "
            "opened in paper trading.",
        )
    price_avg = _num((strategies.get("price_correlation") or {}).get("avg_pair"))
    if price_avg is not None and avg_pair is not None and avg_pair < price_avg - 0.15:
        add(
            "strength",
            "grid_decorrelates",
            "Grids diversify better than the coins",
            f"Strategy correlation {avg_pair:.2f} vs price correlation {price_avg:.2f}.",
        )

    beta = _num(strategies.get("btc_beta_mean"))
    btc_corr = _num(strategies.get("btc_correlation_mean"))
    if btc_corr is not None:
        if btc_corr >= 0.5:
            add(
                "weakness",
                "market_beta",
                "Returns are mostly market direction",
                f"Average correlation to BTC is {btc_corr:.2f} (beta {beta}){scope_note}. A "
                "long-only grid loses in sustained declines regardless of coin choice.",
            )
        elif btc_corr <= 0.25:
            add(
                "strength",
                "low_market_beta",
                "Low dependence on BTC direction",
                f"Average correlation to BTC is {btc_corr:.2f}{scope_note}.",
            )

    oos_annual = _num(strategies.get("oos_annualized_mean"))
    holdout_annual = _num(strategies.get("holdout_annualized_mean"))
    if oos_annual is not None and holdout_annual is not None:
        if oos_annual <= 0 < holdout_annual:
            # Not a confirmation: the strategy lost on the folds and only the
            # most recent window was kind to it.
            add(
                "weakness",
                "period_dependent",
                "Results swing with the market period",
                f"Annualised {oos_annual:.1%} on the walk-forward folds but {holdout_annual:.1%} "
                f"on the holdout{scope_note}: the edge is not stable across periods.",
            )
        elif holdout_annual < 0 < oos_annual or (
            oos_annual > 0 and holdout_annual < 0.5 * oos_annual
        ):
            add(
                "weakness",
                "holdout_decay",
                "Holdout underperforms the folds",
                f"Annualised {holdout_annual:.1%} on the holdout vs {oos_annual:.1%} "
                f"on the walk-forward folds{scope_note}: part of the fold result is selection luck.",
            )
        elif oos_annual > 0 and holdout_annual >= 0.75 * oos_annual:
            add(
                "strength",
                "holdout_consistent",
                "Holdout confirms the folds",
                f"Annualised {holdout_annual:.1%} on the holdout vs {oos_annual:.1%} "
                f"on the folds{scope_note}.",
            )

    fill_delta = _num(strategies.get("fill_model_delta_mean"))
    if fill_delta is not None and abs(fill_delta) >= 0.02:
        add(
            "weakness",
            "fill_sensitive",
            "Results depend on the fill model",
            f"Switching fill model changes OOS return by {fill_delta:+.1%} on average; "
            "queue position matters for these grids.",
        )
    stress = _num(strategies.get("cost_stress_pass_rate"))
    if stress is not None:
        if stress >= 0.8:
            add(
                "strength",
                "cost_robust",
                "Robust to higher costs",
                f"{stress:.0%} of coins stay profitable with doubled fees and slippage{scope_note}.",
            )
        elif stress < 0.5:
            add(
                "weakness",
                "cost_fragile",
                "Fragile to costs",
                f"Only {stress:.0%} of coins survive doubled fees and slippage{scope_note}.",
            )
    if int(strategies.get("eligible") or 0) == 0:
        add(
            "weakness",
            "empty_book",
            "No coin qualifies",
            "Nothing passed the hard gates in this run, so no new paper sessions open. "
            "This is intended: the book stays empty rather than trading weak setups.",
        )
    return out
