"""Daily analysis orchestration: sync data, backtest, score, persist, report."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone as dt_timezone
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.common.binance import BinanceFuturesClient
from apps.coins.models import Coin
from apps.engine.binance_sync import sync_coin_market_data
from apps.engine.features import compute_features
from apps.engine.grid import GridParams, SimulationConfig
from apps.engine.models import AnalysisRun, CoinAnalysis, log_decision
from apps.engine.reports import build_reports
from apps.engine.scoring import ranking_summary, score_results
from apps.engine.timescale import load_candles, load_funding
from apps.engine.walkforward import run_walk_forward

logger = logging.getLogger("engine")


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


def run_daily_analysis(
    *,
    trigger: str = "scheduled",
    params: dict[str, Any] | None = None,
    job_run_id=None,
) -> AnalysisRun:
    analysis_params = _analysis_params(params)
    cfg = settings.ENGINE
    config = engine_config()
    base_params = base_grid_params()
    interval = analysis_params["interval"]
    lookback_days = analysis_params["lookback_days"]
    warmup_days = int(cfg["WARMUP_BUFFER_DAYS"])

    analysis = AnalysisRun.objects.create(
        status=AnalysisRun.Status.RUNNING,
        trigger=trigger,
        params={
            **analysis_params,
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
            },
        },
    )
    log_decision(
        "analysis",
        f"Analysis run {analysis.id} started ({trigger}).",
        analysis=analysis,
        job_run_id=job_run_id,
        payload=analysis_params,
    )

    client = BinanceFuturesClient()
    coins = Coin.objects.filter(in_top100=True, analysis_eligible=True).order_by("rank", "symbol")
    if analysis_params["symbols"]:
        coins = coins.filter(symbol__in=analysis_params["symbols"])
    if analysis_params["limit"]:
        coins = coins[: analysis_params["limit"]]
    coin_list = list(coins)
    if not coin_list:
        message = "No eligible top-100 coins found. Run the monthly coin refresh first."
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
            "data_start": rows[0]["time"] if rows else None,
            "data_end": rows[-1]["time"] if rows else None,
        }

        if len(rows) < 200:
            item["eligible"] = False
            item["exclusion_reason"] = (
                f"insufficient candle history ({len(rows)} bars)"
                + (f"; sync error: {sync_info.get('error')}" if sync_info.get("error") else "")
            )
        else:
            try:
                wf = run_walk_forward(
                    rows,
                    funding,
                    config=config,
                    base_params=base_params,
                    lookback_days=lookback_days,
                    min_trades=int(cfg["MIN_OOS_TRADES"]),
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
                        }
                    )
        results.append(item)

        if index % 10 == 0 or index == len(coin_list):
            logger.info(
                "Analysis %s: %s/%s coins processed in %.1fs",
                analysis.id,
                index,
                len(coin_list),
                time.monotonic() - started_monotonic,
            )

    ranked = score_results(
        results,
        min_adv_usd=float(cfg["MIN_ADV_USD"]),
        min_trades=int(cfg["MIN_OOS_TRADES"]),
        max_drawdown=float(cfg["MAX_DRAWDOWN"]),
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
        }
    )
    analysis.save(update_fields=["data_cutoff", "summary"])

    config_payload = {
        "min_adv_usd": cfg["MIN_ADV_USD"],
        "min_oos_trades": cfg["MIN_OOS_TRADES"],
        "max_drawdown": cfg["MAX_DRAWDOWN"],
    }
    try:
        reports = build_reports(analysis, ranked, config_payload)
        analysis.summary["report_ids"] = [str(report.id) for report in reports]
    except Exception as exc:  # noqa: BLE001
        logger.exception("Report generation failed")
        analysis.summary["report_error"] = str(exc)

    try:
        from apps.trading.paper import ensure_paper_sessions

        selected = [
            item
            for item in ranked
            if item.get("eligible")
            and item.get("suitability") in {"prime", "viable"}
        ][: int(cfg["TOP_N_PAPER"])]
        sessions = ensure_paper_sessions(analysis, selected)
        analysis.summary["paper_sessions_created"] = len(sessions)
        analysis.summary["paper_symbols"] = [s.coin.symbol for s in sessions]
    except Exception as exc:  # noqa: BLE001
        logger.exception("Paper session bootstrap failed")
        analysis.summary["paper_session_error"] = str(exc)

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


def latest_analysis() -> AnalysisRun | None:
    return (
        AnalysisRun.objects.filter(status=AnalysisRun.Status.SUCCEEDED)
        .order_by("-started_at")
        .first()
    )
