# ETL API v1 and Strategy Versioning — Design

Date: 2026-10-30
Status: Approved design, pending spec review
Scope: Sub-projects **A** (v1 read API foundation) and **B** (strategy versioning and event capture)

## 1. Intent

An external ETL/analytics application must be able to pull the grid bot's
configuration, market, execution and backtest data over a stable, read-only,
versioned REST API, without touching the bot's database, so that it can
measure the current setup, reproduce it, and recommend better parameters from
validated out-of-sample history.

Success criteria:

- The ETL can run a full historical backfill and then incremental pulls with no
  duplicate or missed rows.
- Every record states its environment, symbol, timeframe (where relevant),
  strategy version and config version, and whether it is complete.
- Performance before and after any parameter change can be separated by version
  and by `param_change` events.
- The API cannot change bot state. Recommendations produced externally are
  never applied automatically.

### Out of scope (later sub-projects)

- **C, order lifecycle and execution quality:** placed/cancelled orders, order
  latency, per-fill slippage. Paper trading records fills only today, and there
  is no live runner.
- **D, derived analytics:** server-side windowed metrics (1h…90d, custom),
  configuration comparison, underperformance detection. v1 exposes the raw
  inputs; the ETL computes windows itself.
- Full backtest trade lists. `CoinAnalysis.trade_sample` stays a sample and is
  flagged as such.
- Bulk export / change-data-capture feeds.
- Any write endpoint.

## 2. Current state (relevant facts)

- `StrategySettings` is a singleton overwritten in place. There is no record of
  when a setting changed.
- `PaperSession.params` / `.config` hold a snapshot taken at session start;
  sessions are mutated in place on every tick.
- `PaperFill` rows are append-only. `paper_equity_points`, `market_candles`,
  `funding_rates` are raw-SQL Timescale tables (`apps/engine/timescale.py`).
- Backtests: `AnalysisRun` (has `data_cutoff`) and `CoinAnalysis`
  (`in_sample_metrics`, `oos_metrics`, `folds`, `features`, `params`,
  `trade_sample`, `data_start`, `data_end`).
- Rebalances, pauses and retirements are logged as free-form `DecisionLog` rows.
- Live: only `LiveOrderAudit` (gate attempts). No live runner exists.
- Auth: one shared `X-Engine-Token` (`apps/common/permissions.py`) that grants
  both reads and writes.
- The Docker image does not record a git SHA.

## 3. Architecture

A new Django app, **`apps/etl_api`**, mounted at **`/api/v1/`**. It has its own
serializers, authentication, throttling, pagination and OpenAPI schema, and
reads the existing models. The dashboard API under `/api/` is untouched, so
internal UI changes cannot break the external contract.

Versioning and event capture (sub-project B) live in
**`apps/engine/versioning.py`** plus new models in the engine and trading apps,
because the engine and trading code is what writes them.

```
External ETL ──HTTPS, Bearer key──▶ /api/v1/*  (apps/etl_api, GET only)
                                        │
                     reads ORM models + timescale.py helpers
                                        │
   engine/analysis.py ─┐                ▼
   trading/paper.py ───┼─▶ versioning.resolve_*() ─▶ StrategyVersion / ConfigVersion
   strategy_views.py ──┘                          ─▶ GridEvent / SessionSnapshot
```

### 3.1 Authentication and access control

Model `apps.etl_api.models.ApiClient`:

| Field | Type | Notes |
|---|---|---|
| `id` | UUID | |
| `name` | char | Human label, e.g. `etl-prod` |
| `key_prefix` | char(12), unique | First 12 chars of the key, shown in listings |
| `key_hash` | char(64) | SHA-256 hex of the full key; the key itself is never stored |
| `scopes` | JSON list | v1 defines only `read`; default `["read"]` |
| `rate_limit_per_min` | int | Default 120 |
| `created_at`, `last_used_at`, `revoked_at` | datetime | `revoked_at` set ⇒ rejected |

- Key format: `mpb_` + 40 URL-safe random characters, printed once at creation.
- Header: `Authorization: Bearer <key>`. Comparison uses
  `secrets.compare_digest` on the hash.
- Management command: `manage.py etl_api_key create --name X [--rate N]`,
  `revoke <prefix>`, `list`.
- Every v1 view allows GET/HEAD/OPTIONS only; other methods return 405. No
  write scope exists in v1.
- The legacy `X-Engine-Token` is **not** accepted on `/api/v1/`.
- `ENGINE_ALLOW_PUBLIC_READS` does **not** apply to `/api/v1/`; a key is always
  required.

### 3.2 Rate limiting

A DRF throttle keyed by `ApiClient.id`, limit = `rate_limit_per_min`, counted
in a fixed one-minute window stored in the database (`RateWindow`), so the limit
holds across gunicorn workers. (The default Django cache is per-process
LocMem in this project.) Every response carries `X-RateLimit-Limit`,
`X-RateLimit-Remaining`, `X-RateLimit-Reset` (epoch seconds). When the limit is
exceeded: 429 with `Retry-After`.

### 3.3 Response envelope

All successful list responses:

```json
{
  "data": [ ... ],
  "meta": {
    "api_version": "1",
    "generated_at": "2026-10-30T12:00:00Z",
    "next_cursor": "eyJ0IjoiMjAyNi0xMC0zMFQxMTo1OTo1NVoiLCJpZCI6MTIzfQ",
    "watermark": "2026-10-30T11:59:55Z",
    "page_size": 500
  },
  "completeness": { ... endpoint-specific, see §4 ... }
}
```

Detail responses use the same shape with `data` as an object and no cursor.

### 3.4 Pagination and incremental retrieval

- Cursor pagination only (opaque base64 JSON of the ordering key, plus `id`
  for ties). `next_cursor` is `null` on the last page.
- `page_size` defaults to 500, max 1000 (candles: max 5000).
- `updated_since` (or the endpoint's own incremental key, listed in §4) returns
  rows whose key is `>= updated_since`, ordered ascending.
- Rows whose key is later than `now − 5s` are held back from the response, so
  rows from in-flight transactions are never skipped.
- `meta.watermark` = the largest key returned so far in this cursor walk, or
  the request's `updated_since` if no row was returned. The ETL stores the
  watermark from the last page and sends it back as `updated_since` next time.
  Because `updated_since` is inclusive, the boundary row can come back twice;
  the ETL deduplicates on `id`, and the docs say so.
- Mutable state (sessions) is incremental through `SessionSnapshot` history
  (§5.3), so each state change is retrievable, not only the latest.

### 3.5 Common query parameters

| Param | Applies to | Notes |
|---|---|---|
| `symbol` | most | Binance symbol, e.g. `BTCUSDT`; repeatable |
| `timeframe` | market, backtests | `1h`, `4h`, … (validated against stored intervals) |
| `start`, `end` | time-series | UTC ISO-8601; `start <= end`; `end` is inclusive |
| `updated_since` | lists | see §3.4 |
| `environment` | backtests, sessions, fills, events | `backtest`, `walk_forward`, `paper`, `live` |
| `strategy_version`, `config_version` | versioned resources | hash id |
| `session` | fills, equity, events | session UUID |
| `cursor`, `page_size` | lists | |

The maximum window per request is 400 days for market data. A larger window
returns 400 `window_too_large`; the client paginates by time instead.

### 3.6 Errors

`Content-Type: application/problem+json` (RFC 7807):

```json
{
  "type": "https://marketaplusb/errors/invalid_parameter",
  "title": "Invalid parameter",
  "status": 400,
  "detail": "start must be before end",
  "instance": "/api/v1/market/candles/",
  "errors": [{"field": "start", "code": "after_end"}]
}
```

| Status | `type` slug | When |
|---|---|---|
| 400 | `invalid_parameter` | bad date, unknown symbol or timeframe, `start > end` |
| 400 | `window_too_large` | time window exceeds the endpoint max |
| 401 | `unauthenticated` | missing or malformed key |
| 403 | `revoked_key` | key revoked |
| 403 | `insufficient_scope` | key lacks `read` |
| 404 | `not_found` | |
| 405 | `method_not_allowed` | any write method |
| 410 | `cursor_expired` | cursor references a schema/version no longer valid |
| 429 | `rate_limited` | throttled; `Retry-After` set |

### 3.7 Timestamps and look-ahead protection

- All timestamps are UTC, serialised as ISO-8601 with `Z`.
- Candles expose both `open_time` and `close_time`; a candle is returned only if
  `close_time <= end` (no partially formed bars).
- Backtests expose `data_cutoff` and `holdout_start`; folds expose
  `train_start/train_end/test_start/test_end`. The API guarantees
  `test_start >= train_end` in every returned fold, and the test suite enforces
  it.
- `market/features/` values are stamped `as_of = data_cutoff` of the analysis
  that computed them: they were computed only from data up to that instant.

## 4. Endpoints

All under `/api/v1/`, GET only.

| Endpoint | Source | Incremental key |
|---|---|---|
| `strategy-versions/`, `strategy-versions/{id}/` | `StrategyVersion` | `first_seen_at` |
| `config-versions/`, `config-versions/{id}/` | `ConfigVersion` | `first_seen_at` |
| `market/candles/` (requires `symbol`, `timeframe`) | `market_candles` | `open_time` |
| `market/funding/` (requires `symbol`) | `funding_rates` | `funding_time` |
| `market/features/` | `CoinAnalysis.features` | `as_of` |
| `backtests/`, `backtests/{id}/` | `AnalysisRun` + `CoinAnalysis` | `created_at` |
| `backtests/{id}/folds/` | `CoinAnalysis.folds` | — |
| `sessions/`, `sessions/{id}/` | `PaperSession` | `updated_at` |
| `sessions/{id}/snapshots/` | `SessionSnapshot` | `time` |
| `sessions/{id}/equity/` | `paper_equity_points` | `time` |
| `sessions/{id}/evaluations/` | `PaperEvaluation` | `evaluated_at` |
| `fills/` (and `sessions/{id}/fills/`) | `PaperFill` | `(time, id)` |
| `events/` | `GridEvent` | `(time, id)` |
| `live/order-audit/` | `LiveOrderAudit` | `created_at` |
| `meta/schema/` | DRF OpenAPI 3 | — |
| `meta/health/` | data freshness per source | — |

### 4.1 Field definitions

**StrategyVersion**

| Field | Type | Definition |
|---|---|---|
| `id` | string(64) | SHA-256 of the canonical JSON of `snapshot` + `code_sha` |
| `snapshot` | object | Effective global config: `signal_interval`, `fill_model`, `trade_through_bps`, `overbought_guard_enabled`, `holdout_days`, portfolio limits (`book_capital`, `max_sessions`, `max_book_exposure`, `daily_loss_limit_pct`, `retire_after_failed_runs`, `max_strategy_correlation`), and the engine settings that affect results (`LOOKBACK_DAYS`, `INTERVAL`, `LEVERAGE`, `MAKER_FEE_RATE`, `TAKER_FEE_RATE`, `SLIPPAGE_BPS`, and the remaining `ENGINE` keys that feed the simulator) |
| `code_sha` | string | Git SHA of the running build, or `"unknown"` |
| `parent_id` | string \| null | Previous version in time order |
| `diff` | object | `{field: {"from": x, "to": y}}` against `parent_id` |
| `first_seen_at` | datetime | First time this version was resolved |
| `backfilled` | bool | Created by the backfill migration |

`completeness`: `{"code_sha_known": bool}`.

**ConfigVersion**

| Field | Type | Definition |
|---|---|---|
| `id` | string(64) | SHA-256 of canonical grid params |
| `symbol` | string | |
| `params` | object | `grid_levels`, `spacing`, `range_low`, `range_high`, `rebalance_mode`, `stop_atr_multiple`, `tp_atr_multiple`, `leverage`, plus any other tuned key present |
| `first_seen_at` | datetime | |
| `backfilled` | bool | |

**Candle**: `symbol`, `timeframe`, `open_time`, `close_time`, `open`, `high`,
`low`, `close`, `volume`, `quote_volume`, `trades`.
`completeness`: `{"expected_bars", "returned_bars", "gaps": [{"from","to"}]}`
for the requested window.

**Funding**: `symbol`, `funding_time`, `rate`, `mark_price`.

**Features**: `symbol`, `timeframe`, `as_of`, `analysis_id`,
`strategy_version`, and the computed keys from `features.py` (ATR, ADX, Hurst
exponent, variance ratio, range containment, trend slope/fit, max drawdown,
regime). `null` when the engine flagged `insufficient_data`.

**MetricsBlock** (shared by backtests, folds, evaluations): `net_return`,
`annualized_return`, `max_drawdown`, `max_drawdown_duration_days`, `sharpe`,
`sortino`, `calmar`, `profit_factor`, `win_rate`, `trades`, `closed_lots`,
`fees_paid`, `funding_paid`, `slippage_paid`, `avg_exposure_ratio`,
`max_exposure_ratio`, `time_in_range`, `rebalances`, `cost_stress_return`.
A metric the engine did not compute is `null`, never omitted.

**Backtest** (one per `CoinAnalysis`): `id`, `run_id`, `environment`
(`backtest`), `symbol`, `timeframe`, `trigger`, `mode`, `scope`,
`strategy_version`, `config_version`, `data_start`, `data_end`,
`data_cutoff`, `holdout_start`, `in_sample` (MetricsBlock),
`out_of_sample` (MetricsBlock), `score`, `rank`, `suitability`, `eligible`,
`exclusion_reason`, `component_scores`, `trade_sample`, `created_at`.
`completeness`: `{"is_partial": run.status == "running", "run_status",
"trade_sample_is_complete": false}`.

**Fold**: `environment` (`walk_forward`), `fold`, `train_start`,
`train_end`, `test_start`, `test_end`, `params`, `train_metrics`,
`test_metrics` (MetricsBlock), `regime`.

**Session**: `id`, `environment` (`paper`; `live` reserved), `symbol`,
`status`, `bias`, `strategy_version`, `config_version`, `capital`,
`leverage`, `position_qty`, `avg_entry`, `mark_price`, `equity`,
`peak_equity`, `max_drawdown`, `realized_pnl`, `unrealized_pnl`
(`position_qty × (mark_price − avg_entry)`), `fees_paid`, `funding_paid`,
`carry_cost`, `fill_count`, `closed_lots`, `wins`, `losses`, `net_return`,
`grid` (current range, levels, spacing from `grid_state`), `started_at`,
`last_tick_at`, `updated_at`, `stop_reason`.
`completeness`: `{"is_partial": status == "active"}`.

**SessionSnapshot**: `session_id`, `time`, `trigger`
(`start`/`rebalance`/`param_change`/`pause`/`resume`/`stop`/`daily`), plus the
session state fields above as they were at `time`.

**Equity point**: `time`, `equity`, `price`, `position_qty`, `drawdown`.

**Fill**: `id`, `session_id`, `environment`, `symbol`, `time`, `side`,
`price`, `qty`, `notional`, `fee`, `realized_pnl`, `level`, `reason`,
`strategy_version`, `config_version`.

**GridEvent**: `id`, `time`, `type` (`rebalance`, `recentre`, `pause`,
`resume`, `param_change`, `retire`, `stop`, `kill_switch`), `environment`,
`symbol`, `session_id`, `reason`, `before`, `after` (range, levels, params,
or changed settings), `strategy_version`, `config_version`, `backfilled`.

**Live order audit**: `created_at`, `symbol`, `side`, `order_type`, `qty`,
`price`, `notional`, `allowed`, `reason`. The raw exchange `response` is
excluded.

**Health** (`meta/health/`): per source, the latest timestamp and lag in
seconds (`market_candles` per interval, `funding_rates`, latest analysis, latest
paper tick), plus the current `strategy_version`.

## 5. Versioning and event capture (sub-project B)

### 5.1 Models

- `apps.engine.models.StrategyVersion` and `ConfigVersion`: fields as in §4.1.
  Append-only: no update or delete paths in code.
- `apps.trading.models.GridEvent`: as in §4.1, indexed on `(time, id)` and
  `(session, time)`.
- `apps.trading.models.SessionSnapshot`: `session` FK, `time`, `trigger`,
  `state` JSON, indexed on `(session, time)`.
- New nullable FKs: `AnalysisRun.strategy_version`,
  `CoinAnalysis.config_version`, `PaperSession.strategy_version`,
  `PaperSession.config_version`, `PaperFill.strategy_version`,
  `PaperFill.config_version` (denormalised so fill queries need no join).

### 5.2 Canonical hashing

`versioning.canonical_json(obj)`: keys sorted, floats rounded to 10
significant digits, no whitespace, UTF-8. `resolve_strategy_version()` and
`resolve_config_version(symbol, params)` hash it and `get_or_create`
(idempotent, safe under concurrent callers via the unique primary key).
`code_sha` comes from env `GIT_SHA`, defaulting to `"unknown"`.

### 5.3 Capture points

| Location | Action |
|---|---|
| `apps/trading/strategy_views.py` settings save | resolve strategy version; if it changed, write a `param_change` GridEvent with before/after `diff` |
| `apps/engine/analysis.py` `AnalysisRun.objects.create` | stamp `strategy_version`; each `CoinAnalysis` gets `config_version` from its tuned params |
| `apps/trading/paper.py` `PaperSession.objects.create` | stamp both versions; write `SessionSnapshot(trigger="start")` |
| `apps/trading/paper.py` fill `bulk_create` | stamp the session's current versions on each fill |
| `apps/trading/paper.py` rebalance / pause / resume / stop / retire, next to the existing `log_decision` calls | write the GridEvent and a SessionSnapshot |
| `apps/trading/execution.py` kill-switch toggle | write a `kill_switch` GridEvent |
| Daily scheduler job | `SessionSnapshot(trigger="daily")` for each active session |

The engine and trading behaviour does not change: capture is additive writes
only.

### 5.4 Build metadata

`backend/Dockerfile` gains `ARG GIT_SHA` → `ENV GIT_SHA`; `compose.yaml` passes
it. `DEPLOYMENT.md` documents
`docker compose build --build-arg GIT_SHA=$(git rev-parse HEAD)`.

### 5.5 Backfill migration

A data migration:

- Resolves a `ConfigVersion` for every existing `CoinAnalysis` and `PaperSession`
  from its stored `params`/`config`, and a `StrategyVersion` from each
  session's `config` snapshot where present (else the current settings), with
  `code_sha="unknown"` and `backfilled=true`.
- Stamps existing fills from their session.
- Converts past `DecisionLog` rebalance/pause/retire rows into `GridEvent`
  only where the payload carries the needed fields, with `backfilled=true`.
  Nothing is invented.
- Writes one `SessionSnapshot(trigger="start")` per existing session from its
  current state, flagged `backfilled`.

## 6. Documentation deliverables

- OpenAPI 3 schema served at `/api/v1/meta/schema/`, built from the v1 route
  and field registry (no new dependency).
- `docs/etl-api-v1.md`: authentication and key management, envelope,
  pagination and the watermark algorithm, error catalogue, every endpoint with
  its fields, and worked curl examples for (1) a historical backfill of
  candles, backtests and fills, (2) an incremental pull loop, and (3)
  before/after analysis of a `param_change` event.

## 7. Testing

New `apps/etl_api/tests.py` (Django `TestCase`, matching the existing suites),
plus versioning tests in `apps/engine/tests.py` / `apps/trading/tests.py`:

- **Auth:** missing key → 401; revoked → 403; no `read` scope → 403;
  POST/PUT/PATCH/DELETE → 405; key never stored in plaintext; legacy
  `X-Engine-Token` rejected on v1.
- **Throttling:** limit+1 requests → 429 with `Retry-After`; rate-limit headers
  present.
- **Pagination:** cursor walk returns every row exactly once, including ties on
  the timestamp; rows inserted mid-walk don't cause duplicates or skips;
  `updated_since` round-trip via `watermark` covers all rows.
- **Envelope and errors:** each endpoint returns `data/meta/completeness`;
  errors are `problem+json` with the documented `type`.
- **Contract snapshots:** each v1 serializer's field set is asserted against a
  frozen list, so removing or renaming a field fails the build.
- **Versioning:** identical config → same hash; float/ordering noise → same
  hash; changing one setting → new version + `param_change` event with the
  right diff; fills inherit the session's versions; `code_sha` missing → `unknown`.
- **Look-ahead:** candles never past `end` and never partial; every returned
  fold has `test_start >= train_end`; features carry `as_of <= data_cutoff`.
- **Completeness:** a gap in seeded candles appears in `gaps`; running analysis
  → `is_partial: true`.
- **Backfill migration:** run against seeded legacy rows; versions and
  `backfilled` flags set; no GridEvent invented from incomplete payloads.

## 8. Extensibility

- New resources are added under `/api/v1/` without changing existing schemas;
  breaking changes go to `/api/v2/`.
- Sub-project C adds `orders/` and latency fields to `fills/` as new optional
  fields.
- Sub-project D adds `analytics/` endpoints that compute only from data with
  timestamps `<=` the request's `as_of`.
- Scopes are a list, so a future `recommend:write` scope (submit recommendations
  for human review, never auto-applied) can be added without changing the key
  model.
