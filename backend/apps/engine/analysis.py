"""Daily analysis orchestration: sync data, backtest, score, persist, report."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone as dt_timezone
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.common.binance import INTERVAL_MS, BinanceFuturesClient
from apps.coins.models import Coin
from apps.engine.binance_sync import sync_coin_market_data
from apps.engine.features import compute_features
from apps.engine.grid import GridParams, SimulationConfig
from apps.engine.hybrid_gate import Gate, build_gate
from apps.engine.models import AnalysisRun, CoinAnalysis, log_decision
from apps.engine.reports import build_reports
from apps.engine.robustness import daily_returns, select_diversified
from apps.engine.scoring import ranking_summary, score_results
from apps.engine.timescale import load_candles, load_funding
from apps.engine.walkforward import run_walk_forward

logger = logging.getLogger("engine")

HYBRID_WARMUP_DAYS = 10


def engine_config() -> SimulationConfig:
    cfg = settings.ENGINE
    return SimulationConfig(
        capital=float(cfg["PAPER_CAPITAL"]),
        leverage=float(cfg["LEVERAGE"]),
        maker_fee_rate=float(cfg["MAKER_FEE_RATE"]),
        taker_fee_rate=float(cfg["TAKER_FEE_RATE"]),
        slippage_bps=float(cfg["SLIPPAGE_BPS"]),
        interval=str(cfg["INTERVAL"]),
    )


def base_grid_params(overrides: dict[str, Any] | None = None) -> GridParams:
    cfg = settings.ENGINE
    values: dict[str, Any] = {
        "grid_levels": int(cfg["GRID_LEVELS"]),
        "range_lookback_days": int(cfg["RANGE_LOOKBACK_DAYS"]),
        "rebalance_days": int(cfg["REBALANCE_DAYS"]),
        "spacing_mode": "geometric",
        "atr_multiplier": float(cfg["ATR_MULTIPLIER"]),
        "initial_inventory_pct": float(cfg["INITIAL_INVENTORY_PCT"]),
        "stop_loss_pct": float(cfg["STOP_LOSS_PCT"]),
        "max_utilization": 0.90,
    }
    for key, value in (overrides or {}).items():
        if key in values and value is not None:
            values[key] = value
    return GridParams(**values)


def _order_book_features(
    client: BinanceFuturesClient,
    symbol: str,
    *,
    capital: float,
    leverage: float,
    max_utilization: float,
    grid_levels: int,
    band_bps: float,
) -> dict[str, Any]:
    """Live depth/spread snapshot relative to one grid slot's notional."""

    if not symbol:
        return {}
    try:
        payload = client.depth(symbol, limit=100)
        bids = [(float(price), float(qty)) for price, qty in payload.get("bids") or []]
        asks = [(float(price), float(qty)) for price, qty in payload.get("asks") or []]
    except Exception as exc:  # noqa: BLE001 - a snapshot failure must not stop analysis
        logger.warning("Order-book snapshot failed for %s: %s", symbol, exc)
        return {}
    if not bids or not asks:
        return {}
    best_bid = bids[0][0]
    best_ask = asks[0][0]
    if best_bid <= 0 or best_ask <= 0 or best_ask < best_bid:
        return {}
    mid = (best_bid + best_ask) / 2.0
    spread_bps = (best_ask - best_bid) / mid * 10_000.0
    band = max(0.0, band_bps) / 10_000.0
    bid_depth = sum(price * qty for price, qty in bids if price >= mid * (1.0 - band))
    ask_depth = sum(price * qty for price, qty in asks if price <= mid * (1.0 + band))
    slot_notional = (
        capital * leverage * max_utilization / max(1, grid_levels)
    )
    depth_cover = (
        min(bid_depth, ask_depth) / slot_notional if slot_notional > 0 else None
    )
    return {
        "best_bid": round(best_bid, 10),
        "best_ask": round(best_ask, 10),
        "mid_price": round(mid, 10),
        "spread_bps": round(spread_bps, 4),
        "depth_bid_usd": round(bid_depth, 2),
        "depth_ask_usd": round(ask_depth, 2),
        "depth_band_bps": band_bps,
        "depth_cover_ratio": round(depth_cover, 4) if depth_cover is not None else None,
        "order_book_sampled_at": timezone.now().isoformat(),
    }


def _analysis_params(params: dict[str, Any] | None) -> dict[str, Any]:
    cfg = settings.ENGINE
    result = {
        "lookback_days": int(cfg["LOOKBACK_DAYS"]),
        "interval": cfg["INTERVAL"],
        "symbols": [],
        "limit": int(cfg["SYMBOL_LIMIT"]),
        "force": False,
    }
    for key in result:
        if params and key in params and params[key] is not None:
            result[key] = params[key]
    result["symbols"] = [str(s).upper() for s in result["symbols"]]
    result["limit"] = max(0, int(result["limit"]))
    result["lookback_days"] = max(120, int(result["lookback_days"]))
    return result


def _hybrid_adx_enabled() -> bool:
    from apps.trading.models import LiveTradingState

    return bool((LiveTradingState.load().limits or {}).get("hybrid_adx"))


def _closed_klines(
    client: BinanceFuturesClient,
    symbol: str,
    interval: str,
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    """Fetch klines and drop the candle that is still forming at ``end``."""

    rows = client.klines_range(symbol, interval, start, end)
    step = timedelta(milliseconds=INTERVAL_MS[interval])
    return [row for row in rows if row["time"] + step <= end]


def _build_hybrid_gate(
    client: BinanceFuturesClient,
    symbol: str,
    rows: list[dict[str, Any]],
    *,
    lookback_days: int,
    adx_enabled: bool,
) -> Gate | None:
    """Hybrid 1h/15m trend gate covering the simulation window."""

    if not symbol:
        return None
    end = timezone.now()
    start = end - timedelta(days=lookback_days + HYBRID_WARMUP_DAYS)
    klines_1h = _closed_klines(client, symbol, "1h", start, end)
    klines_15m = _closed_klines(client, symbol, "15m", start, end)
    gate = build_gate(rows, klines_1h, klines_15m, adx_enabled=adx_enabled)
    return gate if gate.allow else None


def run_daily_analysis(
    *,
    trigger: str = "scheduled",
    params: dict[str, Any] | None = None,
    job_run_id=None,
    trend_mode: str = AnalysisRun.Mode.NORMAL,
) -> AnalysisRun:
    if trend_mode not in AnalysisRun.Mode.values:
        raise ValueError(f"Unknown analysis mode: {trend_mode}")
    hybrid = trend_mode == AnalysisRun.Mode.HYBRID
    analysis_params = _analysis_params(params)
    cfg = settings.ENGINE
    config = engine_config()
    base_params = base_grid_params()
    interval = analysis_params["interval"]
    hybrid_days = max(120, int(cfg["HYBRID_LOOKBACK_DAYS"]))
    # Hybrid results only count bars inside the fetched 1h/15m coverage, so the
    # simulation window is clamped to the hybrid lookback.
    lookback_days = (
        min(analysis_params["lookback_days"], hybrid_days)
        if hybrid
        else analysis_params["lookback_days"]
    )
    hybrid_adx = _hybrid_adx_enabled() if hybrid else False
    warmup_days = int(cfg["WARMUP_BUFFER_DAYS"])

    analysis = AnalysisRun.objects.create(
        status=AnalysisRun.Status.RUNNING,
        trigger=trigger,
        mode=trend_mode,
        scope=(
            AnalysisRun.Scope.SOLO
            if analysis_params["symbols"]
            else AnalysisRun.Scope.UNIVERSE
        ),
        params={
            **analysis_params,
            "lookback_days": lookback_days,
            "mode": trend_mode,
            "hybrid_adx": hybrid_adx,
            "config": {
                "capital": config.capital,
                "leverage": config.leverage,
                "maker_fee_rate": config.maker_fee_rate,
                "taker_fee_rate": config.taker_fee_rate,
                "slippage_bps": config.slippage_bps,
                "stop_loss_pct": base_params.stop_loss_pct,
                "initial_inventory_pct": base_params.initial_inventory_pct,
                "min_adv_usd": cfg["MIN_ADV_USD"],
                "min_oos_trades": cfg["MIN_OOS_TRADES"],
                "max_drawdown": cfg["MAX_DRAWDOWN"],
                "min_data_days": cfg["MIN_DATA_DAYS"],
                "min_atr_pct": cfg["MIN_ATR_PCT"],
                "max_atr_pct": cfg["MAX_ATR_PCT"],
                "pf_review": cfg["PF_REVIEW"],
                "pf_validate": cfg["PF_VALIDATE"],
                "cost_stress_multiplier": cfg["COST_STRESS_MULTIPLIER"],
                "max_spread_bps": cfg["MAX_SPREAD_BPS"],
                "min_depth_cover": cfg["MIN_DEPTH_COVER"],
                "max_correlation": cfg["MAX_CORRELATION"],
            },
        },
    )
    log_decision(
        "analysis",
        f"Analysis run {analysis.id} started ({trigger}, {trend_mode}).",
        analysis=analysis,
        job_run_id=job_run_id,
        payload={**analysis_params, "mode": trend_mode},
    )

    client = BinanceFuturesClient()
    coins = Coin.objects.filter(analysis_eligible=True)
    if analysis_params["symbols"]:
        # Explicit symbols bypass the top-100 universe so manually added coins
        # can be analysed without joining the recurring scans.
        coins = coins.filter(symbol__in=analysis_params["symbols"])
    else:
        coins = coins.filter(in_top100=True)
    coins = coins.order_by("rank", "symbol")
    if analysis_params["limit"]:
        coins = coins[: analysis_params["limit"]]
    coin_list = list(coins)
    if not coin_list:
        message = (
            f"No analysis-eligible coins found for {', '.join(analysis_params['symbols'])}."
            if analysis_params["symbols"]
            else "No eligible top-100 coins found. Run the monthly coin refresh first."
        )
        analysis.status = AnalysisRun.Status.FAILED
        analysis.error = message
        analysis.finished_at = timezone.now()
        analysis.duration_seconds = (analysis.finished_at - analysis.started_at).total_seconds()
        analysis.save(update_fields=["status", "error", "finished_at", "duration_seconds"])
        log_decision(
            "analysis", message, severity="error", analysis=analysis, job_run_id=job_run_id
        )
        return analysis

    results: list[dict[str, Any]] = []
    data_errors: list[dict[str, Any]] = []
    latest_cutoff: datetime | None = None
    data_start_floor = timezone.now() - timedelta(days=lookback_days + warmup_days)
    started_monotonic = time.monotonic()

    for index, coin in enumerate(coin_list, start=1):
        sync_info: dict[str, Any] = {}
        # The hybrid run follows the normal run in the same job and reuses the
        # 4h candles it just synced; only the 1h/15m gate inputs are fetched.
        if not hybrid:
            try:
                sync_info = sync_coin_market_data(
                    coin,
                    client,
                    lookback_days=lookback_days,
                    interval=interval,
                    warmup_days=warmup_days,
                    force=bool(analysis_params["force"]),
                )
                if not sync_info.get("ok"):
                    data_errors.append({"symbol": coin.symbol, "error": sync_info.get("error")})
            except Exception as exc:  # keep going on per-coin failures
                logger.warning("Market data sync failed for %s: %s", coin.symbol, exc)
                data_errors.append({"symbol": coin.symbol, "error": str(exc)})
                sync_info = {"ok": False, "error": str(exc)}

        rows = load_candles(coin.binance_symbol, interval, data_start_floor, timezone.now())
        funding = load_funding(coin.binance_symbol, data_start_floor, timezone.now())
        if rows:
            latest_cutoff = max(latest_cutoff or rows[-1]["time"], rows[-1]["time"])
        features = compute_features(rows, funding, base_params, config) if len(rows) >= 50 else {
            "insufficient_data": True,
            "bars": len(rows),
        }

        item: dict[str, Any] = {
            "coin": coin,
            "eligible": True,
            "exclusion_reason": "",
            "oos_metrics": {},
            "in_sample_metrics": {},
            "features": features,
            "component_scores": {},
            "params": base_params.to_dict(),
            "folds": [],
            "chart": {},
            "trade_sample": [],
            "diagnostics": {},
            "_daily_returns": daily_returns(rows),
            "data_start": rows[0]["time"] if rows else None,
            "data_end": rows[-1]["time"] if rows else None,
        }

        gate: Gate | None = None
        if hybrid and len(rows) >= 200:
            try:
                gate = _build_hybrid_gate(
                    client,
                    coin.binance_symbol,
                    rows,
                    lookback_days=lookback_days,
                    adx_enabled=hybrid_adx,
                )
            except Exception as exc:  # noqa: BLE001 - keep the rest of the universe
                logger.warning("Hybrid kline fetch failed for %s: %s", coin.symbol, exc)
                data_errors.append({"symbol": coin.symbol, "error": str(exc)})

        if len(rows) < 200:
            item["eligible"] = False
            item["exclusion_reason"] = (
                f"insufficient candle history ({len(rows)} bars)"
                + (f"; sync error: {sync_info.get('error')}" if sync_info.get("error") else "")
            )
        elif hybrid and gate is None:
            item["eligible"] = False
            item["exclusion_reason"] = "hybrid trend data unavailable"
        else:
            try:
                wf = run_walk_forward(
                    rows,
                    funding,
                    config=config,
                    base_params=base_params,
                    lookback_days=lookback_days,
                    min_trades=int(cfg["MIN_OOS_TRADES"]),
                    cost_stress_multiplier=float(cfg["COST_STRESS_MULTIPLIER"]),
                    sensitivity=bool(cfg["WALKFORWARD_SENSITIVITY"]),
                    gate=gate,
                )
            except Exception as exc:  # noqa: BLE001 - protect the rest of the universe
                logger.exception("Backtest failed for %s", coin.symbol)
                wf = None
                item["eligible"] = False
                item["exclusion_reason"] = f"backtest error: {exc}"
            if wf is not None:
                if wf.error:
                    item["eligible"] = False
                    item["exclusion_reason"] = wf.error
                else:
                    recommended = GridParams.from_dict(wf.params) if wf.params else base_params
                    features = {
                        **features,
                        **compute_features(rows, funding, recommended, config),
                    }
                    item.update(
                        {
                            "oos_metrics": wf.oos_metrics,
                            "in_sample_metrics": wf.in_sample_metrics,
                            "features": features,
                            "params": wf.params,
                            "folds": wf.folds,
                            "chart": wf.chart,
                            "trade_sample": wf.trade_sample,
                            "data_start": wf.data_start,
                            "data_end": wf.data_end,
                            "_wf_diagnostics": wf.diagnostics,
                        }
                    )
                    if cfg["ORDER_BOOK_ENABLED"]:
                        recommended = GridParams.from_dict(wf.params or {})
                        book = _order_book_features(
                            client,
                            coin.binance_symbol,
                            capital=config.capital,
                            leverage=config.leverage,
                            max_utilization=recommended.max_utilization,
                            grid_levels=recommended.grid_levels,
                            band_bps=float(cfg["DEPTH_BAND_BPS"]),
                        )
                        if book:
                            item["features"] = {**features, **book}
                    if gate is not None:
                        item["features"] = {
                            **item["features"],
                            "hybrid_gate": gate.stats(),
                        }
        results.append(item)

        if index % 10 == 0 or index == len(coin_list):
            logger.info(
                "Analysis %s: %s/%s coins processed in %.1fs",
                analysis.id,
                index,
                len(coin_list),
                time.monotonic() - started_monotonic,
            )

    # Scoring runs the hard gates first, then correlates only the coins that
    # passed them, so an ineligible coin cannot change the diversification of
    # the ranked universe.
    correlation_info: dict[str, Any] = {}
    ranked = score_results(
        results,
        min_adv_usd=float(cfg["MIN_ADV_USD"]),
        min_trades=int(cfg["MIN_OOS_TRADES"]),
        max_drawdown=float(cfg["MAX_DRAWDOWN"]),
        min_atr_pct=float(cfg["MIN_ATR_PCT"]),
        max_atr_pct=float(cfg["MAX_ATR_PCT"]),
        pf_review=float(cfg["PF_REVIEW"]),
        pf_validate=float(cfg["PF_VALIDATE"]),
        max_spread_bps=float(cfg["MAX_SPREAD_BPS"]),
        min_depth_cover=float(cfg["MIN_DEPTH_COVER"]),
        enforce_cost_stress=bool(cfg["ENFORCE_COST_STRESS"]),
        min_stressed_return=float(cfg["MIN_STRESSED_RETURN"]),
        max_correlation=float(cfg["MAX_CORRELATION"]),
        max_top_trade_share=float(cfg["MAX_TOP_TRADE_SHARE"]),
        min_data_days=int(cfg["MIN_DATA_DAYS"]),
        corr_lookback_days=int(cfg["CORR_LOOKBACK_DAYS"]),
        correlation_out=correlation_info,
    )

    saved: list[CoinAnalysis] = []
    with transaction.atomic():
        for item in ranked:
            saved.append(
                CoinAnalysis.objects.create(
                    analysis=analysis,
                    coin=item["coin"],
                    rank=item.get("rank"),
                    score=item.get("score", 0.0),
                    suitability=item.get("suitability", CoinAnalysis.Suitability.INELIGIBLE),
                    eligible=bool(item.get("eligible")),
                    exclusion_reason=(item.get("exclusion_reason") or "")[:255],
                    oos_metrics=item.get("oos_metrics") or {},
                    in_sample_metrics=item.get("in_sample_metrics") or {},
                    features=item.get("features") or {},
                    component_scores=item.get("component_scores") or {},
                    params=item.get("params") or {},
                    folds=item.get("folds") or [],
                    chart=item.get("chart") or {},
                    trade_sample=item.get("trade_sample") or [],
                    diagnostics=item.get("diagnostics") or {},
                    data_start=item.get("data_start"),
                    data_end=item.get("data_end"),
                )
            )

    analysis.data_cutoff = latest_cutoff
    analysis.summary = ranking_summary(ranked)
    analysis.summary.update(
        {
            "requested": len(coin_list),
            "data_errors": len(data_errors),
            "data_error_samples": data_errors[:10],
            "lookback_days": lookback_days,
            "interval": interval,
            "runtime_seconds": round(time.monotonic() - started_monotonic, 1),
            "correlation": correlation_info,
            "thresholds": {
                "min_adv_usd": cfg["MIN_ADV_USD"],
                "min_oos_trades": cfg["MIN_OOS_TRADES"],
                "max_drawdown": cfg["MAX_DRAWDOWN"],
                "min_data_days": cfg["MIN_DATA_DAYS"],
                "min_atr_pct": cfg["MIN_ATR_PCT"],
                "max_atr_pct": cfg["MAX_ATR_PCT"],
                "pf_review": cfg["PF_REVIEW"],
                "pf_validate": cfg["PF_VALIDATE"],
                "cost_stress_multiplier": cfg["COST_STRESS_MULTIPLIER"],
                "max_spread_bps": cfg["MAX_SPREAD_BPS"],
                "min_depth_cover": cfg["MIN_DEPTH_COVER"],
                "max_correlation": cfg["MAX_CORRELATION"],
                "max_top_trade_share": cfg["MAX_TOP_TRADE_SHARE"],
            },
        }
    )
    analysis.save(update_fields=["data_cutoff", "summary"])

    analysis.summary["mode"] = trend_mode
    if hybrid:
        gate_rows = [
            item["features"]["hybrid_gate"]
            for item in ranked
            if (item.get("features") or {}).get("hybrid_gate")
        ]
        analysis.summary["hybrid"] = {
            "adx_enabled": hybrid_adx,
            "lookback_days": lookback_days,
            "warmup_days": HYBRID_WARMUP_DAYS,
            "covered_symbols": len(gate_rows),
            "blocked_pct_avg": round(
                sum(float(row.get("blocked_pct") or 0.0) for row in gate_rows)
                / max(1, len(gate_rows)),
                4,
            ),
            "confirmed_exits": sum(
                int(row.get("confirmed_exits") or 0) for row in gate_rows
            ),
        }

    config_payload = {
        "min_adv_usd": cfg["MIN_ADV_USD"],
        "min_oos_trades": cfg["MIN_OOS_TRADES"],
        "max_drawdown": cfg["MAX_DRAWDOWN"],
        "min_data_days": cfg["MIN_DATA_DAYS"],
        "min_atr_pct": cfg["MIN_ATR_PCT"],
        "max_atr_pct": cfg["MAX_ATR_PCT"],
        "pf_review": cfg["PF_REVIEW"],
        "pf_validate": cfg["PF_VALIDATE"],
        "cost_stress_multiplier": cfg["COST_STRESS_MULTIPLIER"],
        "max_spread_bps": cfg["MAX_SPREAD_BPS"],
        "min_depth_cover": cfg["MIN_DEPTH_COVER"],
        "max_correlation": cfg["MAX_CORRELATION"],
        "max_top_trade_share": cfg["MAX_TOP_TRADE_SHARE"],
    }
    # Solo runs must not displace the universe ranking/performance reports, and
    # hybrid runs are diagnostics rather than the published report set.
    if analysis.scope == AnalysisRun.Scope.UNIVERSE and not hybrid:
        try:
            reports = build_reports(analysis, ranked, config_payload)
            analysis.summary["report_ids"] = [str(report.id) for report in reports]
        except Exception as exc:  # noqa: BLE001
            logger.exception("Report generation failed")
            analysis.summary["report_error"] = str(exc)

    if not hybrid:
        try:
            from apps.trading.paper import ensure_paper_sessions

            candidates = [
                item
                for item in ranked
                if item.get("eligible")
                and item.get("suitability") in {"prime", "viable"}
            ]
            selected, skipped = select_diversified(
                candidates,
                max_corr=float(cfg["MAX_CORRELATION"]),
                limit=int(cfg["TOP_N_PAPER"]),
            )
            sessions = ensure_paper_sessions(analysis, selected)
            analysis.summary["paper_sessions_created"] = len(sessions)
            analysis.summary["paper_symbols"] = [s.coin.symbol for s in sessions]
            analysis.summary["paper_skipped_correlation"] = skipped
            if skipped:
                log_decision(
                    "paper",
                    "Correlation filter skipped duplicate paper candidates: "
                    + ", ".join(
                        f"{row['symbol']} ({row['correlation']:.2f} with {row['correlated_with']})"
                        for row in skipped
                    ),
                    severity="warning",
                    analysis=analysis,
                    job_run_id=job_run_id,
                    payload={"skipped": skipped},
                )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Paper session bootstrap failed")
            analysis.summary["paper_session_error"] = str(exc)

    flagged = [item for item in ranked if item.get("flags")]
    if flagged:
        log_decision(
            "analysis",
            f"{len(flagged)} coin(s) carry review flags; see the ranking report "
            "for profit-factor, concentration, regime, and cost-stress warnings.",
            severity="warning",
            analysis=analysis,
            job_run_id=job_run_id,
            payload={
                "flags": analysis.summary.get("flag_counts"),
                "symbols": [item["coin"].symbol for item in flagged][:30],
            },
        )

    top_symbols = [item["coin"].symbol for item in ranked if item.get("eligible")][:10]
    analysis.status = AnalysisRun.Status.SUCCEEDED
    analysis.finished_at = timezone.now()
    analysis.duration_seconds = (analysis.finished_at - analysis.started_at).total_seconds()
    analysis.summary["duration_seconds"] = analysis.duration_seconds
    analysis.save(update_fields=["status", "finished_at", "duration_seconds", "summary"])
    log_decision(
        "analysis",
        f"Analysis run {analysis.id} finished: {analysis.summary.get('eligible')}/"
        f"{analysis.summary.get('universe')} eligible. Top: {', '.join(top_symbols) or 'none'}.",
        analysis=analysis,
        job_run_id=job_run_id,
        payload={
            "eligible": analysis.summary.get("eligible"),
            "top_symbols": top_symbols,
            "duration_seconds": analysis.duration_seconds,
        },
    )
    return analysis


def latest_analysis(mode: str = AnalysisRun.Mode.NORMAL) -> AnalysisRun | None:
    """Latest succeeded universe run of ``mode``, falling back to any run."""

    succeeded = AnalysisRun.objects.filter(status=AnalysisRun.Status.SUCCEEDED)
    if mode:
        succeeded = succeeded.filter(mode=mode)
    return (
        succeeded.filter(scope=AnalysisRun.Scope.UNIVERSE).order_by("-started_at").first()
        or succeeded.order_by("-started_at").first()
    )
