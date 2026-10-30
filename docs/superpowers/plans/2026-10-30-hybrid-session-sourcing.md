# Hybrid Session Sourcing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When hybrid trend mode is on, paper sessions are opened, parameterised and retired from a full-length hybrid analysis, with the other mode's run kept as an optional comparison.

**Architecture:** The daily job reads `LiveTradingState.mode` and runs that mode first with `drives_sessions=True`, which alone opens and retires sessions and builds reports. The other mode runs afterwards as a comparison when `StrategySettings.run_comparison_analysis` is on. The hybrid run moves from a 180-day live Binance fetch to stored 1h/15m candles over the full lookback, with the holdout. Mode switching reports staleness and can queue a rerun.

**Tech Stack:** Django 5.2, DRF 3.16, Postgres/Timescale (`market_candles`), Next.js frontend (TypeScript, TanStack Query). No new dependencies.

**Spec:** `docs/superpowers/specs/2026-10-30-hybrid-session-sourcing-design.md`

## Global Constraints

- All backend paths are relative to `backend/`. Run backend tests from `backend/` with `.venv/Scripts/python.exe manage.py test <label> -v 2`. The full suite is `apps`.
- Frontend checks run from `frontend/`: `bun run typecheck` and `bun run lint`. There is no frontend test runner.
- The dev Postgres must be reachable on 127.0.0.1:5432. Never start Podman or containers. If the database is down, stop and report.
- Commit on `main` (no feature branch). Each commit message ends with a blank line and `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Never add `.claude/` or `.superpowers/`.
- No new dependencies.
- The hybrid gate rule, the tick-time gate, and the BTC regime logic stay unchanged, apart from the regime sync flag.
- `run_comparison_analysis` is NOT added to `versioning.strategy_snapshot`.
- The existing hybrid exclusion reason stays exactly `"hybrid trend data unavailable"`.
- Do not run `migrate` against the dev database. The controller decides that.

## Review Focus

1. **Mode switched while a daily job is running.** The job keeps the mode it read at start: the run that drives sessions and the comparison don't swap mid-job (Task 4 test `test_mode_read_once_at_job_start`).
2. **Hybrid primary run with the BTC regime enabled, when BTC 15m has never been synced or is behind.** The regime inputs are synced before the timeline is built (Task 3 test `test_hybrid_primary_syncs_regime_inputs`).
3. **A coin whose stored 1h history starts after the window start** (for example after the signal interval changed from 4h to 1h). The sync forces a full back-fill instead of a forward-only top-up (Task 2 test `test_sync_window_forces_backfill_when_history_short`).
4. **A manual or API-triggered analysis while hybrid mode is on.** It goes through the same `daily_analysis` job, so the hybrid run drives sessions (Task 4 test `test_manual_trigger_uses_active_mode`).
5. **A solo-scope run (`symbols` param) in hybrid mode.** It may open sessions for those symbols but never retires others, as today for normal runs (Task 3 test `test_solo_hybrid_run_does_not_retire`).

---

## File Structure

| File | Change |
|---|---|
| `apps/trading/models.py` | `StrategySettings.run_comparison_analysis` |
| `apps/trading/migrations/0011_strategysettings_run_comparison_analysis.py` | new |
| `apps/engine/models.py` | `AnalysisRun.drives_sessions` |
| `apps/engine/migrations/0009_analysisrun_drives_sessions.py` | new; schema plus RunPython backfill |
| `apps/engine/analysis.py` | `_sync_window`, stored-kline `_build_hybrid_gate`, `drives_sessions`, full hybrid lookback, `ACTIVE_MODE` |
| `apps/jobs/handlers.py` | mode-aware job with comparison |
| `apps/trading/modes.py`, `apps/trading/serializers.py`, `apps/trading/views.py` | mode staleness and rerun |
| `apps/trading/strategy_views.py` | settings field; `latest_analysis` callers |
| `apps/engine/views.py` | `latest_analysis` callers; comparison block |
| `apps/etl_api/serializers.py`, `apps/etl_api/views/meta.py`, `apps/etl_api/tests_contract.py`, `docs/etl-api-v1.md` | `drives_sessions` |
| `config/settings.py`, `README.md` | remove `HYBRID_LOOKBACK_DAYS`; docs |
| `frontend/src/features/grid/types.ts`, `.../strategy-admin/components/trading-mode-card.tsx`, `.../strategy-settings-card.tsx`, `.../strategy-overview.tsx`, plus the API/query hook for the trading mode | UI |
| `apps/engine/tests_hybrid_sourcing.py` | new test module for Tasks 2–5 |

---

### Task 1: Settings and record fields

**Files:**
- Modify: `apps/trading/models.py` (StrategySettings, near `holdout_days` ~289), `apps/engine/models.py` (AnalysisRun)
- Create: `apps/trading/migrations/0011_strategysettings_run_comparison_analysis.py`, `apps/engine/migrations/0009_analysisrun_drives_sessions.py`
- Modify: `apps/trading/strategy_views.py` (the settings serializer field list)
- Test: `apps/engine/tests_hybrid_sourcing.py` (create)

**Interfaces:**
- Produces:
  - `StrategySettings.run_comparison_analysis = models.BooleanField(default=True)`
  - `AnalysisRun.drives_sessions = models.BooleanField(default=False, db_index=True)`
  - Migration function `backfill_drives_sessions(apps, schema_editor)`. It sets `drives_sessions=True` for runs with `mode="normal"`, `scope="universe"` and a `"paper_sessions_created"` key in `summary`, and leaves everything else false. Reverse is `RunPython.noop`.

- [ ] **Step 1: Write the failing tests** in `tests_hybrid_sourcing.py`

```python
class FieldTests(TestCase):
    def test_defaults(self):
        self.assertTrue(StrategySettings.load().run_comparison_analysis)
        self.assertFalse(AnalysisRun.objects.create().drives_sessions)

    def test_comparison_toggle_does_not_change_strategy_version(self):
        a = resolve_strategy_version()
        row = StrategySettings.load(); row.run_comparison_analysis = False; row.save()
        self.assertEqual(resolve_strategy_version(row).id, a.id)

    def test_settings_api_exposes_and_updates_toggle(self):
        # GET /api/strategy/settings/ has run_comparison_analysis True;
        # POST {"run_comparison_analysis": false} with the engine token persists False

    def test_backfill_marks_only_session_driving_runs(self):
        drove = AnalysisRun.objects.create(mode="normal", scope="universe", summary={"paper_sessions_created": 0})
        solo = AnalysisRun.objects.create(mode="normal", scope="solo", summary={"paper_sessions_created": 1})
        hyb = AnalysisRun.objects.create(mode="hybrid", scope="universe", summary={})
        old = AnalysisRun.objects.create(mode="normal", scope="universe", summary={})
        import_module("apps.engine.migrations.0009_analysisrun_drives_sessions").backfill_drives_sessions(django_apps, None)
        # refresh each: drove True; solo, hyb, old False
```

- [ ] **Step 2: Run to verify failure:** `.venv/Scripts/python.exe manage.py test apps.engine.tests_hybrid_sourcing -v 2`. Expected: errors (missing fields and module).
- [ ] **Step 3: Implement.** Add the fields. Run `makemigrations trading -n strategysettings_run_comparison_analysis` and `makemigrations engine -n analysisrun_drives_sessions`. Add the `RunPython(backfill_drives_sessions, RunPython.noop)` operation after the AddField in the engine migration. Add `"run_comparison_analysis"` to the settings serializer `fields`.
- [ ] **Step 4: Run** the module, then the full suite → PASS.
- [ ] **Step 5: Commit:** `git commit -m "Add the comparison-run setting and the drives_sessions record"`

---

### Task 2: Stored 1h/15m gate inputs and back-fill-aware sync

**Files:**
- Modify: `apps/engine/analysis.py` (`_closed_klines` ~188, `_build_hybrid_gate` ~203, `_regime_timeline` sync loop ~257-270)
- Test: `apps/engine/tests_hybrid_sourcing.py`

**Interfaces:**
- Consumes: `apps.engine.timescale.load_candles(symbol, interval, start, end)`, `earliest_candle_time(symbol, interval)`, `apps.engine.binance_sync.sync_coin_market_data(coin, client, *, lookback_days, interval, warmup_days, force)`.
- Produces:
  - `_sync_window(coin, client, interval: str, *, lookback_days: int, warmup_days: int) -> dict`. It computes `start = now - (lookback_days + warmup_days)` days and calls `sync_coin_market_data(..., force=earliest is None or earliest > start + timedelta(days=1))`, where `earliest = earliest_candle_time(coin.binance_symbol, interval)`. It returns the sync result.
  - `_build_hybrid_gate(symbol: str, rows: list[dict], *, lookback_days: int, adx_enabled: bool) -> Gate | None`. No client. It reads 1h and 15m with `load_candles(symbol, interval, start, end)` over `start = now - (lookback_days + HYBRID_WARMUP_DAYS)` days, keeps only closed bars (`row["time"] + step <= end`), then calls `build_gate`. It returns None when `gate.allow` is empty, as today.
  - `_closed_klines` is deleted. The closed-bar filter becomes `_closed(rows, interval, end) -> list[dict]`.
  - `_regime_timeline` uses `_sync_window` for its `("15m", "1h")` loop. Its behaviour is unchanged.

- [ ] **Step 1: Write the failing tests** (seed `market_candles` with `upsert_candles`; patch `sync_coin_market_data` where only its arguments matter)

```python
class StoredGateTests(TestCase):
    def test_gate_from_stored_klines_equals_build_gate(self):
        # seed 1h and 15m for "AAAUSDT" over 200 days plus base rows;
        # _build_hybrid_gate("AAAUSDT", rows, lookback_days=180, adx_enabled=False)
        # == build_gate(rows, closed_1h, closed_15m, adx_enabled=False) on allow/exit/derisk dicts

    def test_forming_bar_is_excluded(self):
        # store a 15m bar whose time + 15m > now; it is absent from the klines passed to build_gate (patch build_gate, inspect args)

    def test_missing_15m_returns_none(self):
        # only 1h stored -> None

    @patch("apps.engine.analysis.sync_coin_market_data")
    def test_sync_window_forces_backfill_when_history_short(self, sync):
        # stored 1h earliest = now-30d, window = 520+45 days -> force=True

    @patch("apps.engine.analysis.sync_coin_market_data")
    def test_sync_window_incremental_when_covered(self, sync):
        # stored earliest older than the window start -> force=False

    @patch("apps.engine.analysis.sync_coin_market_data")
    def test_sync_window_forces_when_nothing_stored(self, sync):
        # no rows -> force=True
```

- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement** the three functions. Update the one `_build_hybrid_gate` call site to the new signature; Task 3 changes the surrounding logic. Update `tests_hybrid.py` only where it calls the removed `_closed_klines` or the old signature.
- [ ] **Step 4: Run** `apps.engine.tests_hybrid_sourcing apps.engine.tests_hybrid apps.engine.tests_regime_timeline` → PASS, then the full suite.
- [ ] **Step 5: Commit:** `git commit -m "Build the hybrid gate from stored 1h/15m candles"`

---

### Task 3: drives_sessions and a full-length hybrid run

**Files:**
- Modify: `apps/engine/analysis.py` (`run_daily_analysis` ~398, `_run_daily_analysis` ~413-460, the regime sync ~532-537, the per-coin sync ~625-645, the gate ~674-697, summary ~901-915, reports and bootstrap ~939-955), `config/settings.py` (remove `HYBRID_LOOKBACK_DAYS`)
- Test: `apps/engine/tests_hybrid_sourcing.py`

**Interfaces:**
- Consumes: `_sync_window`, `_build_hybrid_gate` (Task 2); `AnalysisRun.drives_sessions` (Task 1).
- Produces:
  - `run_daily_analysis(*, trigger="scheduled", params=None, job_run_id=None, trend_mode=AnalysisRun.Mode.NORMAL, drives_sessions: bool = False) -> AnalysisRun`. The `_run_daily_analysis` signature mirrors it.
  - `AnalysisRun.objects.create(..., drives_sessions=drives_sessions)`
  - `analysis.summary["hybrid_coverage"] = {"coins": n_attempted, "covered": n_with_gate}` on hybrid runs.

Rules:
- `holdout_days = int(strategy_settings.holdout_days or 0)` and `lookback_days = analysis_params["lookback_days"]` for both modes. Delete `hybrid_days` and its comment.
- The regime timeline call uses `sync=True`.
- Per coin, every run: the base-interval sync as today (the `if not hybrid:` guard goes). In hybrid runs, also `_sync_window(coin, client, "1h", ...)` and `_sync_window(coin, client, "15m", ...)` with `lookback_days` and `warmup_days = HYBRID_WARMUP_DAYS`. Sync errors are appended to `data_errors` as today.
- Reports: `if analysis.scope == UNIVERSE and drives_sessions:`. Bootstrap: `if drives_sessions:`.
- `"warmup_days": HYBRID_WARMUP_DAYS` in the summary is unchanged.

- [ ] **Step 1: Write the failing tests.** Patch `BinanceFuturesClient`, the sync functions and `run_walk_forward`/`backtest_coin` the way `tests_hybrid.py` and `tests_regime_timeline.py` already do. Copy their fixtures.

```python
class RunBehaviourTests(TestCase):
    def test_hybrid_run_uses_full_lookback_and_holdout(self):
        # run hybrid; analysis.params["lookback_days"] == normal lookback;
        # params["config"]["holdout_days"] == StrategySettings.holdout_days

    def test_only_driving_run_bootstraps_and_reports(self):
        # patch _bootstrap_paper_sessions and build_reports; drives_sessions=False -> neither called;
        # True (hybrid) -> both called once

    def test_solo_hybrid_run_does_not_retire(self):
        # params={"symbols": ["AAA"]}, hybrid, drives_sessions=True -> retire_stale_sessions not called

    def test_hybrid_primary_syncs_regime_inputs(self):
        # btc_regime_enabled, hybrid run -> _regime_timeline called with sync=True

    def test_hybrid_run_syncs_1h_and_15m_per_coin(self):
        # _sync_window called with "1h" and "15m" for each eligible coin

    def test_hybrid_coverage_summary(self):
        # 2 coins, one with no 15m -> {"coins": 2, "covered": 1}; the uncovered coin keeps
        # exclusion_reason "hybrid trend data unavailable"

    def test_drives_sessions_recorded(self):
        # AnalysisRun.drives_sessions matches the argument

    def test_comparison_reuses_regime_timeline(self):
        # two runs back to back with the same window and regime config:
        # build_regime_timeline is called once (patch it and count)
```

- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement** per the rules. Remove `HYBRID_LOOKBACK_DAYS` from `config/settings.py` and any remaining reference (grep first).
- [ ] **Step 4: Run** `apps.engine` and then the full suite → PASS. Update existing tests that asserted the 180-day clamp or holdout 0 for hybrid, and list each one in the report.
- [ ] **Step 5: Commit:** `git commit -m "Run the hybrid analysis at full length and let either mode drive sessions"`

---

### Task 4: Mode-aware daily job

**Files:**
- Modify: `apps/jobs/handlers.py` (`handle_daily_analysis` ~28-75), `apps/engine/tests_hybrid.py` (`AnalysisModeTests` job tests ~258-300)
- Test: `apps/engine/tests_hybrid_sourcing.py`

**Interfaces:**
- Consumes: `run_daily_analysis(..., trend_mode, drives_sessions)` (Task 3), `StrategySettings.run_comparison_analysis` (Task 1), `LiveTradingState.load().mode`.
- Produces the job result: `{"analysis_id", "status", "summary", "duration_seconds", "mode", "comparison"}`, where `comparison` is one of `{"analysis_id", "status", "mode", "duration_seconds"}`, `{"skipped": True}`, or `{"mode", "error"}`. The `hybrid` key is removed.

Rules: the mode is read once at the start. The primary run is called with `drives_sessions=True`, the comparison with `drives_sessions=False`. A primary that raises or doesn't succeed raises `RuntimeError` (as today). A comparison that raises or doesn't succeed writes `log_decision("analysis", f"Comparison {mode} analysis failed: {error}", severity="warning", payload={"mode": mode, "error": error}, job_run_id=job_run.id)` and records `comparison.error`; the job continues. The daily snapshots run after both, as today.

- [ ] **Step 1: Write the failing tests** (patch `apps.jobs.handlers.run_daily_analysis` with the `fake` pattern from `tests_hybrid.py:258`)

```python
class DailyJobTests(TestCase):
    def test_hybrid_mode_runs_hybrid_first(self):
        # LiveTradingState mode=hybrid -> calls [(hybrid, True), (normal, False)]; result["mode"]=="hybrid"
    def test_normal_mode_runs_normal_first(self): ...    # [(normal, True), (hybrid, False)]
    def test_comparison_off_runs_once(self): ...         # result["comparison"] == {"skipped": True}
    def test_failed_comparison_keeps_job_succeeded(self):
        # comparison FAILED -> no raise; result["comparison"]["error"]; one warning DecisionLog
    def test_failed_primary_fails_job(self): ...         # RuntimeError
    def test_mode_read_once_at_job_start(self):
        # the fake flips LiveTradingState.mode during the first call; the second call is still the other mode of the original
    def test_manual_trigger_uses_active_mode(self):
        # JobRun trigger="manual", mode hybrid -> the first call is (hybrid, True)
```

Rewrite the two existing `AnalysisModeTests` job tests to the new contract: `test_daily_job_runs_hybrid_after_normal` becomes the normal-mode order with a `comparison` key, and `test_hybrid_failure_is_reported_after_normal_results` becomes the comparison-failure case.

- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `apps.engine apps.jobs` and the full suite → PASS.
- [ ] **Step 5: Commit:** `git commit -m "Run the active trading mode's analysis first and the other as a comparison"`

---

### Task 5: The active mode as the default "current" run

**Files:**
- Modify: `apps/engine/analysis.py` (`latest_analysis` ~990), `apps/engine/views.py` (~95, ~145, `DashboardSummaryView` ~181-216), `apps/trading/strategy_views.py` (~123, ~245), `frontend/src/features/grid/types.ts` (~234), `frontend/src/features/strategy-admin/components/strategy-overview.tsx` (~78)
- Test: `apps/engine/tests_hybrid_sourcing.py`, `apps/engine/tests_hybrid.py` (`test_latest_analysis_separates_modes`)

**Interfaces:**
- Produces:
  - `ACTIVE_MODE = "active"` in `apps/engine/analysis.py`
  - `latest_analysis(mode: str = ACTIVE_MODE) -> AnalysisRun | None`. `ACTIVE_MODE` resolves to `LiveTradingState.load().mode`, explicit modes behave as today, and a falsy mode means any mode, as today.
  - Dashboard payload: `engine.latest_comparison_analysis` replaces `engine.latest_hybrid_analysis`. It holds the latest succeeded universe run of the inactive mode (serialized with `AnalysisRunSerializer`, including `mode`), or null.

- [ ] **Step 1: Write the failing tests**

```python
class CurrentRunTests(TestCase):
    def test_latest_analysis_follows_active_mode(self):
        # normal and hybrid universe runs; mode normal -> normal; set hybrid -> hybrid;
        # explicit modes unaffected; latest_analysis(mode="") -> the newest of either
    def test_results_default_follows_active_mode(self):
        # GET /api/engine/results/ without ?mode -> rows of the active mode's run (ENGINE_ALLOW_PUBLIC_READS or token)
    def test_dashboard_comparison_is_inactive_mode(self):
        # hybrid active -> engine.latest_comparison_analysis.mode == "normal"; the key latest_hybrid_analysis is absent
```

Update `test_latest_analysis_separates_modes` so it sets `LiveTradingState` mode explicitly for the default assertion.

- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement** the backend. Rename the frontend type field to `latest_comparison_analysis` and update `strategy-overview.tsx`: the block label reads "Comparison run (<mode>)" using the run's `mode`.
- [ ] **Step 4: Run** the backend module and the full suite → PASS. From `frontend/`, `bun run typecheck` and `bun run lint` → no errors.
- [ ] **Step 5: Commit:** `git commit -m "Treat the active trading mode's run as the current analysis"`

---

### Task 6: Mode-switch staleness, rerun, and the settings toggle in the UI

**Files:**
- Modify: `apps/trading/modes.py` (`mode_payload` ~28), `apps/trading/serializers.py` (`TradingModeSerializer` ~168), `apps/trading/views.py` (the `mode` action ~289-314)
- Modify: `frontend/src/features/grid/types.ts`, the trading-mode API/query hook (where `useSetTradingMode` is defined), `frontend/src/features/strategy-admin/components/trading-mode-card.tsx`, `frontend/src/features/strategy-admin/components/strategy-settings-card.tsx`
- Test: `apps/engine/tests_hybrid_sourcing.py`

**Interfaces:**
- Consumes: `latest_analysis` (Task 5), `apps.jobs.runner.enqueue_job`, `JobAlreadyActive`, `JobRunSerializer` (as used in `strategy_views.py` ~156-170).
- Produces:
  - `mode_analysis_status(mode: str) -> dict` in `apps/trading/modes.py`, returning `{"latest_analysis": {"id", "started_at", "mode"} | None, "analysis_stale": bool}`. It is stale when that mode has no succeeded universe run, or when its `started_at` is older than the other mode's latest succeeded universe run.
  - `mode_payload(state)` includes `**mode_analysis_status(state.mode)`.
  - `TradingModeSerializer.rerun_analysis = BooleanField(default=False)`.
  - The `mode` POST: after the switch succeeds, if `rerun_analysis` and `analysis_stale`, it calls `enqueue_job("daily_analysis", trigger=JobRun.Trigger.MANUAL, params={})` and adds `job` (JobRunSerializer data); on `JobAlreadyActive` it adds `job_conflict = exc.message`. A 409 `requires_confirmation` response is unchanged and never enqueues.

- [ ] **Step 1: Write the failing tests**

```python
class ModeSwitchTests(TestCase):
    def test_stale_when_no_run_for_mode(self): ...       # GET mode, hybrid has no run -> analysis_stale True, latest_analysis None
    def test_stale_when_older_than_other_mode(self): ...
    def test_fresh_when_newer(self): ...                 # analysis_stale False
    @patch("apps.trading.views.enqueue_job")
    def test_rerun_enqueues_when_stale(self, enqueue): ...  # POST {"mode":"hybrid","confirm":true,"rerun_analysis":true} -> enqueue called once; "job" in body
    @patch("apps.trading.views.enqueue_job")
    def test_rerun_skipped_when_fresh(self, enqueue): ...   # not called
    @patch("apps.trading.views.enqueue_job", side_effect=JobAlreadyActive(...))
    def test_rerun_conflict_reported(self, enqueue): ...    # "job_conflict" in body; mode still switched
    def test_confirmation_409_never_enqueues(self): ...
```

Build the `JobAlreadyActive` instance the same way the existing settings-view tests do; grep `JobAlreadyActive(` in the tests to find it.

- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement the backend.**
- [ ] **Step 4: Implement the frontend:**
  - Types: the trading-mode response gains `latest_analysis`, `analysis_stale`, `job?`, `job_conflict?`; strategy settings gain `run_comparison_analysis: boolean`.
  - Trading-mode card: when `analysis_stale`, show a notice. The text is "No <mode> analysis yet" when `latest_analysis` is null, else "The <mode> ranking is older than the <other> ranking". Add a "Run analysis now" button that calls the mode mutation with `{ mode: current, rerun_analysis: true }` and shows `job_conflict` or "Analysis queued" from the response.
  - Settings card: a switch "Run comparison analysis" bound to `run_comparison_analysis`, with help text "After the active mode's analysis, also run the other mode for the comparison view." Follow the existing switch fields' form and zod pattern in that file.
- [ ] **Step 5: Run** the backend module and the full suite → PASS; `bun run typecheck` and `bun run lint` → no errors.
- [ ] **Step 6: Commit:** `git commit -m "Show stale rankings on mode switch and offer a rerun"`

---

### Task 7: ETL field, docs and README

**Files:**
- Modify: `apps/etl_api/serializers.py` (`backtest_out`, `RESOURCES`), `apps/etl_api/views/meta.py` (non-null/boolean typing if needed), `apps/etl_api/tests_contract.py` (frozen `backtest` tuple), `docs/etl-api-v1.md`, `README.md`
- Test: `apps/etl_api/tests_contract.py`, `apps/etl_api/tests_backtests.py`

**Interfaces:**
- Consumes: `AnalysisRun.drives_sessions` (Task 1).
- Produces: `drives_sessions` (bool, non-null) placed after `scope` in the backtest output.

- [ ] **Step 1: Write the failing tests.** Add `drives_sessions` after `"scope"` in the frozen `backtest` tuple in `tests_contract.py`. In `tests_backtests.py`, a run with `drives_sessions=True` serializes the field as true.
- [ ] **Step 2: Run to verify failure:** `.venv/Scripts/python.exe manage.py test apps.etl_api -v 2`.
- [ ] **Step 3: Implement** the serializer, `RESOURCES` and the schema typing (boolean, non-null).
- [ ] **Step 4: Update the docs:**
  - `docs/etl-api-v1.md`: the backtests field list gains `drives_sessions` ("true on the run that opened and retired paper sessions").
  - `README.md`: in the jobs table row for `daily-analysis`, say it runs the active trading mode's analysis first, which creates and retires paper sessions, then the other mode as a comparison when enabled. Rewrite the "Hybrid trend-gated backtest" paragraph (~208-218) to say it uses stored 1h/15m candles over the full lookback with the holdout, and drives sessions when hybrid mode is on. Delete the `ENGINE_HYBRID_LOOKBACK_DAYS` mention and the "skips the holdout" sentence (~346-347). Add `run_comparison_analysis` to the strategy settings table (default `true`).
- [ ] **Step 5: Run** the full suite → PASS.
- [ ] **Step 6: Commit:** `git commit -m "Expose drives_sessions on the ETL API and document hybrid sourcing"`

---

## Spec Coverage Map

| Spec section | Task |
|---|---|
| §3.1 mode-aware job | 4 |
| §3.2 drives_sessions gating, lookback and holdout | 3 (field: 1) |
| §3.3 stored klines, `_sync_window`, regime `sync=True`, coverage, removing `HYBRID_LOOKBACK_DAYS` | 2, 3 |
| §3.4 setting | 1 (UI: 6) |
| §3.5 mode switch | 6 |
| §3.6 current run, comparison block | 5 |
| §3.7 migration backfill, ETL field | 1, 7 |
| §4 error handling | 3, 4, 6 |
| §5 testing | each task |
| §2 the strategy-version note | 1 |
