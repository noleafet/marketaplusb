"""BTC market regime replayed over history for the backtest.

The timeline runs the same ``reading_series`` and ``evaluate_regime`` the live
controller uses, once per closed BTC 15m bar, starting from ``normal`` with an
idle BTC guard. Only the phase changes are kept; ``gate_from_timeline`` maps
them onto a coin's bars as a simulator ``Gate``:

* ``allow`` is False while the phase is ``warning`` or ``bearish``;
* ``derisk`` is set on the same bars, so inventory is offered on the nearest
  sell levels instead of the full ladder. Nothing is sold at market.

A coin bar sees only BTC bars closed at or before its open, and bars before
the first reading get no entry, so they trade ungated.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from apps.engine.hybrid_gate import Gate
from apps.trading.regime import (
    BEARISH,
    NORMAL,
    WARNING,
    RegimeConfig,
    evaluate_regime,
    reading_series,
)

@dataclass
class RegimeTimeline:
    """Phase changes keyed by BTC 15m close time; the first entry is the
    phase at the first reading."""

    changes: list[tuple[datetime, str]] = field(default_factory=list)
    readings: int = 0
    oi_coverage: float = 0.0
    warning_readings: int = 0
    bearish_readings: int = 0
    bearish_entries: int = 0

    def stats(self) -> dict[str, Any]:
        total = max(1, self.readings)
        return {
            "readings": self.readings,
            "start": self.changes[0][0].isoformat() if self.changes else None,
            "changes": len(self.changes),
            "warning_share": round(self.warning_readings / total, 4),
            "bearish_share": round(self.bearish_readings / total, 4),
            "bearish_entries": self.bearish_entries,
            "oi_coverage": round(self.oi_coverage, 4),
        }


def build_regime_timeline(
    bars_15m: list[dict[str, Any]],
    bars_1h: list[dict[str, Any]],
    funding: list[dict[str, Any]],
    oi_points: list[dict[str, Any]],
    config: RegimeConfig,
) -> RegimeTimeline:
    readings = reading_series(bars_15m, bars_1h, funding, oi_points, config)
    timeline = RegimeTimeline(readings=len(readings))
    phase, entered_at, guard_state = NORMAL, None, {}
    with_oi = 0
    for reading in readings:
        decision = evaluate_regime(phase, entered_at, guard_state, reading, config)
        if not timeline.changes or decision.changed:
            timeline.changes.append((reading.time, decision.phase))
            if decision.phase == BEARISH:
                timeline.bearish_entries += 1
        if decision.phase == WARNING:
            timeline.warning_readings += 1
        elif decision.phase == BEARISH:
            timeline.bearish_readings += 1
        with_oi += 1 if reading.oi_values else 0
        phase, entered_at, guard_state = (
            decision.phase,
            decision.entered_at,
            decision.guard_state,
        )
    timeline.oi_coverage = with_oi / len(readings) if readings else 0.0
    return timeline


def gate_from_timeline(rows: list[dict[str, Any]], timeline: RegimeTimeline | None) -> Gate | None:
    if timeline is None or not timeline.changes:
        return None
    times = [moment for moment, _ in timeline.changes]
    allow: dict[datetime, bool] = {}
    derisk: dict[datetime, bool] = {}
    allowed = blocked = 0
    for row in rows:
        opened = row["time"]
        index = bisect_right(times, opened) - 1
        if index < 0:
            continue
        phase = timeline.changes[index][1]
        allow[opened] = phase == NORMAL
        if phase != NORMAL:
            derisk[opened] = True
        if phase == NORMAL:
            allowed += 1
        else:
            blocked += 1
    if not allow:
        return None
    return Gate(
        allow=allow, exit={}, allowed_bars=allowed, blocked_bars=blocked, derisk=derisk
    )
