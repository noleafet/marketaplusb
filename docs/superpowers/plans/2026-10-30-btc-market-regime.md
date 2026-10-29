# BTC Market Regime Protection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Monitor BTC every paper tick and, when BTC turns bearish, block buys on (warning) or flatten and pause (bearish) every paper session, with automatic recovery, the same regime modelled in the backtest, and full audit/UI visibility.

**Architecture:** One pure evaluator (`apps/trading/regime.py::evaluate_regime`) consumes per-15m-close `RegimeReadings`; the live controller computes the latest readings from Binance and applies effects at the start of `tick_paper_sessions`, while the backtest computes the full readings series from stored BTC candles and maps the phase timeline onto each coin's bars through the existing simulator `Gate`. Because live and backtest call the same readings and evaluator code, parity holds by construction and is asserted by a test.

**Tech Stack:** Django 5.2, DRF, PostgreSQL, numpy; Next.js 16, React 19, TanStack Query/Form, Zod.

**Spec:** `docs/superpowers/specs/2026-10-30-btc-market-regime-design.md`

## Global Constraints

- Phases are exactly `normal`, `warning`, `bearish`.
- Defaults: `btc_regime_enabled=True`, `btc_trailing_stop_pct=0.08`, `btc_trailing_window_days=7`, `btc_oi_flush_pct=0.05`, `btc_oi_near_high_pct=0.03`, `btc_funding_z=2.0`, `btc_min_bearish_hours=4`.
- Ranges: trailing 0.02–0.30, window 1–30 days, OI flush 0.01–0.30, near-high 0.005–0.10, funding z 0.5–5.0, min bearish 0–72 hours.
- OI flush uses the last 6 hourly OI readings and the 24-hour high; support uses 30-day support measured before the bar minus 0.25 × ATR (4h-equivalent); funding z uses the previous 90 prints with 10-day momentum < 0 or EMA20/50 trend not up.
- Guard thresholds come from `settings.RISK` unchanged (RSI 75/35, 6% trailing, MACD shrink 2, OI drop 5%, near-high 3%, 60-minute OI failsafe).
- All BTC inputs use closed candles; "price" is the last closed 15m close.
- Missing or short BTC data never changes the phase.
- The regime only blocks buys or flattens; it never opens positions. Live trading is not touched.
- Applies identically in normal and hybrid mode. No EMA100 trend gate on BTC.
- Symbol is `BTCUSDT`.
- Backend tests: run from `backend/` with `.venv/Scripts/python.exe manage.py test <label> --noinput`.
- Frontend: `npx tsc --noEmit` and `npx oxlint <files>` from `frontend/`; follow `frontend/CLAUDE.md` (TanStack Form + Zod for forms, icons from `@/components/icons`, single quotes, no trailing comma).

## Review Focus

1. A session paused manually or by its own coin guard before BTC turns bearish must stay paused after BTC recovers (only `pause_source == "btc_regime"` sessions resume) — test in Task 3.
2. BTC recovers while the worker was down for hours: the first tick after restart must evaluate on fresh data and resume correctly, not replay — test in Task 3 (`refresh_regime` with `entered_at` 10 hours ago and recovered readings resumes in one call).
3. Turning the regime off while `bearish` must release its paused sessions and set `normal` on the next tick — test in Task 3.
4. A new paper session created during `warning` must not buy until the regime clears — test in Task 4.
5. Backtest coin bars before BTC history coverage must trade ungated, not blocked — test in Task 6.

---

### Task 1: Models, settings fields, and event type

**Files:**
- Modify: `backend/apps/trading/models.py` (StrategySettings fields; new `MarketRegimeState`, `OpenInterestPoint`; `GridEvent.Type.REGIME`)
- Create: `backend/apps/trading/migrations/0010_btc_market_regime.py` (generated)
- Modify: `backend/apps/trading/strategy_views.py` (serializer fields + validators)
- Modify: `backend/apps/engine/versioning.py` (`SNAPSHOT_FIELDS`)
- Test: `backend/apps/trading/tests_regime.py` (new)

**Interfaces:**
- Produces:
  - `StrategySettings.btc_regime_enabled: bool`, `btc_trailing_stop_pct: float`, `btc_trailing_window_days: int`, `btc_oi_flush_pct: float`, `btc_oi_near_high_pct: float`, `btc_funding_z: float`, `btc_min_bearish_hours: float`.
  - `MarketRegimeState` singleton with `load() -> MarketRegimeState`; fields `phase` (`Phase.NORMAL|WARNING|BEARISH`), `entered_at: datetime|None`, `reasons: list[str]` (JSON), `guard_state: dict`, `metrics: dict`, `last_evaluated_at: datetime|None`, `last_error: str`, `updated_at`.
  - `OpenInterestPoint(symbol: str, time: datetime, open_interest: float)`, unique on `(symbol, time)`, ordering `time`.
  - `GridEvent.Type.REGIME = "regime"`.

- [ ] **Step 1: Write failing tests** in `tests_regime.py`, class `RegimeModelTests`:
  - `test_settings_defaults`: `StrategySettings.load()` has the seven defaults from Global Constraints.
  - `test_regime_state_singleton_defaults`: `MarketRegimeState.load()` twice returns the same pk; `phase == "normal"`, `reasons == []`, `last_error == ""`.
  - `test_open_interest_point_unique`: second create with the same symbol and time raises `IntegrityError`.
  - `test_settings_api_validates_regime_fields`: POST `/api/strategy/settings/` with `btc_trailing_stop_pct=0.5` and `btc_min_bearish_hours=100` (token header) returns 400 naming both fields; a valid POST of `btc_trailing_stop_pct=0.1` returns 200 and persists.
  - `test_regime_fields_in_strategy_snapshot`: `strategy_snapshot()` contains every new field name.
- [ ] **Step 2: Run** `manage.py test apps.trading.tests_regime` — expect errors (fields/models missing).
- [ ] **Step 3: Implement** the fields, models, and event type; add the fields to `StrategySettingsSerializer.Meta.fields` with bounded validators (ranges above); append the field names to `SNAPSHOT_FIELDS`; run `manage.py makemigrations trading -n btc_market_regime`.
- [ ] **Step 4: Run** the test label again — PASS. Also run `manage.py test apps.etl_api` — PASS (the snapshot change must not break versioning tests).
- [ ] **Step 5: Commit** — `feat(regime): add BTC regime models and settings`.

### Task 2: Readings and the pure evaluator

**Files:**
- Modify: `backend/apps/trading/risk.py` (extract `recovery_ok`)
- Create: `backend/apps/trading/regime.py` (readings + evaluator only in this task)
- Test: `backend/apps/trading/tests_regime.py`

**Interfaces:**
- Consumes: `apps.engine.grid` helpers (`_indicator_series`, `_funding_z_series`, `atr_scale`, `GridParams`, `BARS_PER_DAY`), `apps.trading.risk` (`rsi`, `macd_histogram`, `anchored_vwap`, `evaluate`, `RiskSnapshot`).
- Produces:
  - `risk.recovery_ok(snapshot: RiskSnapshot, since: datetime|None, config: Mapping, now: datetime) -> bool` — the paused-branch checks (cooldown, RSI > recovery and rising, close > VWAP, OI turning up or failsafe since `since`). `risk.evaluate` calls it; behaviour unchanged.
  - `@dataclass(frozen=True) RegimeConfig(trailing_stop_pct, trailing_window_days, oi_flush_pct, oi_near_high_pct, funding_z, min_bearish_hours, risk: Mapping)` with `RegimeConfig.from_settings(settings_row) -> RegimeConfig`.
  - `@dataclass(frozen=True) RegimeReadings(time, price, bar_high, rsi, rsi_prev, macd_hist: tuple, vwap, window_high, high_24h, close_1h, support, atr, momentum, trend_up: bool, funding_z: float|None, oi_values: tuple)`.
  - `reading_series(bars_15m, bars_1h, funding, oi_points, config) -> list[RegimeReadings]` — one reading per closed 15m bar that has full warm-up (≥ 7 days of 15m bars for `window_high`, ≥ 45 days of 1h bars for support/ATR); each reading uses only data with time ≤ that bar's close. Computed vectorized (series once, then indexed); `oi_values` are the up-to-6 hourly OI points ending at or before the reading time, empty when fewer than 2 exist.
  - `@dataclass RegimeDecision(phase: str, reasons: list[str], metrics: dict, guard_state: dict, entered_at: datetime|None, changed: bool)`.
  - `evaluate_regime(phase: str, entered_at: datetime|None, guard_state: dict, reading: RegimeReadings, config: RegimeConfig) -> RegimeDecision` — pure.

Signal definitions (exact):
- `guard_armed`: guard state phase is `armed` after `risk.evaluate`.
- `funding_crowded`: `funding_z is not None and funding_z >= config.funding_z and (momentum < 0 or not trend_up)`.
- `guard_exit`: `risk.evaluate` returned action `pause`.
- `trailing_stop`: `price <= window_high * (1 - trailing_stop_pct)`.
- `oi_flush`: `len(oi_values) >= 2 and (oi_values[-1] / oi_values[0] - 1) <= -oi_flush_pct and price >= high_24h * (1 - oi_near_high_pct)`.
- `support_breakdown`: `support > 0 and close_1h < support - 0.25 * atr`.
- Recovery (leave `bearish`) requires all: `risk.recovery_ok(snapshot, entered_at, config.risk, reading.time)`, `close_1h >= support - 0.25 * atr`, not `trailing_stop`, not `oi_flush`, `reading.time - entered_at >= min_bearish_hours`.

- [ ] **Step 1: Write failing tests**, class `RegimeEvaluatorTests`, using a helper `reading(**overrides)` that returns a neutral `RegimeReadings` (price 100, window_high 101, high_24h 101, rsi 50, support 90, atr 2, close_1h 100, funding_z 0, oi flat):
  - `test_neutral_stays_normal`.
  - `test_trailing_stop_threshold`: window_high 100 → price 92.01 normal, price 92.0 bearish with reason `trailing_stop`.
  - `test_oi_flush_near_high_is_bearish`: oi (100…94.9) and price 99 with high_24h 100 → bearish `oi_flush`; same OI with price 96 (more than 3% below) → normal.
  - `test_support_breakdown`: close_1h 89.4 (support 90, atr 2 → line 89.5) → bearish; 89.6 → normal.
  - `test_funding_crowding_is_warning_only`: funding_z 2.0 with momentum −0.01 → warning `funding_crowded`; with momentum +0.01 and trend_up True → normal.
  - `test_guard_armed_is_warning`: rsi 80 → warning `guard_armed`, `guard_state["phase"] == "armed"`.
  - `test_guard_exit_escalates_to_bearish`: from an armed guard_state with peak 100, price 93.9 → bearish `guard_exit`.
  - `test_normal_jumps_straight_to_bearish` (support breakdown from normal).
  - `test_warning_clears_when_signals_clear`.
  - `test_recovery_requires_every_check_and_min_hours`: in bearish with `entered_at` 3h59m before; all checks pass → still bearish; at 4h00m → normal. Then, separately, each single failing check (rsi 30; close below VWAP; close_1h below line; trailing breached; OI falling with no failsafe elapsed) keeps bearish.
  - `test_recovery_lands_in_warning_if_warning_signal_holds` (funding crowded at recovery).
  - `test_reading_series_is_causal`: truncating the input bars after index k leaves every reading at or before k identical.
  - `test_reading_series_oi_absent_before_coverage`: readings before the first OI point have `oi_values == ()`.
  - `test_risk_evaluate_unchanged_after_extracting_recovery_ok`: run `apps.trading.tests` overbought guard tests (Step 4 command covers it).
- [ ] **Step 2: Run** `manage.py test apps.trading.tests_regime` — fail (module missing).
- [ ] **Step 3: Implement** `recovery_ok` extraction, then `RegimeConfig`, `RegimeReadings`, `reading_series`, `RegimeDecision`, `evaluate_regime` in `regime.py`. Bearish signals are checked before warning signals; `changed` is `phase != previous phase`; `entered_at` is reset to `reading.time` on every phase change.
- [ ] **Step 4: Run** `manage.py test apps.trading.tests_regime apps.trading.tests` — PASS.
- [ ] **Step 5: Commit** — `feat(regime): add BTC readings and pure regime evaluator`.

### Task 3: Live controller and session effects

**Files:**
- Modify: `backend/apps/trading/regime.py` (live functions)
- Modify: `backend/apps/trading/portfolio.py` (`buy_block_reason`)
- Modify: `backend/apps/trading/paper.py` (`tick_paper_sessions` calls `refresh_regime` first; flatten helper reuse)
- Test: `backend/apps/trading/tests_regime.py`

**Interfaces:**
- Consumes: Task 1 models; Task 2 `reading_series`, `evaluate_regime`, `RegimeConfig`; `BinanceFuturesClient.klines`, `.funding_rates`, `.open_interest_hist`; `apps.trading.events.record_event`; `paper.resume_session`; `IncrementalGridStrategy.flatten`.
- Produces:
  - `fetch_live_inputs(client, now) -> tuple[list, list, list, list]` (15m bars ≥ 8 days, 1h bars ≥ 46 days, funding ≥ 31 days, OI last 6 hourly) — raises `BinanceError` on failure.
  - `refresh_regime(now: datetime, client=None) -> MarketRegimeState` — loads settings; if disabled, releases and returns `normal`; otherwise fetches, evaluates the last reading, saves under `select_for_update`, and applies effects. On fetch or short-data failure: phase unchanged, `last_error` set, warning decision log.
  - `apply_bearish(now) -> list[str]` (symbols flattened), `release_regime_pauses(now) -> list[str]` (symbols resumed).
  - `REGIME_PAUSE_SOURCE = "btc_regime"`.
  - `portfolio.buy_block_reason(state, settings_row, regime_phase: str = "normal") -> str` returns `"btc_regime_warning"` for `warning` and `"btc_regime_bearish"` for `bearish`, after the existing checks.

Effects (exact):
- Entering `bearish`: for each `ACTIVE` session not `bias == bearish`: flatten at `mark_price` via `strategy.flatten(price, now, "btc_regime")` inside the session row lock, persist fills, `status = PAUSED`, `risk_state["pause_source"] = "btc_regime"`, `record_event("pause", session=..., reason="btc_regime")`. Errors per session are logged and skipped (untagged, retried next tick while bearish).
- While `bearish`, `apply_bearish` runs every tick (idempotent: only active sessions).
- Leaving `bearish` (or disabled): every `PAUSED` session with `risk_state.pause_source == "btc_regime"` → clear the key, `resume_session`, `record_event("resume", reason="btc_regime")`.
- Every phase change: `record_event("regime", symbol="BTCUSDT", reason=",".join(reasons), before={"phase": old}, after={"phase": new, "reasons": reasons, "metrics": metrics})` plus `log_decision("risk", ...)` with severity `warning` when entering warning/bearish.
- `tick_paper_sessions`: `regime = refresh_regime(now, client)` before `refresh_book_state`; pass `regime.phase` to `buy_block_reason`; include `"regime": {"phase", "reasons"}` in the summary.

- [ ] **Step 1: Write failing tests**, class `RegimeControllerTests` (patch `fetch_live_inputs` and `reading_series` to return controlled readings; reuse `make_coin`/`make_session` from `apps.trading.tests`):
  - `test_fetch_failure_keeps_phase`: phase bearish, fetch raises `BinanceError` → still bearish, `last_error` non-empty.
  - `test_warning_blocks_buys_without_selling`: two active sessions with inventory; warning readings; `tick_paper_sessions` (with `_tick_session` patched to capture `portfolio_block`) passes `"btc_regime_warning"`; positions unchanged. Repeat with `LiveTradingState.mode = "hybrid"` and assert the same block (spec: identical in both modes).
  - `test_bearish_flattens_and_tags_active_sessions`: two active sessions with inventory → both `PAUSED`, `position_qty == 0`, `risk_state["pause_source"] == "btc_regime"`, two `pause` events and one `regime` event.
  - `test_bearish_leaves_manual_coin_guard_and_bearish_locked_alone`: a manually paused session, a session paused by its coin guard (`risk_state.phase == "paused"`), and a `bias="bearish"` session are unchanged.
  - `test_recovery_resumes_only_regime_paused_sessions` (Review Focus 1).
  - `test_recovery_after_long_outage_resumes_in_one_call` (Review Focus 2): bearish `entered_at` 10 h ago, recovered readings → one `refresh_regime` call ends `normal` and resumes tagged sessions.
  - `test_disabling_releases_pauses` (Review Focus 3).
  - `test_flatten_failure_is_retried_next_tick`: first flatten raises for one session → it stays active and untagged; second call while bearish tags it.
  - `test_buy_block_reason_regime`: unit test of `buy_block_reason` for each phase; existing halt/cap reasons take precedence.
- [ ] **Step 2: Run** the label — fail.
- [ ] **Step 3: Implement** the Interfaces above.
- [ ] **Step 4: Run** `manage.py test apps.trading` — PASS.
- [ ] **Step 5: Commit** — `feat(regime): apply BTC regime to all paper sessions each tick`.

### Task 4: Daily analysis respects the regime

**Files:**
- Modify: `backend/apps/engine/analysis.py` (session creation block)
- Test: `backend/apps/trading/tests_regime.py`

**Interfaces:**
- Consumes: `MarketRegimeState.load()`.
- Produces: `analysis.summary["paper_blocked_by_regime"] = True` and no `ensure_paper_sessions` call when the phase is `bearish`.

- [ ] **Step 1: Write failing tests**, class `RegimeAnalysisTests`:
  - `test_no_new_sessions_while_bearish`: patch the walk-forward to return one eligible prime coin; phase bearish → `run_daily_analysis` creates no `PaperSession`; summary flag true; decision log mentions the regime.
  - `test_session_created_in_warning_cannot_buy` (Review Focus 4): phase warning → session is created; its first `_tick_session` receives `portfolio_block == "btc_regime_warning"` and places no buy orders.
- [ ] **Step 2: Run** — fail.
- [ ] **Step 3: Implement** the check before `ensure_paper_sessions` (ranking, reports, retirement unchanged).
- [ ] **Step 4: Run** `manage.py test apps.trading.tests_regime apps.engine` — PASS.
- [ ] **Step 5: Commit** — `feat(regime): skip new paper sessions while BTC is bearish`.

### Task 5: BTC open-interest recorder job

**Files:**
- Modify: `backend/apps/trading/regime.py` (`record_open_interest`)
- Modify: `backend/apps/jobs/handlers.py` (`"btc_open_interest"` handler)
- Modify: `backend/apps/jobs/schedules.py` (schedule slug `btc-open-interest`, interval 3600 s)
- Modify: `backend/apps/jobs/views.py` (job description text)
- Test: `backend/apps/trading/tests_regime.py`

**Interfaces:**
- Produces: `record_open_interest(client=None, symbol="BTCUSDT") -> int` (rows inserted). Fetches 1h OI history from the last stored point (first run: `limit=500`, the Binance maximum) and upserts `OpenInterestPoint`s, ignoring duplicates.

- [ ] **Step 1: Write failing tests**, class `OpenInterestRecorderTests` (patch the client): first call inserts all rows; a second call with overlapping rows inserts only new ones; the `btc-open-interest` schedule exists after `ensure_schedules()` with `interval_seconds == 3600`; queuing `btc_open_interest` twice raises `DuplicateJobQueued`.
- [ ] **Step 2: Run** — fail.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `manage.py test apps.trading.tests_regime apps.jobs` — PASS.
- [ ] **Step 5: Commit** — `feat(regime): record BTC open interest hourly`.

### Task 6: Backtest regime timeline, diagnostics, and parity

**Files:**
- Create: `backend/apps/engine/regime_timeline.py`
- Modify: `backend/apps/engine/hybrid_gate.py` (`combine_gates`)
- Modify: `backend/apps/engine/walkforward.py` (`regime_gate` parameter, without-regime runs)
- Modify: `backend/apps/engine/analysis.py` (BTC 15m/1h sync, build timeline once, pass gates, summary stats)
- Modify: `backend/apps/engine/validation.py` (regime effect stats + finding)
- Test: `backend/apps/engine/tests_regime_timeline.py` (new)

**Interfaces:**
- Consumes: Task 2 `reading_series`, `evaluate_regime`, `RegimeConfig`; Task 1 `OpenInterestPoint`; `Gate`; `sync_coin_market_data(coin, client, lookback_days=..., interval=..., warmup_days=...)`; `load_candles`, `load_funding`.
- Produces:
  - `@dataclass RegimeTimeline(changes: list[tuple[datetime, str]], readings: int, oi_coverage: float)` — phase changes keyed by BTC 15m close time; `stats() -> dict` (`warning_share`, `bearish_share`, `bearish_entries`, `oi_coverage`).
  - `build_regime_timeline(bars_15m, bars_1h, funding, oi_points, config: RegimeConfig) -> RegimeTimeline` — replays `evaluate_regime` over `reading_series(...)` in order, starting from `normal` with empty guard state.
  - `gate_from_timeline(rows, timeline) -> Gate` — for each coin bar open time `t`: phase = last change at or before `t`; `allow = phase == "normal"`; `exit = True` on the first coin bar whose phase is `bearish` after a bar whose phase was not. Bars before the first reading get no entry (ungated).
  - `combine_gates(a: Gate|None, b: Gate|None) -> Gate|None` — allow only if both allow (missing entry counts as allow); exit if either exits.
  - `run_walk_forward(..., regime_gate: Gate | None = None)`: every simulation uses `combine_gates(gate, regime_gate)`; when `regime_gate` is set, test folds and holdout also run with `gate` alone, adding `oos_metrics["regime_off_return"]`, `["regime_effect_return"]` (with − without), `["regime_effect_drawdown"]` (without − with, positive = less drawdown), and the same three keys in `holdout_metrics`.
  - Analysis: when `strategy_settings.btc_regime_enabled`, sync `BTCUSDT` 15m and 1h candles over the analysis window, build the timeline once and cache it in a module-level dict keyed by `(last BTC 15m bar time, RegimeConfig)` so the hybrid pass reuses it (spec §5.2), store `summary["btc_regime"] = timeline.stats()`; pass `regime_gate=gate_from_timeline(rows, timeline)` per coin.
  - Validation: `strategy_validation` adds `regime_effect_return_mean`, `regime_effect_drawdown_mean`; `findings` adds `regime_effective` (strength) when mean drawdown effect > 0 and return cost < drawdown effect, else `regime_costly` (weakness), with the exact numbers in the detail text.

- [ ] **Step 1: Write failing tests** in `tests_regime_timeline.py`:
  - `test_gate_mapping_is_causal`: a change at BTC 15m close 10:15 does not affect the coin bar opening at 10:00 and does affect the bar opening at 11:00.
  - `test_exit_only_on_first_bearish_bar` and `test_warning_blocks_without_exit`.
  - `test_bars_before_coverage_are_ungated` (Review Focus 5).
  - `test_timeline_cached_for_hybrid_pass`: two `run_daily_analysis` calls (normal then hybrid) with the same BTC data call `build_regime_timeline` once.
  - `test_combine_gates` truth table, including `None` inputs.
  - `test_live_and_backtest_phase_changes_match` (spec §5.4): synthetic BTC 15m/1h/funding/OI series with a sell-off; stepping `evaluate_regime` reading by reading (the live path, with state carried between calls) and `build_regime_timeline` produce identical `(time, phase)` changes.
  - `test_walk_forward_reports_regime_effect`: on synthetic coin rows with a regime gate that exits mid-test, `oos_metrics` has the three regime keys and `regime_off_return` equals the result of the same run with `regime_gate=None`.
  - `test_validation_regime_finding`: stats with positive drawdown effect and smaller return cost → `regime_effective` strength; reversed → `regime_costly` weakness.
- [ ] **Step 2: Run** `manage.py test apps.engine.tests_regime_timeline` — fail.
- [ ] **Step 3: Implement** the Interfaces above.
- [ ] **Step 4: Run** `manage.py test apps.engine apps.trading` — PASS (parity and existing walk-forward tests).
- [ ] **Step 5: Commit** — `feat(regime): model the BTC regime in the backtest`.

### Task 7: Regime API endpoint

**Files:**
- Modify: `backend/apps/trading/strategy_views.py` (`MarketRegimeView`)
- Modify: `backend/apps/trading/urls.py` (route `strategy/regime`)
- Test: `backend/apps/trading/tests_regime.py`

**Interfaces:**
- Produces: `GET /api/strategy/regime/` →
  `{"enabled": bool, "phase": str, "entered_at": iso|null, "reasons": [str], "metrics": {...}, "thresholds": {...from settings}, "recovery": [{"check": str, "passed": bool}]|null, "last_evaluated_at": iso|null, "last_error": str, "transitions": [{"time", "from", "to", "reasons"}]}` (last 20 `regime` events, newest first). `recovery` is populated only while bearish, with checks named `guard_recovery`, `above_support`, `trailing_clear`, `no_oi_flush`, `min_duration`. Read-only (no POST).

- [ ] **Step 1: Write failing tests**, class `RegimeApiTests`: response shape for `normal`; while bearish the five recovery checks are listed with booleans; POST returns 405; the ETL events endpoint returns a `regime` event when filtered by `type=regime` (use the ETL auth helper from `apps/etl_api/tests_sessions.py`).
- [ ] **Step 2: Run** — fail.
- [ ] **Step 3: Implement.** Recovery booleans are computed by a `recovery_status(state, config) -> list[dict]` helper in `regime.py` from the stored metrics, reusing the Task 2 checks.
- [ ] **Step 4: Run** `manage.py test apps.trading apps.etl_api` — PASS.
- [ ] **Step 5: Commit** — `feat(regime): expose BTC regime status API`.

### Task 8: Frontend card, settings, and banners

**Files:**
- Modify: `frontend/src/features/grid/types.ts` (`MarketRegime`, `RegimePhase`, settings fields)
- Modify: `frontend/src/features/grid/api.ts` (`marketRegime`), `frontend/src/features/grid/queries.ts` (`useMarketRegime`, refetch 30 s, key `['grid','market-regime']`)
- Create: `frontend/src/features/strategy-admin/components/market-regime-card.tsx`
- Create: `frontend/src/features/grid/components/regime-banner.tsx`
- Modify: `frontend/src/features/strategy-admin/components/strategy-overview.tsx` (card after `TradingModeCard`)
- Modify: `frontend/src/features/strategy-admin/components/strategy-settings-card.tsx` (Zod schema + fields)
- Modify: `frontend/src/app/dashboard/page.tsx`, `frontend/src/app/dashboard/paper/page.tsx` (banner)
- Modify: `frontend/src/features/strategy-admin/catalog.ts` (regime entry in the grid-strategy section)

**Interfaces:**
- Consumes: Task 7 response; Task 1 settings fields.
- Produces: `<MarketRegimeCard />`, `<RegimeBanner />` (renders nothing when phase is `normal` or regime disabled; amber for warning, red for bearish, text states the effect and the triggering reasons).

Card content: phase badge (green normal / amber warning / red bearish / neutral "off"), triggering reasons with `entered_at`, one row per signal showing the reading against its threshold (drawdown % of trailing stop, OI change vs flush %, distance to support line, funding z vs threshold, RSI), the recovery checklist while bearish, the last 5 transitions, and `last_error` as a warning note when set.

Settings: `btc_regime_enabled` (SwitchField) and the six numeric thresholds (TextField `type='number'`) with Zod bounds equal to the Global Constraints ranges; trailing and OI percentages are edited as percents and stored as fractions, like `daily_loss_limit_pct`.

- [ ] **Step 1: Implement** the types, API, query, card, banner, settings fields, and catalog entry.
- [ ] **Step 2: Run** `npx tsc --noEmit` and `npx oxlint` on the changed files — no errors.
- [ ] **Step 3: Browser check** (backend and frontend dev servers running): set `MarketRegimeState.phase` to `warning`, then `bearish` (with reasons and metrics) via `manage.py shell`, and confirm the card, recovery checklist, and banners render on Admin → Strategies, Overview, and Paper sessions; set it back to `normal` and confirm banners disappear; change a threshold in settings and confirm it saves and validates (e.g. trailing 50% rejected). Check at 375 px width for horizontal overflow.
- [ ] **Step 4: Commit** — `feat(regime): show BTC regime status, settings, and banners`.

### Task 9: Documentation and full verification

**Files:**
- Modify: `README.md` (regime section: signals, phases, recovery, settings table rows, backtest coverage note, OI recorder schedule)
- Modify: `.env.example` (no new env vars; one comment pointing to Admin → Strategies for regime settings)

- [ ] **Step 1: Update** the docs.
- [ ] **Step 2: Run** `manage.py test apps --noinput` — all PASS; `npx tsc --noEmit` clean; `npx next build` succeeds.
- [ ] **Step 3: Run** one `daily_analysis` job locally with the worker and confirm `summary["btc_regime"]` is present and coin results carry `regime_effect_*` metrics.
- [ ] **Step 4: Commit** — `docs(regime): document BTC market regime protection`.
