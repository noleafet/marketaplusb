# Hybrid Session Sourcing — Design

Date: 2026-10-30
Status: Approved design, pending spec review

## 1. Intent

When hybrid trend mode is on, paper sessions must be chosen, parameterised and
retired by the hybrid analysis: the grid backtest with the closed 1h EMA(100)
trend filter, 15m exit confirmation, and the ADX check when it is enabled.
That is the strategy the sessions actually trade in hybrid mode. Today they
are always chosen by the normal (ungated) analysis, so in hybrid mode coins
are picked by a strategy that differs from the one executed.

Success criteria:

- In hybrid mode, new paper sessions open from the hybrid ranking and its tuned
  params, and existing sessions are retired against the hybrid ranking.
- In normal mode, behaviour is unchanged.
- The hybrid analysis gets the same validation depth as the normal one: full
  lookback (`ENGINE_LOOKBACK_DAYS`, 520 by default) and the holdout.
- The normal/hybrid comparison stays available by default.
- Switching mode never silently starts a long job, but shows when the new
  mode's ranking is stale and offers a rerun.

### Decisions made with the user

| Topic | Decision |
|---|---|
| Comparison run | On by default; a setting can turn it off |
| On mode switch | Show staleness and offer a rerun (no automatic run) |
| Sessions open at switch time | Retire gradually through the existing failed-run rule |
| Approach | A mode-aware daily job: the active mode runs first and drives sessions |

### Out of scope

- Changes to the hybrid gate rule itself, the tick-time gate, or the BTC
  regime logic.
- Live trading (no live runner exists).
- Making existing sessions retire immediately on a mode switch.

## 2. Current state

- `handle_daily_analysis` (`apps/jobs/handlers.py`) always runs the normal
  analysis, then the hybrid one. The job fails if either fails.
- In `_run_daily_analysis` (`apps/engine/analysis.py`), `hybrid` runs:
  - skip candle sync, relying on the normal run that just synced (`sync=not hybrid`);
  - skip reports (`analysis.scope == UNIVERSE and not hybrid`);
  - skip `_bootstrap_paper_sessions` (`if not hybrid:`);
  - use `lookback = min(lookback_days, HYBRID_LOOKBACK_DAYS)` (180) and holdout 0;
  - fetch 1h and 15m klines live from Binance per coin in
    `_build_hybrid_gate` via `_closed_klines`.
- `market_candles` already stores any interval, and `sync_coin_market_data`
  syncs incrementally per interval. On the dev database, 1h is stored for all
  60 eligible coins (about 600 days, because the signal interval is 1h); 15m is
  stored for BTC only (regime timeline); 4h for 60 coins.
- `latest_analysis(mode=NORMAL)` defaults to normal and feeds the dashboard,
  rankings and the settings staleness check.
- `set_trading_mode` switches `LiveTradingState.mode`; switching to hybrid
  needs `confirm=true` and purges open paper orders and positions.
- The settings view already has a staleness and rerun pattern:
  `analysis_stale` in the payload and `rerun_analysis` in the request, which
  enqueues `daily_analysis` and returns `job_conflict` on `JobAlreadyActive`.

## 3. Design

### 3.1 Mode-aware daily job

`handle_daily_analysis`:

```
active = LiveTradingState.load().mode            # "normal" | "hybrid"
other  = the other mode
primary = run_daily_analysis(trend_mode=active, drives_sessions=True, ...)
if primary failed: raise (job fails, as today)
if StrategySettings.run_comparison_analysis:
    try: comparison = run_daily_analysis(trend_mode=other, drives_sessions=False, ...)
    except / failed: log a warning DecisionLog, record it in the result; job still succeeds
snapshot open sessions "daily" (unchanged)
```

- The result payload keeps `analysis_id` and `status` for the primary run, and
  gains `mode` and `comparison` (`{analysis_id, status, mode}`, or
  `{"skipped": true}`, or `{"error": "..."}`). The existing `hybrid` key is
  replaced by `comparison`. Nothing outside the jobs app and tests reads it.
- The mode is read once at job start. A switch during a running job takes
  effect on the next run.

`run_daily_analysis` and `_run_daily_analysis` gain `drives_sessions: bool = False`.

### 3.2 Inside a run

Every `if not hybrid` / `and not hybrid` that gates bookkeeping is replaced by
`drives_sessions`:

| Behaviour | Today | New |
|---|---|---|
| `_bootstrap_paper_sessions` (open and retire) | normal only | `drives_sessions` only |
| Reports (universe scope) | normal only | `drives_sessions` only |
| Candle sync | normal only | every run (see 3.3) |
| Lookback | normal: full; hybrid: 180 | both full |
| Holdout | normal: `holdout_days`; hybrid: 0 | both `holdout_days` |

`AnalysisRun` gains `drives_sessions = BooleanField(default=False, db_index=True)`,
set at creation from the argument. A manual or API-triggered analysis (no job)
defaults to `drives_sessions=False` unless its caller passes it. The job is
the only caller that passes `True`.

The hybrid-specific simulation (gate application, `hybrid_blocked_bars`,
`hybrid_exits`, gate stats) is unchanged.

### 3.3 Stored 1h/15m gate inputs

- Each run syncs, per coin: the base interval as today, and in hybrid runs
  also `1h` and `15m`, through `sync_coin_market_data` with the same lookback
  and warmup. A sync already done earlier in the same job is a cheap no-op:
  the incremental sync fetches only bars newer than the latest stored one.
- `_build_hybrid_gate(symbol, rows, *, lookback_days, adx_enabled)` reads 1h
  and 15m from `market_candles` via `load_candles`, over the same window it
  uses today (`lookback_days + HYBRID_WARMUP_DAYS`), keeping only closed bars.
  It no longer takes a client. `_closed_klines` is removed if nothing else
  uses it.
- `ENGINE_HYBRID_LOOKBACK_DAYS` is removed from settings and the README. The
  `max(120, …)` clamp goes with it.
- A coin whose 1h or 15m coverage does not reach the window start stays
  ineligible with `exclusion_reason = "hybrid trend data unavailable"`, as
  today. The run summary gains `hybrid_coverage: {"coins": n, "covered": m}`.
- First run after deploy: the 15m backfill (about 2,300 Binance requests across
  60 coins at 1,500 bars per request) happens inside that run's sync step.
  It is per coin and incremental, so an interrupted run resumes where it stopped.

### 3.4 Settings

`StrategySettings.run_comparison_analysis = BooleanField(default=True)`.
It appears in the strategy settings serializer and the settings page as
"Run comparison analysis", with help text saying it runs the inactive mode
after the active one, for the comparison view.

### 3.5 Mode switch flow

`POST /api/live/status/mode/` (the `mode` action):

- The response (and `GET`) gains, for the mode now active:
  - `latest_analysis`: `{id, started_at, mode}` of the latest succeeded universe
    run of that mode, or null.
  - `analysis_stale`: true when there is no such run, or when its `started_at`
    is older than the latest succeeded universe run of the other mode.
- The request accepts `rerun_analysis: true`. When it is set and the result is
  stale, the view enqueues `daily_analysis` (trigger manual) and returns `job`,
  or `job_conflict` with the existing message on `JobAlreadyActive`. This is
  the same contract as the settings view.
- The confirmation step for switching to hybrid is unchanged.

Trading-mode card (`frontend/src/features/strategy-admin/components/trading-mode-card.tsx`):
when `analysis_stale` is true, show a short notice ("The hybrid ranking is older
than the normal one" / "No hybrid analysis yet") and a "Run analysis now"
button that posts the current mode with `rerun_analysis: true`. Show the job
or conflict message the response returns.

### 3.6 Which run is current

- `latest_analysis` gets a module constant `ACTIVE_MODE = "active"` as its new
  default. `mode=ACTIVE_MODE` resolves to `LiveTradingState.load().mode`.
  Explicit `"normal"`/`"hybrid"` behave as today, and a falsy mode keeps
  today's meaning (any mode).
- Callers that relied on the old normal default get the active mode:
  - the dashboard summary (`engine/views.py` `DashboardSummaryView`);
  - coin results (`engine/views.py` ~95, where `?mode` falls back to the
    active mode instead of `NORMAL`);
  - the latest reports (`engine/views.py` ~145; reports now come only from the
    run that drives sessions);
  - the settings staleness check and the strategy validation view
    (`trading/strategy_views.py` ~123 and ~245).
  `?mode=normal|hybrid` still selects explicitly.
- Dashboard: the existing hybrid block becomes the comparison block. It shows
  the latest run of the inactive mode, labelled with that mode. Frontend copy
  changes from "hybrid" to "comparison (<mode>)" where it describes that block.

### 3.7 Records and ETL API

- Migration (engine): add `AnalysisRun.drives_sessions`. Backfill it true for
  existing runs with `mode="normal"` and `scope="universe"` whose summary has a
  `paper_sessions_created` key (the runs that actually bootstrapped sessions),
  and false for the rest.
- ETL API `backtests/`: add `drives_sessions` after `scope` in `backtest_out`.
  Update `RESOURCES`, the frozen contract tuple and `docs/etl-api-v1.md`.

## 4. Error handling

- Primary run fails: the job fails, exactly as a failed normal run does today.
- Comparison run fails: a warning DecisionLog
  (`category="analysis"`, payload `{mode, error}`), and the job result records
  `comparison.error`. The job succeeds, because sessions are already handled.
- 15m backfill partially fails: the affected coins are ineligible for that hybrid
  run with the existing reason, and `data_errors` lists them. The run still succeeds.
- `rerun_analysis` with a job already running: `job_conflict` message, and the
  mode switch itself still applies.

## 5. Testing

- Job: in hybrid mode it runs hybrid (`drives_sessions=True`) then normal; in
  normal mode it runs normal then hybrid; with the comparison off, one run only;
  a failed comparison leaves the job succeeded with `comparison.error`; a failed
  primary fails the job.
- Sessions: with hybrid active, `ensure_paper_sessions` receives the hybrid
  run's ranking and params, and `retire_stale_sessions` compares against it.
  With normal active, the behaviour is unchanged.
- Reports: built only by the run with `drives_sessions`.
- Gate parity: `_build_hybrid_gate` over stored klines equals `build_gate` over
  the same klines passed directly. Closed-bar filtering matches the old
  `_closed_klines` (no forming bar).
- Hybrid lookback and holdout: a hybrid run uses the full lookback and `holdout_days`.
- Coverage: a coin missing 15m is excluded with the existing reason, and
  `hybrid_coverage` counts it.
- Mode switch: `analysis_stale` covers no run, an older run and a newer run;
  `rerun_analysis` enqueues when stale, does nothing when fresh, and returns
  `job_conflict` when a job is active.
- `latest_analysis()` default follows `LiveTradingState.mode`.
- Migration: the `drives_sessions` backfill rules.
- ETL contract: the `backtests/` field list includes `drives_sessions`.
- Frontend: the trading-mode card renders the stale notice and the rerun button
  (the existing component test pattern, if any; otherwise manual check in the preview).
