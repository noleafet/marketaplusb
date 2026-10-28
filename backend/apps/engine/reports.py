"""Markdown report generation for rankings, performance, and risk."""

from __future__ import annotations

from typing import Any

from apps.engine.models import AnalysisRun, Report


def _fmt_pct(value: Any, digits: int = 1) -> str:
    try:
        return f"{float(value) * 100:.{digits}f}%"
    except (TypeError, ValueError):
        return "n/a"


def _fmt_num(value: Any, digits: int = 2) -> str:
    try:
        return f"{float(value):,.{digits}f}"
    except (TypeError, ValueError):
        return "n/a"


def _params_label(params: dict[str, Any]) -> str:
    if not params:
        return "n/a"
    return (
        f"{params.get('grid_levels')}L / {params.get('range_lookback_days')}d range / "
        f"{params.get('rebalance_days')}d rebalance"
    )


def _table(header: list[str], rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(header) + " |", "| " + " | ".join(["---"] * len(header)) + " |"]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return "\n".join(lines)


def build_reports(
    analysis: AnalysisRun, results: list[dict[str, Any]], config: dict[str, Any]
) -> list[Report]:
    cutoff = analysis.data_cutoff.isoformat() if analysis.data_cutoff else "n/a"
    top = [r for r in results if r.get("eligible")][:25]
    reports: list[Report] = []

    rank_rows = [
        [
            str(r["rank"]),
            r["coin"].symbol,
            r["coin"].name,
            str(r["score"]),
            str(r.get("baseline_score", "n/a")),
            _fmt_pct(r["oos_metrics"].get("net_return")),
            _fmt_pct(r["oos_metrics"].get("max_drawdown")),
            str(r["oos_metrics"].get("trades", 0)),
            _fmt_num(r["oos_metrics"].get("fold_consistency"), 2),
            _fmt_pct(r["oos_metrics"].get("cost_stress_return")),
            ", ".join(flag["code"] for flag in r.get("flags") or []) or "—",
            str(r.get("suitability", "")),
            _params_label(r.get("params", {})),
        ]
        for r in top
    ]
    ranking_body = "\n".join(
        [
            "# Grid suitability ranking",
            "",
            f"Analysis run `{analysis.id}` · data cutoff **{cutoff}** · universe **{len(results)}** coins.",
            "",
            "Ranking uses out-of-sample walk-forward results only. Fees (maker/taker), "
            "adverse slippage, perpetual funding, margin limits, and stop-outs are included. "
            "The grid starts flat so returns measure grid cycling rather than buy-and-hold beta. "
            "Hard eligibility gates are applied before ranking; review flags do not hide a "
            "profitable result but mark it for manual validation.",
            "",
            _table(
                [
                    "Rank",
                    "Coin",
                    "Name",
                    "Score",
                    "Baseline",
                    "OOS net",
                    "OOS max DD",
                    "Fills",
                    "Fold consistency",
                    "2x-cost net",
                    "Review flags",
                    "Class",
                    "Params",
                ],
                rank_rows,
            )
            if rank_rows
            else "_No coin passed the eligibility gates on this run._",
            "",
            "## Eligibility notes",
            "",
            *[
                f"- **{r['coin'].symbol}** — {r.get('exclusion_reason') or 'n/a'}"
                for r in results
                if not r.get("eligible")
            ][:30],
        ]
    )
    reports.append(
        Report.objects.create(
            analysis=analysis,
            kind=Report.Kind.RANKING,
            title=f"Grid ranking · {analysis.started_at:%Y-%m-%d}",
            body_markdown=ranking_body,
            payload={
                "cutoff": cutoff,
                "universe": len(results),
                "top": [
                    {
                        "rank": r["rank"],
                        "symbol": r["coin"].symbol,
                        "score": r["score"],
                        "baseline_score": r.get("baseline_score"),
                        "suitability": r["suitability"],
                        "flags": r.get("flags") or [],
                        "oos_metrics": r["oos_metrics"],
                        "params": r["params"],
                    }
                    for r in top
                ],
            },
        )
    )

    performance_rows = [
        [
            r["coin"].symbol,
            _fmt_pct(r["oos_metrics"].get("net_return")),
            _fmt_pct(r["oos_metrics"].get("annualized_return")),
            _fmt_num(r["oos_metrics"].get("sharpe"), 2),
            ", ".join(_fmt_pct(x) for x in (r["oos_metrics"].get("fold_returns") or [])),
            _fmt_pct(r["oos_metrics"].get("parameter_stability")),
            _fmt_num(r["oos_metrics"].get("fees_paid"), 2),
            _fmt_num(r["oos_metrics"].get("funding_paid"), 2),
        ]
        for r in top
    ]
    performance_body = "\n".join(
        [
            "# Out-of-sample performance",
            "",
            f"Run `{analysis.id}` · {len(top)} eligible coins shown.",
            "",
            "Walk-forward folds: parameters are optimized on each training window, then "
            "applied unchanged to the next contiguous test window. Fold consistency is the "
            "share of test windows with positive net return. Parameter stability is the share "
            "of folds selecting the modal parameter set.",
            "",
            _table(
                [
                    "Coin",
                    "OOS net",
                    "Annualized",
                    "Sharpe",
                    "Fold returns",
                    "Param stability",
                    "Fees",
                    "Funding",
                ],
                performance_rows,
            )
            if performance_rows
            else "_No eligible performance results._",
            "",
            "> Past simulated performance is not evidence of future profitability. "
            "The reported OOS result is still subject to universe-selection and regime risk.",
        ]
    )
    reports.append(
        Report.objects.create(
            analysis=analysis,
            kind=Report.Kind.PERFORMANCE,
            title=f"Walk-forward performance · {analysis.started_at:%Y-%m-%d}",
            body_markdown=performance_body,
            payload={
                "eligible": len(top),
                "metrics": [
                    {"symbol": r["coin"].symbol, **r["oos_metrics"]} for r in top
                ],
            },
        )
    )

    risk_rows = [
        [
            r["coin"].symbol,
            _fmt_pct(r["oos_metrics"].get("max_drawdown")),
            _fmt_pct(r["features"].get("atr_pct")),
            _fmt_pct(r["features"].get("range_containment")),
            _fmt_num(r["features"].get("adx"), 1),
            _fmt_pct(r["features"].get("funding_long_annual_cost")),
            _fmt_num(r["features"].get("cost_to_edge"), 2),
            _fmt_num(r["features"].get("adv_usd_median"), 0),
            _fmt_num(r["features"].get("spread_bps"), 2),
            _fmt_num(r["features"].get("depth_cover_ratio"), 2),
            _fmt_pct(r["oos_metrics"].get("cost_stress_return")),
            str(r["oos_metrics"].get("dominant_regime") or "—"),
            _fmt_num(r["oos_metrics"].get("recovery_days"), 0),
            "yes" if r["oos_metrics"].get("stopped") else "no",
        ]
        for r in top
    ]
    risk_body = "\n".join(
        [
            "# Risk and execution report",
            "",
            f"Run `{analysis.id}` · data cutoff **{cutoff}**.",
            "",
            "High ADX indicates trend risk, low range containment indicates an unstable "
            "range, positive funding is a carry cost for long inventory, and cost-to-edge "
            "above 1 means estimated round-trip costs exceed one grid step.",
            "",
            _table(
                [
                    "Coin",
                    "OOS max DD",
                    "ATR%",
                    "Range containment",
                    "ADX",
                    "Funding cost (ann.)",
                    "Cost/edge",
                    "ADV",
                    "Spread (bps)",
                    "Depth cover",
                    "2x-cost net",
                    "Dominant regime",
                    "Recovery (d)",
                    "Stopped",
                ],
                risk_rows,
            )
            if risk_rows
            else "_No eligible risk results._",
            "",
            "## Hard eligibility rules",
            "",
            f"- Minimum median 24h quote volume: ${config.get('min_adv_usd', 0):,.0f}",
            f"- Minimum OOS fills: {config.get('min_oos_trades')}",
            f"- Maximum OOS drawdown: {_fmt_pct(config.get('max_drawdown'))}",
            f"- ATR% band: {_fmt_pct(config.get('min_atr_pct'), 2)} – "
            f"{_fmt_pct(config.get('max_atr_pct'), 2)}",
            f"- Maximum bid-ask spread: {_fmt_num(config.get('max_spread_bps'), 1)} bps",
            f"- Minimum order-book depth cover: "
            f"{_fmt_num(config.get('min_depth_cover'), 1)}x a grid slot",
            f"- Must stay profitable with fees/slippage multiplied by "
            f"{_fmt_num(config.get('cost_stress_multiplier'), 1)}x",
            f"- Profit factor above {_fmt_num(config.get('pf_review'), 0)} is flagged; "
            f"above {_fmt_num(config.get('pf_validate'), 0)} requires extra validation",
            "- Positive OOS net return, at least half of folds profitable, no stop-out/liquidation",
            f"- At least {config.get('min_data_days', 120)} days of history",
            "",
            "## Exclusions",
            "",
            *[
                f"- **{r['coin'].symbol}** — {r.get('exclusion_reason')}"
                for r in results
                if not r.get("eligible")
            ][:40],
        ]
    )
    reports.append(
        Report.objects.create(
            analysis=analysis,
            kind=Report.Kind.RISK,
            title=f"Risk & execution · {analysis.started_at:%Y-%m-%d}",
            body_markdown=risk_body,
            payload={
                "hard_gates": config,
                "exclusions": {
                    r["coin"].symbol: r.get("exclusion_reason")
                    for r in results
                    if not r.get("eligible")
                },
            },
        )
    )

    methodology_body = "\n".join(
        [
            "# Methodology",
            "",
            "## Universe",
            "",
            "Coins are sourced monthly from CoinGecko's top 100 by USD market cap and "
            "retained permanently. Only coins with an active Binance USDⓈ-M USDT perpetual "
            "contract are analysed; stablecoins and low-volatility tokens are excluded.",
            "",
            "## Data",
            "",
            "Binance USDⓈ-M public klines and funding-rate history only. No account data, "
            "no private endpoints, and no exchange-testnet assumptions are used for ranking. "
            "Candles are stored in Postgres time-series tables. Currently in-progress candles "
            "are excluded.",
            "",
            "## Grid model",
            "",
            "Long-only limit-order grid, flat start, geometric or arithmetic levels built from "
            "trailing 5th/95th percentile bounds with ATR padding. Orders re-center on a fixed "
            "rebalance interval. Fills include maker fees and adverse slippage; forced exits use "
            "taker fees. Intrabar path is conservatively approximated as open → nearer extreme → "
            "farther extreme → close, with no same-bar order recycling. Gaps fill at the opening "
            "print. Leveraged isolated margin, maintenance margin, funding on open inventory, and "
            "a hard stop-out are simulated.",
            "",
            "## Validation",
            "",
            "Each fold optimizes the grid across a small parameter grid on a training window, "
            "selects one parameter set, and evaluates it on the next untouched test window. "
            "Reported OOS metrics are compounded across test folds. Parameter stability and fold "
            "consistency are reported to expose overfitting. The small parameter grid limits, but "
            "does not eliminate, selection bias.",
            "",
            "## Scoring",
            "",
            "Hard gates (history, folds, liquidity, fills, positive OOS return, drawdown, fold "
            "consistency, no stop-out, ATR band, spread, order-book depth, cost stress, and "
            "profit-factor validation) decide eligibility before any rank is assigned. "
            "Ineligible coins keep a diagnostic score but no rank.",
            "",
            "Eligible coins are ranked on eight de-correlated factors: profitability (18%), "
            "risk-adjusted return (18%), drawdown control (15%), walk-forward consistency (14%), "
            "regime robustness (10%), structural stability (10%), execution (10%), and "
            "diversification (5%). Inputs are cross-sectional percentiles. Correlated measures "
            "are grouped rather than counted twice (for example, raw return and return/drawdown "
            "share one risk-adjusted factor), and penalties are itemised in the dashboard.",
            "",
            "Each test fold is also labelled by market regime, rerun with 2x fees and slippage, "
            "and rerun with small parameter perturbations. The previous baseline formula is "
            "reported alongside the enhanced score so ranking changes can be audited.",
            "",
            "## Promotion path",
            "",
            "1. Historical walk-forward ranking",
            "2. Paper trading on live public market data with simulated fills/funding",
            "3. Threshold review and explicit approval",
            "4. Live futures only with API keys that cannot withdraw, strict limits, and a kill switch",
            "",
            "Live trading is disabled by default and is never started by a scheduled job.",
        ]
    )
    reports.append(
        Report.objects.create(
            analysis=analysis,
            kind=Report.Kind.METHODOLOGY,
            title="Engine methodology",
            body_markdown=methodology_body,
            payload={"version": 2},
        )
    )
    return reports
