"""Daily analysis orchestration: sync data, backtest, score, persist, report."""

from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from datetime import datetime, timedelta, timezone as dt_timezone
from types import SimpleNamespace
from typing import Any

from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone

from apps.common.binance import INTERVAL_MS, BinanceFuturesClient
from apps.coins.models import Coin
from apps.engine.binance_sync import sync_coin_market_data
from apps.engine.features import compute_features
from apps.engine.grid import GridParams, SimulationConfig
from apps.engine.hybrid_gate import Gate, build_gate
from apps.engine.models import AnalysisRun, CoinAnalysis, log_decision
from apps.engine.regime_timeline import RegimeTimeline, build_regime_timeline, gate_from_timeline
from apps.engine.reports import build_reports
from apps.engine.versioning import resolve_config_version, resolve_strategy_version
from apps.engine.robustness import (
    correlation_diagnostics,
    daily_returns,
    market_exposure,
    select_diversified,
)
from apps.engine.scoring import ranking_summary, score_results
from apps.engine.timescale import (
    earliest_candle_time,
    latest_candle_time,
    load_candles,
    load_funding,
)
from apps.engine.validation import (
    MIN_SCOPE_COINS,
    findings,
    pooled_ic,
    score_validation,
    strategy_validation,
)
from apps.engine.walkforward import WalkForwardResult, walk_forward_job as _walk_forward_job
from apps.trading.models import OpenInterestPoint
from apps.trading.regime import SYMBOL as REGIME_SYMBOL, WARMUP_1H_DAYS, RegimeConfig

logger = logging.getLogger("engine")

HYBRID_WARMUP_DAYS = 10
# Strategy returns exist only on walk-forward test days and the holdout, so
# use all of them (~11 months) rather than a short price-style window.
STRATEGY_CORR_LOOKBACK_DAYS = 365


def _strategy_settings():
    from apps.trading.models import StrategySettings

    return StrategySettings.load()


def engine_config(interval: str | None = None) -> SimulationConfig:
    """Backtest configuration shared with paper trading.

    The candle interval and fill model come from the admin strategy settings,
    so the daily backtest always measures the strategy paper trading runs.
    """

    cfg = settings.ENGINE
    strategy = _strategy_settings()
    return SimulationConfig(
        capital=float(cfg["PAPER_CAPITAL"]),
        leverage=float(cfg["LEVERAGE"]),
        maker_fee_rate=float(cfg["MAKER_FEE_RATE"]),
        taker_fee_rate=float(cfg["TAKER_FEE_RATE"]),
        slippage_bps=float(cfg["SLIPPAGE_BPS"]),
        interval=str(interval or strategy.signal_interval or cfg["INTERVAL"]),
        trade_through_bps=strategy.effective_trade_through_bps,
    )


def analysis_workers() -> int:
    """Process count for the per-coin walk-forward (1 = run in-process)."""

    configured = int(settings.ENGINE.get("WORKERS", 0) or 0)
    if configured > 0:
        return configured
    return max(1, min(6, (os.cpu_count() or 2) - 1))



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
        "interval": _strategy_settings().signal_interval or cfg["INTERVAL"],
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


def _closed(rows: list[dict[str, Any]], interval: str, end: datetime) -> list[dict[str, Any]]:
    """Drop the candle that is still forming at ``end``."""

    step = timedelta(milliseconds=INTERVAL_MS[interval])
    return [row for row in rows if row["time"] + step <= end]


def _sync_window(
    coin,
    client: BinanceFuturesClient,
    interval: str,
    *,
    lookback_days: int,
    warmup_days: int,
) -> dict[str, Any]:
    """Sync ``coin``'s ``interval`` candles over lookback + warm-up days.

    The sync is incremental from the newest stored candle, so history stored
    for a shorter window is back-filled once with a full fetch.
    """

    start = timezone.now() - timedelta(days=lookback_days + warmup_days)
    earliest = earliest_candle_time(coin.binance_symbol, interval)
    return sync_coin_market_data(
        coin,
        client,
        lookback_days=lookback_days,
        interval=interval,
        warmup_days=warmup_days,
        force=earliest is None or earliest > start + timedelta(days=1),
    )


def _build_hybrid_gate(
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
    klines_1h = _closed(load_candles(symbol, "1h", start, end), "1h", end)
    klines_15m = _closed(load_candles(symbol, "15m", start, end), "15m", end)
    gate = build_gate(rows, klines_1h, klines_15m, adx_enabled=adx_enabled)
    return gate if gate.allow else None


# The latest BTC regime timeline, keyed by (last BTC 15m bar, regime config)
# and stored with its start: the comparison run that follows the primary run
# in the same job reuses it, and a run needing an earlier start rebuilds it.
_REGIME_TIMELINE_CACHE: dict[tuple, tuple[datetime, RegimeTimeline]] = {}


def _regime_config_key(config: RegimeConfig) -> tuple:
    return (
        config.trailing_stop_pct,
        config.trailing_window_days,
        config.oi_flush_pct,
        config.oi_near_high_pct,
        config.funding_z,
        config.min_bearish_hours,
        tuple(sorted((str(k), str(v)) for k, v in (config.risk or {}).items())),
    )


def _regime_timeline(
    client: BinanceFuturesClient,
    strategy_settings,
    *,
    lookback_days: int,
    warmup_days: int,
    sync: bool,
) -> RegimeTimeline | None:
    """BTC regime timeline over the analysis window, or None when disabled."""

    if not strategy_settings.btc_regime_enabled:
        return None
    btc = SimpleNamespace(binance_symbol=REGIME_SYMBOL)
    regime_warmup = warmup_days + WARMUP_1H_DAYS + 2
    end = timezone.now()
    start = end - timedelta(days=lookback_days + regime_warmup)
    if sync:
        for interval in ("15m", "1h"):
            _sync_window(
                btc,
                client,
                interval,
                lookback_days=lookback_days,
                warmup_days=regime_warmup,
            )
    config = RegimeConfig.from_settings(strategy_settings)
    last_bar = latest_candle_time(REGIME_SYMBOL, "15m")
    if last_bar is None:
        return None
    key = (last_bar, _regime_config_key(config))
    cached = _REGIME_TIMELINE_CACHE.get(key)
    if cached is not None and cached[0] <= start:
        return cached[1]
    oi_points = list(
        OpenInterestPoint.objects.filter(symbol=REGIME_SYMBOL, time__gte=start).values(
            "time", "open_interest"
        )
    )
    timeline = build_regime_timeline(
        load_candles(REGIME_SYMBOL, "15m", start, end),
        load_candles(REGIME_SYMBOL, "1h", start, end),
        load_funding(REGIME_SYMBOL, start, end),
        oi_points,
        config,
    )
    _REGIME_TIMELINE_CACHE.clear()
    _REGIME_TIMELINE_CACHE[key] = (start, timeline)
    return timeline


# Arbitrary constant key for the Postgres advisory lock that serialises
# analysis runs ("grid" in ASCII).
ANALYSIS_LOCK_KEY = 0x67726964


class AnalysisAlreadyRunning(RuntimeError):
    pass


@contextmanager
def _analysis_lock():
    """Hold a session advisory lock for the whole analysis run.

    The job queue already allows one active ``daily_analysis`` job; this lock
    also covers runs started outside the queue (a shell, a script, a job
    whose row was reaped while its process lived on). It is released when the
    run ends or, if the process dies, when its connection closes.
    """

    if connection.vendor != "postgresql":
        yield
        return
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", [ANALYSIS_LOCK_KEY])
        acquired = bool(cursor.fetchone()[0])
    if not acquired:
        raise AnalysisAlreadyRunning(
            "Another grid suitability analysis is already running; this run was not started."
        )
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s)", [ANALYSIS_LOCK_KEY])


def _bootstrap_paper_sessions(
    analysis: AnalysisRun,
    ranked: list[dict[str, Any]],
    strategy_settings,
    job_run_id=None,
) -> None:
    """Retire stale paper sessions and open the selected candidates."""

    from apps.trading.models import MarketRegimeState
    from apps.trading.paper import ensure_paper_sessions, retire_stale_sessions
    from apps.trading.portfolio import open_symbols

    candidates = [
        item
        for item in ranked
        if item.get("eligible") and item.get("suitability") in {"prime", "viable"}
    ]
    if analysis.scope == AnalysisRun.Scope.UNIVERSE:
        analysis.summary["paper_retired"] = retire_stale_sessions(
            analysis, candidates, strategy_settings
        )
    # A bearish BTC regime would flatten a new session on the next tick, so
    # none is opened; ranking and reports are unaffected. In warning, sessions
    # open normally and the tick keeps their buys blocked.
    regime = MarketRegimeState.load()
    if strategy_settings.btc_regime_enabled and regime.phase == MarketRegimeState.Phase.BEARISH:
        analysis.summary["paper_blocked_by_regime"] = True
        analysis.summary["paper_sessions_created"] = 0
        analysis.summary["paper_symbols"] = []
        log_decision(
            "paper",
            "BTC market regime is bearish; no new paper sessions opened "
            f"({len(candidates)} candidate(s) ranked).",
            severity="warning",
            analysis=analysis,
            job_run_id=job_run_id,
            payload={"regime_reasons": list(regime.reasons or []), "candidates": len(candidates)},
        )
        return
    held = open_symbols()
    slots = max(0, int(strategy_settings.max_sessions) - len(held))
    selected, skipped = select_diversified(
        [item for item in candidates if item["coin"].symbol not in held],
        max_corr=float(strategy_settings.max_strategy_correlation),
        limit=slots,
        existing=held,
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


def run_daily_analysis(
    *,
    trigger: str = "scheduled",
    params: dict[str, Any] | None = None,
    job_run_id=None,
    trend_mode: str = AnalysisRun.Mode.NORMAL,
    drives_sessions: bool = False,
) -> AnalysisRun:
    """Run one analysis; refuses to start while another one is running.

    Only a run with ``drives_sessions`` builds the published reports and opens
    (and, for universe runs, retires) paper sessions; any other run is a ranking for comparison.
    """

    with _analysis_lock():
        return _run_daily_analysis(
            trigger=trigger,
            params=params,
            job_run_id=job_run_id,
            trend_mode=trend_mode,
            drives_sessions=drives_sessions,
        )


def _run_daily_analysis(
    *,
    trigger: str,
    params: dict[str, Any] | None,
    job_run_id,
    trend_mode: str,
    drives_sessions: bool = False,
) -> AnalysisRun:
    if trend_mode not in AnalysisRun.Mode.values:
        raise ValueError(f"Unknown analysis mode: {trend_mode}")
    hybrid = trend_mode == AnalysisRun.Mode.HYBRID
    analysis_params = _analysis_params(params)
    cfg = settings.ENGINE
    strategy_settings = _strategy_settings()
    interval = analysis_params["interval"]
    config = engine_config(interval)
    base_params = base_grid_params()
    holdout_days = int(strategy_settings.holdout_days or 0)
    lookback_days = analysis_params["lookback_days"]
    hybrid_adx = _hybrid_adx_enabled() if hybrid else False
    warmup_days = int(cfg["WARMUP_BUFFER_DAYS"])

    analysis = AnalysisRun.objects.create(
        strategy_version=resolve_strategy_version(),
        status=AnalysisRun.Status.RUNNING,
        trigger=trigger,
        mode=trend_mode,
        drives_sessions=drives_sessions,
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
                "trade_through_bps": config.trade_through_bps,
                "fill_model": strategy_settings.fill_model,
                "holdout_days": holdout_days,
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
                "max_correlation": float(strategy_settings.max_strategy_correlation),
                "max_price_correlation": cfg["MAX_CORRELATION"],
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

    # The BTC regime timeline is built once and gates every coin. A failure
    # leaves the run unregimed rather than failing it; the summary says so.
    regime_timeline: RegimeTimeline | None = None
    regime_info: dict[str, Any] = {"enabled": bool(strategy_settings.btc_regime_enabled)}
    try:
        regime_timeline = _regime_timeline(
            client,
            strategy_settings,
            lookback_days=lookback_days,
            warmup_days=warmup_days,
            sync=True,
        )
        if regime_timeline is not None:
            regime_info.update(regime_timeline.stats())
        elif strategy_settings.btc_regime_enabled:
            regime_info["error"] = "no BTC 15m candles stored"
    except Exception as exc:  # noqa: BLE001 - the regime is an overlay, not a gate on the run
        logger.warning("BTC regime timeline failed: %s", exc)
        data_errors.append({"symbol": REGIME_SYMBOL, "error": f"BTC regime: {exc}"})
        regime_info["error"] = str(exc)

    workers = analysis_workers()
    executor = ProcessPoolExecutor(max_workers=workers) if workers > 1 else None
    pending: dict[Future, dict[str, Any]] = {}
    processed = 0
    hybrid_attempted = 0
    hybrid_covered = 0

    def finish(job: dict[str, Any], wf: WalkForwardResult | None, error: str = "") -> None:
        """Merge a walk-forward result into its item (runs in this process)."""

        nonlocal processed
        item = job["item"]
        coin = job["coin"]
        gate = job["gate"]
        if wf is None:
            item["eligible"] = False
            item["exclusion_reason"] = f"backtest error: {error}"
        elif wf.error:
            item["eligible"] = False
            item["exclusion_reason"] = wf.error
        else:
            recommended = GridParams.from_dict(wf.params) if wf.params else base_params
            features = {
                **item["features"],
                **compute_features(job["rows"], job["funding"], recommended, config),
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
                    "_strategy_returns": wf.strategy_returns,
                }
            )
            if cfg["ORDER_BOOK_ENABLED"]:
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
                    item["features"] = {**item["features"], **book}
            if gate is not None:
                item["features"] = {**item["features"], "hybrid_gate": gate.stats()}
        processed += 1
        if processed % 10 == 0:
            logger.info(
                "Analysis %s: %s/%s coins processed in %.1fs",
                analysis.id,
                processed,
                len(coin_list),
                time.monotonic() - started_monotonic,
            )

    def drain(block_until: int) -> None:
        while len(pending) > block_until:
            done, _ = wait(list(pending), return_when=FIRST_COMPLETED)
            for future in done:
                job = pending.pop(future)
                try:
                    finish(job, future.result())
                except Exception as exc:  # noqa: BLE001 - protect the universe
                    logger.exception("Backtest failed for %s", job["coin"].symbol)
                    finish(job, None, str(exc))

    try:
        for coin in coin_list:
            sync_info: dict[str, Any] = {}
            # Every run syncs its own inputs. When a second run follows in the
            # same job the incremental sync finds nothing new and is cheap.
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
            if hybrid:
                # The trend gate reads stored 1h/15m candles over the window
                # _build_hybrid_gate uses.
                for gate_interval in ("1h", "15m"):
                    try:
                        gate_sync = _sync_window(
                            coin,
                            client,
                            gate_interval,
                            lookback_days=lookback_days,
                            warmup_days=HYBRID_WARMUP_DAYS,
                        )
                        if not gate_sync.get("ok"):
                            data_errors.append(
                                {"symbol": coin.symbol, "error": gate_sync.get("error")}
                            )
                    except Exception as exc:  # keep going on per-coin failures
                        logger.warning(
                            "%s sync failed for %s: %s", gate_interval, coin.symbol, exc
                        )
                        data_errors.append(
                            {"symbol": coin.symbol, "error": f"{gate_interval}: {exc}"}
                        )

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
            results.append(item)
            min_bars = 200

            gate: Gate | None = None
            if hybrid and len(rows) >= min_bars:
                hybrid_attempted += 1
                try:
                    gate = _build_hybrid_gate(
                        coin.binance_symbol,
                        rows,
                        lookback_days=lookback_days,
                        adx_enabled=hybrid_adx,
                    )
                except Exception as exc:  # noqa: BLE001 - keep the rest of the universe
                    logger.warning("Hybrid trend gate unavailable for %s: %s", coin.symbol, exc)
                    data_errors.append({"symbol": coin.symbol, "error": str(exc)})
                if gate is not None:
                    hybrid_covered += 1

            if len(rows) < min_bars:
                item["eligible"] = False
                item["exclusion_reason"] = (
                    f"insufficient candle history ({len(rows)} bars)"
                    + (f"; sync error: {sync_info.get('error')}" if sync_info.get("error") else "")
                )
                continue
            if hybrid and gate is None:
                item["eligible"] = False
                item["exclusion_reason"] = "hybrid trend data unavailable"
                continue

            job = {"coin": coin, "item": item, "rows": rows, "funding": funding, "gate": gate}
            kwargs = {
                "rows": rows,
                "funding_rows": funding,
                "config": config,
                "base_params": base_params,
                "lookback_days": lookback_days,
                "min_trades": int(cfg["MIN_OOS_TRADES"]),
                "cost_stress_multiplier": float(cfg["COST_STRESS_MULTIPLIER"]),
                "sensitivity": bool(cfg["WALKFORWARD_SENSITIVITY"]),
                "gate": gate,
                "holdout_days": holdout_days,
                "regime_gate": gate_from_timeline(rows, regime_timeline),
            }
            if executor is None:
                try:
                    finish(job, _walk_forward_job(kwargs))
                except Exception as exc:  # noqa: BLE001 - protect the rest of the universe
                    logger.exception("Backtest failed for %s", coin.symbol)
                    finish(job, None, str(exc))
            else:
                pending[executor.submit(_walk_forward_job, kwargs)] = job
                # Bound memory: at most two coins of candles queued per worker.
                drain(workers * 2)
        drain(0)
    finally:
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=True)

    # BTC is the market reference for every strategy's beta.
    btc_returns = next(
        (
            item["_daily_returns"]
            for item in results
            if item["coin"].binance_symbol == "BTCUSDT"
        ),
        None,
    )
    if btc_returns is None:
        try:
            btc_rows = load_candles("BTCUSDT", interval, data_start_floor, timezone.now())
            btc_returns = daily_returns(btc_rows) if btc_rows else None
        except Exception:  # noqa: BLE001 - beta is diagnostic only
            btc_returns = None
    for item in results:
        strategy_returns = item.get("_strategy_returns") or {}
        item["_market"] = (
            market_exposure(strategy_returns, btc_returns)
            if strategy_returns and btc_returns
            else {}
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
        max_top_trade_share=float(cfg["MAX_TOP_TRADE_SHARE"]),
        min_data_days=int(cfg["MIN_DATA_DAYS"]),
        # Diversification is measured on the strategies' own daily returns:
        # that is the exposure the paper book actually holds.
        max_correlation=float(strategy_settings.max_strategy_correlation),
        corr_lookback_days=STRATEGY_CORR_LOOKBACK_DAYS,
        correlation_out=correlation_info,
        correlation_returns_key="_strategy_returns",
    )
    price_correlation = correlation_diagnostics(
        ranked,
        lookback_days=int(cfg["CORR_LOOKBACK_DAYS"]),
        max_corr=float(cfg["MAX_CORRELATION"]),
        returns_key="_daily_returns",
        store_key="_price_correlation",
        # Same scope as the strategy diagnostics below.
        eligible_only=sum(1 for item in ranked if item.get("eligible")) >= MIN_SCOPE_COINS,
    )
    # Diagnostic only: strategy correlation across every analysed coin, so a
    # run where nothing qualifies can still show how concentrated the
    # strategies are. Stored under its own key; it never feeds the ranking.
    universe_correlation = correlation_diagnostics(
        ranked,
        lookback_days=STRATEGY_CORR_LOOKBACK_DAYS,
        max_corr=float(strategy_settings.max_strategy_correlation),
        returns_key="_strategy_returns",
        store_key="_universe_correlation",
        eligible_only=False,
    )
    for item in ranked:
        diagnostics = item.setdefault("diagnostics", {})
        diagnostics["market"] = item.get("_market") or {}
        diagnostics["price_correlation"] = {
            key: value
            for key, value in (item.get("_price_correlation") or {}).items()
            if key != "peer_map"
        }

    saved: list[CoinAnalysis] = []
    with transaction.atomic():
        for item in ranked:
            saved.append(
                CoinAnalysis.objects.create(
                    config_version=resolve_config_version(
                        item["coin"].binance_symbol, item.get("params") or {}
                    ),
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
            "universe_correlation": universe_correlation,
            "price_correlation": {
                key: value for key, value in price_correlation.items() if key != "pairs"
            },
            "workers": workers,
            "holdout_days": holdout_days,
            "fill_model": strategy_settings.fill_model,
            "trade_through_bps": config.trade_through_bps,
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
                "max_correlation": float(strategy_settings.max_strategy_correlation),
                "max_price_correlation": cfg["MAX_CORRELATION"],
                "max_top_trade_share": cfg["MAX_TOP_TRADE_SHARE"],
            },
        }
    )
    # Score validity and strategy correlation, judged on the untouched holdout.
    scores_check = score_validation(ranked)
    strategies_check = strategy_validation(
        ranked, correlation_info, price_correlation, universe_correlation
    )
    history = list(
        AnalysisRun.objects.filter(
            status=AnalysisRun.Status.SUCCEEDED,
            mode=trend_mode,
            scope=AnalysisRun.Scope.UNIVERSE,
        )
        .exclude(id=analysis.id)
        .order_by("-started_at")
        .values_list("summary", flat=True)[:11]
    )
    pooled = pooled_ic([{"score_validation": scores_check}, *history])
    analysis.summary["score_validation"] = scores_check
    analysis.summary["strategy_validation"] = strategies_check
    analysis.summary["validation_findings"] = findings(
        scores_check,
        strategies_check,
        pooled=pooled,
        max_strategy_correlation=float(strategy_settings.max_strategy_correlation),
    )
    analysis.summary["pooled_ic"] = pooled
    analysis.summary["btc_regime"] = regime_info
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
        # ``coins`` counts only coins with enough base history (200 bars) to build a gate.
        analysis.summary["hybrid_coverage"] = {
            "coins": hybrid_attempted,
            "covered": hybrid_covered,
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
        "max_correlation": float(strategy_settings.max_strategy_correlation),
        "max_price_correlation": cfg["MAX_CORRELATION"],
        "max_top_trade_share": cfg["MAX_TOP_TRADE_SHARE"],
    }
    # Solo runs must not displace the universe ranking/performance reports, and
    # only the run that drives paper sessions publishes the report set.
    if analysis.scope == AnalysisRun.Scope.UNIVERSE and drives_sessions:
        try:
            reports = build_reports(analysis, ranked, config_payload)
            analysis.summary["report_ids"] = [str(report.id) for report in reports]
        except Exception as exc:  # noqa: BLE001
            logger.exception("Report generation failed")
            analysis.summary["report_error"] = str(exc)

    if drives_sessions:
        try:
            _bootstrap_paper_sessions(analysis, ranked, strategy_settings, job_run_id)
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


ACTIVE_MODE = "active"


def latest_analysis(mode: str = ACTIVE_MODE) -> AnalysisRun | None:
    """Latest succeeded universe run of ``mode``, falling back to any run.

    ``ACTIVE_MODE`` resolves to the live trading mode; a falsy mode means any.
    """

    if mode == ACTIVE_MODE:
        from apps.trading.models import LiveTradingState

        mode = LiveTradingState.load().mode
    succeeded = AnalysisRun.objects.filter(status=AnalysisRun.Status.SUCCEEDED)
    if mode:
        succeeded = succeeded.filter(mode=mode)
    return (
        succeeded.filter(scope=AnalysisRun.Scope.UNIVERSE).order_by("-started_at").first()
        or succeeded.order_by("-started_at").first()
    )
