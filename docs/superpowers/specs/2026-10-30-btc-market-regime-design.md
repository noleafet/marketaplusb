# BTC Market Regime Protection — Design

Date: 2026-10-30
Status: Approved design, pending spec review

## 1. Intent

The grid bot is long-only, and crypto sell-offs are usually led by BTC. Today
every protection is judged per coin, so a BTC-led sell-off is seen as many
separate events and each grid keeps buying its first leg down until its own
gate or stop fires.

BTC Market Regime Protection monitors BTC continuously and, when BTC turns
bearish, protects **every** paper session at once.

Success criteria:

- One BTC evaluation per paper tick drives one global phase that every coin
  obeys in the same tick.
- A warning blocks new buys everywhere; a confirmed bearish regime sells all
  inventory to USDT and pauses every active session; trading resumes
  automatically only after BTC passes explicit recovery checks.
- It applies identically in normal and hybrid trading mode.
- The backtest models the same regime (except open-interest signals for
  history that has no stored open interest), so paper and backtest stay
  comparable, and every analysis reports what the regime gained or cost.
- Every phase change, flatten, and resume is recorded and visible to the ETL
  API.

### Out of scope

- Live trading (still not wired; the regime state is global, so a future live
  runner reads the same flag).
- The EMA100 trend gate on BTC (explicitly excluded).
- Shorting or hedging during a bearish regime.

## 2. Current state (relevant facts)

- `apps/trading/risk.py` holds the per-coin overbought guard: a state machine
  (`idle → armed → paused → idle`) fed by `fetch_snapshot` (closed 15m candles,
  RSI, MACD histogram, anchored VWAP, 1h open interest).
- `apps/trading/paper.py::tick_paper_sessions` runs every 5 minutes, refreshes
  book state (`portfolio.refresh_book_state`), and passes a buy-block reason to
  each session's `strategy.portfolio_blocked`.
- `apps/engine/grid.py::simulate_grid` accepts a `Gate` (`allow` and `exit`
  maps keyed by bar open time, from `apps/engine/hybrid_gate.py`). `allow=False`
  disarms buys; `exit=True` flattens at the bar open.
- `GridEvent` records pause/resume/kill-switch events and is served by the ETL
  API. `StrategySettings` fields are part of the strategy-version snapshot.
- Binance's public open-interest history reaches back about 30 days; the
  backtest window is 520 days.

## 3. Signals and phases

All BTC inputs use **closed** candles. "Price" means the last closed 15m close.

| Signal | Rule (defaults) | Tier |
|---|---|---|
| Guard armed | BTC 15m RSI > 75 (guard enters `armed`) | warning |
| Funding crowding | BTC funding z-score ≥ 2.0 over the previous 90 prints, with BTC 10-day momentum < 0 or the EMA20/50 trend not up | warning |
| Guard exit | Existing guard exit while armed: 6% below the armed peak, MACD histogram shrinking 2 bars with close below VWAP, or OI flush near the peak | bearish |
| Trailing stop | Price ≥ 8% below the highest 15m high of the last 7 days | bearish |
| OI flush | 1h open interest down ≥ 5% over the last 6 readings while price is within 3% of the 24-hour high | bearish |
| Support breakdown | Last closed 1h close below the 30-day support measured before that bar, minus 0.25 × ATR | bearish |

Support, ATR, momentum, trend and funding z-score reuse the grid's existing
functions (`apps/engine/grid.py`) so BTC and coins are measured identically.
ATR is the 4h-equivalent ATR already used by the grid.

Phases:

| Phase | Entered when | Effect |
|---|---|---|
| `normal` | no warning or bearish signal | none |
| `warning` | any warning signal, no bearish signal | new buys blocked on every coin |
| `bearish` | any bearish signal (from `normal` or `warning`) | every active session flattened to USDT and paused |

Transitions:

- `normal ↔ warning`: entered as soon as a warning signal holds, left as soon
  as none does.
- `→ bearish`: immediately on any bearish signal.
- `bearish → normal | warning`: only when **all** recovery checks pass:
  1. the guard's recovery checks pass (RSI > 35 and rising, 15m close above
     VWAP, open interest turning up — or 60 minutes since entering `bearish`
     when OI is unavailable, as the guard already does);
  2. price is back above support minus the buffer;
  3. drawdown from the 7-day high is below the trailing-stop threshold;
  4. no OI flush in the latest reading;
  5. at least 4 hours since entering `bearish`.
  The new phase is `warning` if a warning signal still holds, else `normal`.
- If BTC data cannot be fetched or is too short, the phase **does not change**;
  `last_error` and a warning decision log record it.

## 4. Components

### 4.1 Pure evaluator — `apps/trading/regime.py`

- `RegimeInputs` (dataclass): the BTC readings for one evaluation time — closed
  15m bars (≥ 7 days), closed 1h bars (≥ 30 days + ATR warm-up), funding
  prints, OI readings (possibly empty), and `now`.
- `evaluate_regime(state, inputs, config) -> RegimeDecision`: pure function
  returning the new phase, active signals, metrics, and the updated BTC guard
  state. It calls `risk.evaluate` for the guard part with a snapshot built from
  the inputs. No I/O, so the live path and the backtest call the same code.
- `RegimeConfig`: built from `StrategySettings` plus `settings.RISK` (guard
  thresholds).

### 4.2 Live controller — `apps/trading/regime.py`

- `refresh_regime(now) -> MarketRegimeState`: fetches BTC inputs (15m klines,
  1h klines, funding, OI), calls `evaluate_regime`, saves the state under a row
  lock, and on a phase change records the transition and applies effects.
- Called at the start of `tick_paper_sessions`, before any session, including
  when no session is active (so recovery is noticed while all are paused).
- Effects:
  - `warning`: `portfolio.buy_block_reason` returns `btc_regime_warning`
    (alongside daily-loss halt and exposure cap); sessions re-arm buys through
    the existing re-arm logic when it clears.
  - entering `bearish`: each **active** session is flattened at the current
    price through the same path the per-coin guard uses, set to `paused`, and
    tagged `risk_state.pause_source = "btc_regime"`. A failed flatten leaves
    that session untagged; it is retried on the next tick while still bearish.
  - leaving `bearish`: only sessions tagged `btc_regime` resume (grid rebuilt
    around the current price, as the existing resume does). If a session's own
    overbought guard had also paused it, its own recovery checks must pass too.
- Disabling the regime in settings resumes sessions it paused on the next tick
  and sets the phase to `normal`.

### 4.3 Interactions

- Manual pauses and bearish-locked sessions are never changed by the regime.
- The per-coin overbought guard keeps running; the layers stack.
- Normal and hybrid mode behave identically; in hybrid mode the per-coin EMA100
  gate still applies on top.
- Daily analysis: while `bearish`, it ranks and reports but opens no new paper
  sessions (logged). In `warning`, sessions open normally with buys blocked.

## 5. Backtesting the regime

### 5.1 BTC data

- BTC 15m and 1h candles are synced into `market_candles` by the analysis
  (incremental, like other candles) for the full analysis window.
- A new hourly job `btc_open_interest` stores BTC 1h open interest in a new
  model `OpenInterestPoint(symbol, time, open_interest)` (unique on symbol and
  time). Its first run back-fills what Binance provides (about 30 days).

### 5.2 Regime timeline

- `build_regime_timeline(btc_15m, btc_1h, funding, oi_points, config)` in
  `apps/engine/regime_timeline.py` replays `evaluate_regime` at every closed 15m
  BTC bar. OI readings are used only where stored history covers the lookback;
  elsewhere OI is unavailable, exactly as in live when the OI request fails.
- It returns a `Gate` keyed by each coin bar's open time: `allow=False` while
  the latest phase at that time is `warning` or `bearish`; `exit=True` on the
  first coin bar after a transition into `bearish`. Mapping is causal: a coin
  bar sees only BTC bars closed at or before its open.
- Built **once per analysis run** and reused for every coin and for the hybrid
  pass. When the hybrid gate is also active the two are combined (buys allowed
  only if both allow; exit if either exits).
- Coverage statistics (share of bars with OI data, bars blocked, exits) are
  stored in the analysis summary.

### 5.3 Results and diagnostics

- Every simulation (walk-forward training and test, holdout, cost stress,
  sensitivity) uses the regime gate when the regime is enabled.
- Each coin also gets a **without-regime** run on its test folds and holdout,
  reported as `regime_off_return` and `regime_effect` (return and max drawdown
  difference), like the existing alternate-fill diagnostic.
- Run-level validation adds the mean regime effect and a strength/weakness
  finding stating the mean drawdown reduction and the mean return cost (for
  example "BTC regime cut max drawdown by 4.1 points at a cost of 1.2 points
  of return").

### 5.4 Parity

A test feeds the same BTC candles, funding and OI to the live evaluator
(step by step) and to `build_regime_timeline`, and asserts identical phase
changes at identical times.

## 6. Storage and configuration

- `MarketRegimeState` (singleton): `phase`, `entered_at`, `reasons` (list),
  `guard_state` (BTC guard state machine), `metrics` (latest readings),
  `last_evaluated_at`, `last_error`, `updated_at`.
- `OpenInterestPoint` as in 5.1.
- New `StrategySettings` fields (validated like the others, part of the
  strategy-version snapshot):

| Field | Default | Range |
|---|---|---|
| `btc_regime_enabled` | `true` | — |
| `btc_trailing_stop_pct` | 0.08 | 0.02–0.30 |
| `btc_trailing_window_days` | 7 | 1–30 |
| `btc_oi_flush_pct` | 0.05 | 0.01–0.30 |
| `btc_oi_near_high_pct` | 0.03 | 0.005–0.10 |
| `btc_funding_z` | 2.0 | 0.5–5.0 |
| `btc_min_bearish_hours` | 4 | 0–72 |

## 7. Audit trail and API

- `GridEvent` gains type `regime` (symbol `BTCUSDT`, before/after phase,
  reasons, metrics), written on every phase change, plus a decision-log entry.
- Each flattened or resumed session gets a `pause` / `resume` event with reason
  `btc_regime`.
- `GET /api/strategy/regime/`: current phase, reasons, metrics, recovery
  checklist (while bearish), last 20 transitions. Read-only.
- The ETL API serves the new `regime` events through its existing events
  endpoint.

## 8. User interface

- Admin → Strategies: a "BTC market regime" card next to the trading-mode card
  showing the phase badge (green/amber/red), what triggered it and when, each
  BTC reading against its threshold, the recovery checklist while bearish, and
  recent transitions.
- The settings card gains the regime switch and thresholds.
- Dashboard overview and Paper sessions show a banner while the phase is not
  `normal`.

## 9. Error handling

- BTC fetch failure or short data: phase unchanged, `last_error` set, warning
  logged.
- Regime evaluation runs inside the paper tick, which cannot be queued twice,
  and the state row is locked while it updates, so it never runs concurrently.
- The regime can only block buys or flatten; it never opens a position.

## 10. Testing

- Unit (synthetic BTC data, no network): each signal at and around its
  threshold; every transition including `normal → bearish` and
  `warning → normal`; recovery needs every check and the minimum duration;
  missing data keeps the phase.
- Integration through `tick_paper_sessions`: warning blocks buys on all coins
  without selling; bearish flattens and tags all active sessions and resumes
  only those; manual, coin-guard and bearish-locked sessions are untouched;
  disabling releases its pauses; analysis opens no sessions while bearish.
- Backtest: timeline mapping is causal; exit on the first bar after entering
  bearish; combination with the hybrid gate; the parity test in 5.4.
- API and settings validation; browser check of the card and banners in each
  phase by forcing the stored state.

## 11. Known limitations

- Open-interest signals are only backtested where stored OI history exists
  (initially about 30 days, growing hourly).
- The regime acts on closed 15m candles, so its fastest reaction is one 15m
  close plus up to one 5-minute tick.
- The analysis fetches roughly 50,000 BTC 15m candles on first sync and adds
  one extra simulation per test fold and holdout per coin.
