"""BTC market regime protection: readings and the pure evaluator.

BTC is read once per closed 15m bar and classified into one global phase:

    normal   no warning or bearish signal
    warning  guard armed (RSI overbought) or funding crowding
             -> new buys are blocked on every coin
    bearish  guard exit, trailing stop below the window high, open-interest
             flush near the 24h high, or a 1h close below support
             -> every active session is flattened and paused

``bearish`` is left only when every recovery check passes and the minimum
duration has elapsed. The live controller and the backtest timeline both build
``RegimeReadings`` with ``reading_series`` and classify them with
``evaluate_regime``, so the two paths cannot drift apart.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Mapping

import numpy as np
from django.db import transaction
from django.utils import timezone

from apps.common.binance import BinanceError, BinanceFuturesClient
from apps.engine.grid import (
    MIN_FUNDING_HISTORY,
    GridParams,
    _funding_z_series,
    _indicator_series,
    _rolling_extreme,
)
from apps.engine.models import log_decision
from apps.trading import risk
from apps.trading.events import record_event, snapshot_session
from apps.trading.models import (
    GridEvent,
    MarketRegimeState,
    OpenInterestPoint,
    PaperSession,
    StrategySettings,
)

logger = logging.getLogger("engine")

SYMBOL = "BTCUSDT"
REGIME_PAUSE_SOURCE = "btc_regime"
NORMAL = "normal"
WARNING = "warning"
BEARISH = "bearish"

BAR_15M = timedelta(minutes=15)
BAR_1H = timedelta(hours=1)
BARS_15M_PER_DAY = 96
BARS_1H_PER_DAY = 24
# 30-day support plus the 14-day ATR, with a day of slack.
WARMUP_1H_DAYS = 45
OI_READINGS = 6
SUPPORT_BUFFER_ATR = 0.25
FUNDING_LOOKBACK = 90
# Live history. The 1h EMA50-day trend needs far more than its warm-up to
# settle, so live reads 120 days to match the backtest's long history.
LIVE_1H_DAYS = 120
LIVE_FUNDING_DAYS = 32
# Binance serves open-interest history for about the last 30 days, at most
# 500 rows per request.
OI_HISTORY_DAYS = 29
OI_MAX_ROWS = 500
# Thresholds are compared with a small tolerance so a reading exactly on a
# threshold triggers regardless of float rounding.
EPS = 1e-9


@dataclass(frozen=True)
class RegimeConfig:
    trailing_stop_pct: float
    trailing_window_days: int
    oi_flush_pct: float
    oi_near_high_pct: float
    funding_z: float
    min_bearish_hours: float
    risk: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_settings(cls, row: Any) -> "RegimeConfig":
        from django.conf import settings

        return cls(
            trailing_stop_pct=float(row.btc_trailing_stop_pct),
            trailing_window_days=int(row.btc_trailing_window_days),
            oi_flush_pct=float(row.btc_oi_flush_pct),
            oi_near_high_pct=float(row.btc_oi_near_high_pct),
            funding_z=float(row.btc_funding_z),
            min_bearish_hours=float(row.btc_min_bearish_hours),
            risk=dict(settings.RISK),
        )


@dataclass(frozen=True)
class RegimeReadings:
    """BTC state at the close of one 15m bar (``time`` is the close time)."""

    time: datetime
    price: float
    bar_high: float
    rsi: float | None
    rsi_prev: float | None
    macd_hist: tuple[float, ...]
    vwap: float | None
    window_high: float
    high_24h: float
    close_1h: float
    support: float
    atr: float
    momentum: float
    trend_up: bool
    funding_z: float | None
    oi_values: tuple[float, ...]


@dataclass
class RegimeDecision:
    phase: str
    reasons: list[str]
    metrics: dict[str, Any]
    guard_state: dict[str, Any]
    entered_at: datetime | None
    changed: bool


def _rsi_series(closes: np.ndarray, period: int) -> list[float | None]:
    """``risk.rsi`` of every prefix, computed in one pass."""

    out: list[float | None] = [None] * closes.size
    if closes.size < period + 1:
        return out
    deltas = np.diff(closes)
    gains = np.maximum(deltas, 0.0)
    losses = np.maximum(-deltas, 0.0)
    avg_gain = float(np.mean(gains[:period]))
    avg_loss = float(np.mean(losses[:period]))
    for k in range(period, closes.size):
        if k > period:
            avg_gain = (avg_gain * (period - 1) + float(gains[k - 1])) / period
            avg_loss = (avg_loss * (period - 1) + float(losses[k - 1])) / period
        if avg_loss <= 1e-12:
            out[k] = 100.0 if avg_gain > 0 else 50.0
        else:
            out[k] = 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    return out


def _vwap_series(bars: list[dict[str, Any]], close_times: list[datetime]) -> list[float | None]:
    """``risk.anchored_vwap`` anchored at the UTC day of each bar's close."""

    out: list[float | None] = []
    day = None
    total_pv = 0.0
    total_volume = 0.0
    for bar, closed in zip(bars, close_times):
        bar_day = bar["time"].date()
        if bar_day != day:
            day, total_pv, total_volume = bar_day, 0.0, 0.0
        volume = float(bar["volume"])
        if volume > 0:
            total_pv += (bar["high"] + bar["low"] + bar["close"]) / 3.0 * volume
            total_volume += volume
        # A bar closing exactly at midnight belongs to the previous day, so the
        # new day's anchored VWAP has no bars yet.
        if closed.date() != bar_day or total_volume <= 0:
            out.append(None)
        else:
            out.append(total_pv / total_volume)
    return out


def _seconds(values: list[datetime]) -> np.ndarray:
    return np.array([value.timestamp() for value in values], dtype=float)


def reading_series(
    bars_15m: list[dict[str, Any]],
    bars_1h: list[dict[str, Any]],
    funding: list[dict[str, Any]],
    oi_points: list[dict[str, Any]],
    config: RegimeConfig,
) -> list[RegimeReadings]:
    """One reading per closed 15m bar with full warm-up, oldest first.

    Every reading uses only data at or before its bar's close. All series are
    causal, so the readings for a prefix of the inputs equal the leading
    readings for the full inputs.
    """

    window_bars = max(1, int(config.trailing_window_days)) * BARS_15M_PER_DAY
    warmup_1h = WARMUP_1H_DAYS * BARS_1H_PER_DAY
    if len(bars_15m) < window_bars or len(bars_1h) < warmup_1h:
        return []

    cfg = config.risk
    rsi_period = int(cfg["RSI_PERIOD"])
    closes = np.array([bar["close"] for bar in bars_15m], dtype=float)
    highs = np.array([bar["high"] for bar in bars_15m], dtype=float)
    close_times = [bar["time"] + BAR_15M for bar in bars_15m]
    rsi_values = _rsi_series(closes, rsi_period)
    macd_line = risk._ema(closes, int(cfg["MACD_FAST"])) - risk._ema(closes, int(cfg["MACD_SLOW"]))
    hist = macd_line - risk._ema(macd_line, int(cfg["MACD_SIGNAL"]))
    macd_min_bars = int(cfg["MACD_SLOW"]) + int(cfg["MACD_SIGNAL"])
    vwaps = _vwap_series(bars_15m, close_times)
    window_high = _rolling_extreme(highs, window_bars, True)
    high_24h = _rolling_extreme(highs, BARS_15M_PER_DAY, True)

    h_close = np.array([bar["close"] for bar in bars_1h], dtype=float)
    hourly = _indicator_series(
        np.array([bar["high"] for bar in bars_1h], dtype=float),
        np.array([bar["low"] for bar in bars_1h], dtype=float),
        h_close,
        GridParams(),
        BARS_1H_PER_DAY,
    )
    h_closed_at = _seconds([bar["time"] + BAR_1H for bar in bars_1h])

    ordered_funding = sorted(funding, key=lambda row: row["time"])
    funding_at = _seconds([row["time"] for row in ordered_funding])
    funding_z = _funding_z_series(
        np.array([float(row["rate"]) for row in ordered_funding], dtype=float), FUNDING_LOOKBACK
    )

    ordered_oi = sorted(oi_points, key=lambda row: row["time"])
    oi_at = _seconds([row["time"] for row in ordered_oi])
    oi_values = [float(row["open_interest"]) for row in ordered_oi]
    oi_span = OI_READINGS * BAR_1H.total_seconds()

    readings: list[RegimeReadings] = []
    for i in range(window_bars - 1, len(bars_15m)):
        moment = close_times[i]
        at = moment.timestamp()
        h = int(np.searchsorted(h_closed_at, at, side="right")) - 1
        if h < warmup_1h - 1:
            continue
        f = int(np.searchsorted(funding_at, at, side="right"))
        # Only OI readings inside the lookback window count, so a gap in the
        # stored history reads as unavailable rather than stale.
        o_end = int(np.searchsorted(oi_at, at, side="right"))
        o_start = int(np.searchsorted(oi_at, at - oi_span, side="right"))
        recent_oi = tuple(oi_values[o_start:o_end][-OI_READINGS:])
        readings.append(
            RegimeReadings(
                time=moment,
                price=float(closes[i]),
                bar_high=float(highs[i]),
                rsi=rsi_values[i],
                rsi_prev=rsi_values[i - 1] if i > rsi_period else None,
                macd_hist=(
                    tuple(float(x) for x in hist[max(0, i - 3) : i + 1])
                    if i + 1 >= macd_min_bars
                    else ()
                ),
                vwap=vwaps[i],
                window_high=float(window_high[i]),
                high_24h=float(high_24h[i]),
                close_1h=float(h_close[h]),
                support=float(hourly["support_prior"][h]),
                atr=float(hourly["atr"][h]),
                momentum=float(hourly["momentum"][h]),
                trend_up=bool(hourly["trend_up"][h]),
                funding_z=float(funding_z[f - 1]) if f > MIN_FUNDING_HISTORY else None,
                oi_values=recent_oi if len(recent_oi) >= 2 else (),
            )
        )
    return readings


def _snapshot(reading: RegimeReadings) -> risk.RiskSnapshot:
    return risk.RiskSnapshot(
        price=reading.price,
        bar_time=reading.time - BAR_15M,
        bar_high=reading.bar_high,
        close=reading.price,
        rsi=reading.rsi,
        rsi_prev=reading.rsi_prev,
        macd_hist=list(reading.macd_hist),
        vwap=reading.vwap,
        oi_values=list(reading.oi_values),
        oi_available=bool(reading.oi_values),
    )


def _round(value: float | None, digits: int = 6) -> float | None:
    return round(value, digits) if value is not None else None


def evaluate_regime(
    phase: str,
    entered_at: datetime | None,
    guard_state: Mapping[str, Any] | None,
    reading: RegimeReadings,
    config: RegimeConfig,
) -> RegimeDecision:
    """Advance the BTC regime by one reading. Pure: no I/O."""

    previous = phase if phase in (NORMAL, WARNING, BEARISH) else NORMAL
    snapshot = _snapshot(reading)
    guard = risk.evaluate(guard_state or {}, snapshot, config.risk, reading.time)

    support_line = reading.support - SUPPORT_BUFFER_ATR * reading.atr
    drawdown = 1.0 - reading.price / reading.window_high if reading.window_high > 0 else 0.0
    oi = reading.oi_values
    oi_change = oi[-1] / oi[0] - 1.0 if len(oi) >= 2 and oi[0] > 0 else None

    trailing = drawdown >= config.trailing_stop_pct - EPS
    oi_flush = (
        oi_change is not None
        and oi_change <= -config.oi_flush_pct + EPS
        and reading.price >= reading.high_24h * (1.0 - config.oi_near_high_pct) - EPS
    )
    breakdown = reading.support > 0 and reading.close_1h < support_line

    bearish: list[str] = []
    if guard.action == "pause":
        bearish.append("guard_exit")
    if trailing:
        bearish.append("trailing_stop")
    if oi_flush:
        bearish.append("oi_flush")
    if breakdown:
        bearish.append("support_breakdown")

    warning: list[str] = []
    if guard.state.get("phase") == "armed":
        warning.append("guard_armed")
    if (
        reading.funding_z is not None
        and reading.funding_z >= config.funding_z - EPS
        and (reading.momentum < 0 or not reading.trend_up)
    ):
        warning.append("funding_crowded")

    metrics: dict[str, Any] = {
        "time": reading.time.isoformat(),
        "price": round(reading.price, 10),
        "window_high": round(reading.window_high, 10),
        "drawdown_pct": round(drawdown, 6),
        "high_24h": round(reading.high_24h, 10),
        "rsi_15m": _round(reading.rsi, 2),
        "vwap_15m": _round(reading.vwap, 10),
        "close_1h": round(reading.close_1h, 10),
        "support": round(reading.support, 10),
        "support_line": round(support_line, 10),
        "atr": round(reading.atr, 10),
        "momentum": round(reading.momentum, 6),
        "trend_up": bool(reading.trend_up),
        "funding_z": _round(reading.funding_z, 4),
        "oi_change_pct": _round(oi_change),
        "oi_available": bool(oi),
        "guard_phase": guard.state.get("phase") or "idle",
    }

    if previous == BEARISH:
        elapsed_ok = entered_at is None or reading.time - entered_at >= timedelta(
            hours=config.min_bearish_hours
        )
        checks = {
            name: bool(value)
            for name, value in {
                **risk.recovery_checks(snapshot, entered_at, config.risk, reading.time),
                "above_support": not breakdown,
                "below_trailing_stop": not trailing,
                "no_oi_flush": not oi_flush,
                "min_duration": elapsed_ok,
            }.items()
        }
        metrics["recovery"] = checks
        if not (all(checks.values()) and not bearish):
            new_phase = BEARISH
            reasons = bearish or ["recovery_pending"]
        else:
            new_phase = WARNING if warning else NORMAL
            reasons = warning
    elif bearish:
        new_phase, reasons = BEARISH, bearish
    elif warning:
        new_phase, reasons = WARNING, warning
    else:
        new_phase, reasons = NORMAL, []

    changed = new_phase != previous
    return RegimeDecision(
        phase=new_phase,
        reasons=list(reasons),
        metrics=metrics,
        guard_state=dict(guard.state),
        entered_at=reading.time if changed else entered_at,
        changed=changed,
    )


# ---------------------------------------------------------------- live


class RegimeDataError(RuntimeError):
    pass


def _closed(rows: list[dict[str, Any]], length: timedelta, now: datetime) -> list[dict[str, Any]]:
    return sorted((row for row in rows if row["time"] + length <= now), key=lambda r: r["time"])


def fetch_live_inputs(
    client: BinanceFuturesClient, now: datetime, config: RegimeConfig | None = None
) -> tuple[list, list, list, list]:
    """Closed BTC 15m and 1h bars, funding prints, and the latest hourly OI.

    Raises ``BinanceError`` when candles or funding cannot be fetched. Open
    interest failing only makes it unavailable, as in the coin guard.
    """

    window_days = int(config.trailing_window_days) if config else 7
    days_15m = max(8, window_days + 1)
    bars_15m = _closed(
        client.klines_range(SYMBOL, "15m", now - timedelta(days=days_15m), now),
        BAR_15M,
        now,
    )
    bars_1h = _closed(
        client.klines_range(SYMBOL, "1h", now - timedelta(days=LIVE_1H_DAYS), now),
        BAR_1H,
        now,
    )
    funding = client.funding_rates(
        SYMBOL, now - timedelta(days=LIVE_FUNDING_DAYS), now, max_requests=6
    )
    if not isinstance(funding, list):
        raise BinanceError("Unexpected funding response")
    try:
        oi_points = [
            row
            for row in client.open_interest_hist(SYMBOL, "1h", limit=OI_READINGS)
            if row["time"] <= now
        ]
    except (BinanceError, KeyError, TypeError, ValueError) as exc:
        logger.warning("BTC open interest unavailable: %s", exc)
        oi_points = []
    return bars_15m, bars_1h, funding, oi_points


def record_open_interest(client: Any = None, symbol: str = SYMBOL) -> int:
    """Store hourly open interest since the last stored point; returns rows added.

    The stored history lets the backtest replay the OI flush signal. The first
    run back-fills what Binance serves; later runs continue from the last row.
    """

    client = client if client is not None else BinanceFuturesClient()
    now = timezone.now()
    last = (
        OpenInterestPoint.objects.filter(symbol=symbol)
        .order_by("-time")
        .values_list("time", flat=True)
        .first()
    )
    start_ms = None
    if last is not None:
        start = max(last + BAR_1H, now - timedelta(days=OI_HISTORY_DAYS))
        start_ms = int(start.timestamp() * 1000)
    rows = client.open_interest_hist(symbol, "1h", limit=OI_MAX_ROWS, start_ms=start_ms)
    points = [
        OpenInterestPoint(symbol=symbol, time=row["time"], open_interest=float(row["open_interest"]))
        for row in rows
        if row["time"] <= now
    ]
    before = OpenInterestPoint.objects.filter(symbol=symbol).count()
    OpenInterestPoint.objects.bulk_create(points, ignore_conflicts=True)
    return OpenInterestPoint.objects.filter(symbol=symbol).count() - before


EFFECTS = {
    NORMAL: "grid trading unrestricted",
    WARNING: "new buys blocked on every coin",
    BEARISH: "every active session flattened to USDT and paused",
}


def _record_transition(
    old: str, new: str, reasons: list[str], metrics: dict[str, Any], now: datetime
) -> None:
    record_event(
        GridEvent.Type.REGIME,
        symbol=SYMBOL,
        reason=",".join(reasons),
        before={"phase": old},
        after={"phase": new, "reasons": reasons, "metrics": metrics},
        time=now,
    )
    because = f" ({', '.join(reasons)})" if reasons else ""
    log_decision(
        "risk",
        f"BTC market regime {old} -> {new}{because}: {EFFECTS[new]}.",
        severity="warning" if new in (WARNING, BEARISH) else "info",
        payload={"before": old, "after": new, "reasons": reasons, "metrics": metrics},
    )


def apply_bearish(now: datetime, client: BinanceFuturesClient | None = None) -> list[str]:
    """Flatten and pause every active, unlocked session; returns symbols.

    Idempotent: runs every tick while bearish and only touches sessions that
    are still active. A session that fails is left untouched for the next tick.
    """

    from apps.trading import paper

    flattened: list[str] = []
    candidates = (
        PaperSession.objects.filter(status=PaperSession.Status.ACTIVE)
        .exclude(bias=PaperSession.Bias.BEARISH)
        .values_list("id", flat=True)
    )
    for session_id in list(candidates):
        try:
            with transaction.atomic():
                session = (
                    PaperSession.objects.select_for_update()
                    .select_related("coin")
                    .get(id=session_id)
                )
                if (
                    session.status != PaperSession.Status.ACTIVE
                    or session.bias == PaperSession.Bias.BEARISH
                ):
                    continue
                position_qty = session.position_qty
                price = paper.flatten_session(session, now, REGIME_PAUSE_SOURCE, client)
                session.status = PaperSession.Status.PAUSED
                session.risk_state = {
                    **(session.risk_state or {}),
                    "pause_source": REGIME_PAUSE_SOURCE,
                }
                session.save()
            record_event(GridEvent.Type.PAUSE, session=session, reason=REGIME_PAUSE_SOURCE)
            snapshot_session(session, "pause")
            log_decision(
                "risk",
                f"{session.coin.symbol} paused by the BTC market regime; "
                "inventory sold to USDT until BTC recovers.",
                severity="warning",
                coin=session.coin,
                payload={
                    "session_id": str(session.id),
                    "price": round(price, 10),
                    "position_qty": round(position_qty, 10),
                    "equity": round(session.equity, 4),
                },
            )
            flattened.append(session.coin.symbol)
        except Exception:  # noqa: BLE001 - one session must not block the rest
            logger.exception("BTC regime flatten failed for session %s", session_id)
    return flattened


def release_regime_pauses(now: datetime) -> list[str]:
    """Resume every session the regime paused; returns symbols."""

    from apps.trading import paper

    resumed: list[str] = []
    paused = PaperSession.objects.filter(status=PaperSession.Status.PAUSED).select_related("coin")
    for session in paused:
        state = dict(session.risk_state or {})
        if state.get("pause_source") != REGIME_PAUSE_SOURCE:
            continue
        state.pop("pause_source")
        session.risk_state = state
        session.save(update_fields=["risk_state", "updated_at"])
        # Paused by its own coin guard as well: that guard's recovery decides.
        if state.get("phase") == "paused":
            continue
        paper.resume_session(session, reason=REGIME_PAUSE_SOURCE)
        resumed.append(session.coin.symbol)
    return resumed


def recovery_status(state: MarketRegimeState) -> list[dict[str, Any]] | None:
    """The recovery checklist from the last evaluation; None unless bearish."""

    if state.phase != BEARISH:
        return None
    checks = (state.metrics or {}).get("recovery") or {}
    guard = all(
        bool(checks.get(name)) for name in ("cooled", "rsi", "vwap", "open_interest")
    )
    return [
        {"check": "guard_recovery", "passed": guard},
        {"check": "above_support", "passed": bool(checks.get("above_support"))},
        {"check": "trailing_clear", "passed": bool(checks.get("below_trailing_stop"))},
        {"check": "no_oi_flush", "passed": bool(checks.get("no_oi_flush"))},
        {"check": "min_duration", "passed": bool(checks.get("min_duration"))},
    ]


def thresholds(row: StrategySettings) -> dict[str, Any]:
    from django.conf import settings

    return {
        "trailing_stop_pct": float(row.btc_trailing_stop_pct),
        "trailing_window_days": int(row.btc_trailing_window_days),
        "oi_flush_pct": float(row.btc_oi_flush_pct),
        "oi_near_high_pct": float(row.btc_oi_near_high_pct),
        "funding_z": float(row.btc_funding_z),
        "min_bearish_hours": float(row.btc_min_bearish_hours),
        "rsi_overbought": float(settings.RISK["RSI_OVERBOUGHT"]),
        "rsi_recovery": float(settings.RISK["RSI_RECOVERY"]),
        "guard_trailing_pct": float(settings.RISK["TRAILING_STOP_PCT"]),
        "support_buffer_atr": SUPPORT_BUFFER_ATR,
    }


def _apply_effects(state: MarketRegimeState, now: datetime, client: Any) -> None:
    if state.phase == BEARISH:
        apply_bearish(now, client)
    else:
        release_regime_pauses(now)


def refresh_regime(now: datetime, client: Any = None) -> MarketRegimeState:
    """Evaluate BTC on its latest closed 15m bar and apply the phase."""

    row = StrategySettings.load()
    MarketRegimeState.load()
    if not row.btc_regime_enabled:
        with transaction.atomic():
            state = MarketRegimeState.objects.select_for_update().get(pk=1)
            old = state.phase
            state.last_evaluated_at = now
            state.last_error = ""
            if old != NORMAL:
                state.phase = NORMAL
                state.entered_at = now
                state.reasons = ["disabled"]
                _record_transition(old, NORMAL, ["disabled"], dict(state.metrics or {}), now)
            state.save()
        release_regime_pauses(now)
        return state

    client = client if client is not None else BinanceFuturesClient()
    config = RegimeConfig.from_settings(row)
    try:
        readings = reading_series(*fetch_live_inputs(client, now, config), config)
        if not readings:
            raise RegimeDataError("BTC history too short for the regime warm-up")
    except (BinanceError, RegimeDataError) as exc:
        state = MarketRegimeState.load()
        state.last_error = str(exc)[:255]
        state.last_evaluated_at = now
        state.save(update_fields=["last_error", "last_evaluated_at", "updated_at"])
        log_decision(
            "risk",
            f"BTC market regime not evaluated ({exc}); phase stays {state.phase}.",
            severity="warning",
            payload={"phase": state.phase, "error": state.last_error},
        )
        _apply_effects(state, now, client)
        return state

    reading = readings[-1]
    with transaction.atomic():
        state = MarketRegimeState.objects.select_for_update().get(pk=1)
        state.last_evaluated_at = now
        state.last_error = ""
        # Each closed 15m bar is evaluated once, exactly as the backtest does;
        # later ticks inside the same bar only re-apply the stored phase.
        if (state.metrics or {}).get("time") != reading.time.isoformat():
            old = state.phase
            decision = evaluate_regime(
                old, state.entered_at, state.guard_state, reading, config
            )
            state.phase = decision.phase
            state.reasons = decision.reasons
            state.metrics = decision.metrics
            state.guard_state = decision.guard_state
            state.entered_at = decision.entered_at
            if decision.changed:
                _record_transition(old, decision.phase, decision.reasons, decision.metrics, now)
        state.save()
    _apply_effects(state, now, client)
    return state
