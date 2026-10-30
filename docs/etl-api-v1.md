# ETL API v1

A read-only REST API for external ETL and analytics tools. It exposes the grid bot's versions,
market data, backtests, paper sessions, fills, events and live-gate audit under `/api/v1/`,
separate from the dashboard API under `/api/`. v1 has no write endpoints: recommendations produced
from this data are never applied automatically.

The machine-readable contract is served at `GET /api/v1/meta/schema/` (OpenAPI 3.0.3, key
required, returned as a bare document rather than the envelope).

## 1. Keys and authentication

Keys are managed on the server:

```bash
cd backend
python manage.py etl_api_key create --name etl-prod [--rate 120]   # key printed once
python manage.py etl_api_key list
python manage.py etl_api_key revoke mpb_AbCdEfGh    # the 12-character prefix shown by list/create
```

A key looks like `mpb_` plus 40 random characters. Only its SHA-256 hash is stored, so a lost key
cannot be recovered; create a new one and revoke the old. Send it on every request:

```
Authorization: Bearer mpb_...
```

- Missing, malformed or unknown key: 401 `unauthenticated`. Revoked key: 403 `revoked_key`. A key
  without the `read` scope (every key has it by default): 403 `insufficient_scope`.
- The dashboard `X-Engine-Token` is not accepted on `/api/v1/`, and `ENGINE_ALLOW_PUBLIC_READS`
  does not apply: a key is always required.
- Only GET, HEAD and OPTIONS are allowed; any other method returns 405 `method_not_allowed`.

## 2. Rate limits

Each key has a per-minute limit (default 120, set with `--rate`), counted in a fixed one-minute
window in the database so it holds across workers. Every response carries `X-RateLimit-Limit`,
`X-RateLimit-Remaining` and `X-RateLimit-Reset` (epoch seconds). Over the limit the API returns
429 `rate_limited` with a `Retry-After` header (seconds).

## 3. Envelope

Every successful JSON response, list or detail, has the same shape:

```json
{
  "data": [],
  "meta": {
    "api_version": "1",
    "generated_at": "2026-10-30T12:00:00Z",
    "next_cursor": "...",
    "watermark": "2026-10-30T11:59:55Z",
    "page_size": 500
  },
  "completeness": {}
}
```

`data` is an array for lists and an object for detail routes. `next_cursor`, `watermark` and
`page_size` appear on paged lists only. `completeness` is endpoint specific (section 7). Folds
(`backtests/{id}/folds/`) and health return an envelope with a bare `meta` and an empty
`completeness`.

All timestamps are UTC ISO-8601 with a `Z` suffix.

## 4. Pagination, watermarks and incremental pulls

Lists are cursor-paged in ascending order of the endpoint's incremental key (section 6), with the
row's primary key (or, for candles, funding and equity, the time itself) as tie-breaker.

Parameters: `cursor` (opaque, from `meta.next_cursor`), `page_size` (default 500, max 1000;
candles max 5000), `updated_since`, `start`, `end`. `updated_since` and `start` are inclusive
lower bounds on the incremental key, `end` an inclusive upper bound. A date-only `end` such as
`2026-10-01` means midnight UTC, so that day is excluded.

Hold-back: list endpoints never return rows whose key is newer than `now - 5 s`, so rows from
in-flight transactions cannot be skipped. This applies to list (incremental) endpoints only.
Detail-by-id routes (`backtests/{id}/`, folds, version detail and session detail) can return rows
newer than 5 s.

Rows whose incremental key is NULL are excluded from list endpoints.

Watermark algorithm:

1. First run: call the list with no `updated_since` (or your backfill start) and follow
   `next_cursor` until it is `null`.
2. `meta.watermark` is the key of the last row on the final page; if the walk returned no rows it
   is the `updated_since` you sent. Store it.
3. Next run: pass the stored value as `updated_since` and walk again.
4. `updated_since` is inclusive (`>=`), so the boundary row comes back again. Deduplicate on `id`
   (on `open_time`, `funding_time` or `time` for candles, funding and equity points, which have no
   `id`).

Fills and session fills page on `created_at` (insertion time), not `time`. A fill's `time` is the
strategy's bar time and can be up to one signal interval older than the insert, so paging on it
would let a watermark skip late-inserted rows. Fills expose both `time` and `created_at`; use
`created_at` as the watermark and `time` for analysis.

Equity points (`time`) and evaluations (`evaluated_at`) are stamped with the time captured once
at the start of the paper tick, not at insert, so a row can land a little after a newer-looking
watermark was handed out. Re-pull these two with a small overlap, e.g.
`updated_since = watermark - 15 minutes`, and deduplicate on (`session_id`, `time`) for equity
and on `id` for evaluations.

A cursor is bound to the endpoint that issued it. Reusing it elsewhere gives 410 `cursor_expired`;
a malformed cursor gives 400.

## 5. Errors

Errors are `application/problem+json` (RFC 7807):

```json
{
  "type": "https://marketaplusb/errors/invalid_parameter",
  "title": "Invalid parameter",
  "status": 400,
  "detail": "'start' must not be after 'end'.",
  "instance": "/api/v1/market/candles/",
  "errors": [{"field": "start", "code": "after_end"}]
}
```

| Status | `type` slug | When |
|---|---|---|
| 400 | `invalid_parameter` | bad date, unknown candle timeframe or environment, missing required parameter, `start` after `end`, bad cursor or UUID, `page_size` out of range, NUL bytes in a parameter |
| 400 | `window_too_large` | market-data window over 400 days |
| 401 | `unauthenticated` | missing or malformed key |
| 403 | `revoked_key` | key revoked |
| 403 | `insufficient_scope` | key lacks `read` |
| 404 | `not_found` | unknown id, unknown `/api/v1/` path, or an integer id above 2^63-1 |
| 405 | `method_not_allowed` | any write method |
| 410 | `cursor_expired` | cursor issued for a different endpoint |
| 429 | `rate_limited` | throttled; `Retry-After` set |

## 6. Endpoints

All GET, all under `/api/v1/`. Every URL ends with a trailing slash (`market/candles/`, not
`market/candles`); without it you get 404 `not_found`. "Key" is the incremental key used for `updated_since`, `start`,
`end` and the watermark. Common list parameters are `cursor`, `page_size`, `updated_since`,
`start`, `end`; each row below lists only the extra filters.

| Endpoint | Key | Extra filters |
|---|---|---|
| `strategy-versions/`, `strategy-versions/{id}/` | `first_seen_at` | |
| `config-versions/`, `config-versions/{id}/` | `first_seen_at` | `symbol` |
| `market/candles/` | `open_time` | `symbol` and `timeframe` (both required, exactly one symbol) |
| `market/funding/` | `funding_time` | `symbol` (required, exactly one) |
| `market/features/` | analysis `finished_at` | `symbol`, `timeframe` |
| `backtests/`, `backtests/{id}/` | `created_at` | `symbol`, `timeframe`, `environment`, `strategy_version`, `config_version` |
| `backtests/{id}/folds/` | none (not paged) | |
| `sessions/` | `updated_at` | `symbol`, `environment`, `strategy_version`, `config_version`, `session` |
| `sessions/{id}/` | | |
| `sessions/{id}/snapshots/` | `time` | `environment` |
| `sessions/{id}/equity/` | `time` | `environment` |
| `sessions/{id}/evaluations/` | `evaluated_at` | `environment` |
| `sessions/{id}/fills/` | `created_at` | `symbol`, `environment`, `strategy_version`, `config_version` |
| `fills/` | `created_at` | the same, plus `session` |
| `events/` | `time` | `type` (repeatable), `environment`, `symbol`, `session`, `strategy_version`, `config_version` |
| `live/order-audit/` | `created_at` | `symbol` |
| `meta/health/` | | |
| `meta/schema/` | | |

Filter notes: `symbol` is a Binance symbol such as `BTCUSDT` and may be repeated where more than
one is accepted. `timeframe` is a Binance interval (`1m` ... `1d`; candles reject unknown values with 400, other endpoints just filter). On
`backtests/`, `start` and `end` bound `data_end`, not `created_at`, and `timeframe` matches the
run's interval. Market data (`candles`, `funding`) rejects a window over 400 days with 400
`window_too_large`; with only `start`, the window runs to now. Event `type` is one of `rebalance`,
`recentre`, `pause`, `resume`, `param_change`, `retire`, `stop`, `kill_switch`.

### Fields

Field names are stable; fields may be added within v1 but are never removed or renamed. Unknown
metrics are `null`, never omitted.

**StrategyVersion**: `id` (SHA-256 of the canonical snapshot plus `code_sha`), `snapshot` (the
effective global settings and engine parameters), `code_sha` (`"unknown"` when the build did not
record a git SHA), `parent_id`, `diff` (`{field: {"from": x, "to": y}}` against the parent),
`first_seen_at`, `backfilled`. Completeness: list `{"code_sha_unknown_ids": [...]}`, detail
`{"code_sha_known": bool}`.

**ConfigVersion**: `id` (SHA-256 of the canonical grid params), `symbol`, `params`,
`first_seen_at`, `backfilled`.

**Candle**: `symbol`, `timeframe`, `open_time`, `close_time`, `open`, `high`, `low`, `close`,
`volume`, `quote_volume`, `trades`. Completeness: `{"expected_bars", "returned_bars", "gaps":
[{"from", "to"}]}`. These are computed within the returned page only; a gap that spans a page
boundary is not reported, so check across pages yourself if you need it.

**Funding**: `symbol`, `funding_time`, `rate`, `mark_price`.

**Features**: `symbol`, `timeframe`, `as_of`, `finished_at`, `analysis_id`,
`strategy_version`, `features`. Returns the features of every succeeded analysis (all history).
`as_of` is that analysis's `data_cutoff` (the newest candle it saw); `finished_at` is when the
run succeeded and its rows became visible, and is the incremental key. A run can analyse old
data and finish much later, so page and watermark on `finished_at`, never `as_of`. `features` is
`null` when the analysis flagged insufficient data.

**MetricsBlock** (used by backtests, folds, evaluations): `net_return`, `annualized_return`,
`max_drawdown`, `max_drawdown_duration_days`, `sharpe`, `sortino`, `calmar`, `profit_factor`,
`win_rate`, `trades`, `closed_lots`, `fees_paid`, `funding_paid`, `slippage_paid`,
`avg_exposure_ratio`, `max_exposure_ratio`, `time_in_range`, `rebalances`, `cost_stress_return`.

**Backtest** (one per coin analysis): `id`, `run_id`, `environment` (`backtest`), `symbol`,
`timeframe`, `trigger`, `mode`, `scope`, `strategy_version`, `config_version`, `data_start`,
`data_end`, `data_cutoff`, `holdout_start`, `in_sample`, `out_of_sample` (MetricsBlocks), `score`,
`rank`, `suitability`, `eligible`, `exclusion_reason`, `component_scores`, `trade_sample`,
`created_at`. `trade_sample` is a sample, not the full trade list. Completeness: detail
`{"is_partial", "run_status", "trade_sample_is_complete": false}`; list
`{"partial_run_ids", "trade_sample_is_complete": false}`, where `partial_run_ids` covers only the
current page. Filtering `environment=walk_forward` (or any value other than `backtest`) returns an
empty list; folds are served by the folds route.

**Fold**: `environment` (`walk_forward`), `fold`, `train_start`, `train_end`, `test_start`,
`test_end`, `params`, `train_metrics`, `test_metrics`, `regime`. Every returned fold has
`test_start >= train_end`; malformed or overlapping folds are dropped.

**Session**: `id`, `environment` (`paper`), `symbol`, `strategy_version`, `config_version`,
`started_at`, `last_tick_at`, `updated_at`, `status`, `bias`, `capital`, `leverage`,
`position_qty`, `avg_entry`, `mark_price`, `equity`, `peak_equity`, `max_drawdown`,
`realized_pnl`, `unrealized_pnl` (`position_qty x (mark_price - avg_entry)`), `fees_paid`,
`funding_paid`, `carry_cost`, `fill_count`, `closed_lots`, `wins`, `losses`, `net_return`, `grid`,
`stop_reason`. `grid` is `{range_low, range_high, levels, spacing}`; the strategy stores levels,
not spacing, so `spacing` is derived as the mean relative gap between adjacent sorted levels (a
fraction, e.g. 0.01 is 1 percent). Completeness: detail `{"is_partial": status == "active"}`;
list `{"active_ids": [...]}` for the current page only. Sessions are mutated in place, so use
snapshots for history.

**SessionSnapshot**: `id`, `session_id`, `time`, `trigger` (`start`, `rebalance`, `pause`,
`resume`, `stop`, `retire`, `daily`), `state` (the session state as it was at `time`, as a nested
object), `strategy_version`, `config_version`, `backfilled`.

**Equity point**: `time`, `equity`, `price`, `position_qty`, `drawdown`.

**Fill**: `id`, `session_id`, `environment`, `symbol`, `time` (strategy bar time), `created_at`
(insertion time, the paging key), `side`, `price`, `qty`, `notional`, `fee`, `realized_pnl`,
`level`, `reason`, `strategy_version`, `config_version`.

**Evaluation**: `id`, `session_id`, `evaluated_at`, `passed`, `metrics` (MetricsBlock),
`failures`, `thresholds`.

**GridEvent**: `id`, `time`, `type`, `environment`, `symbol`, `session_id`, `reason`, `before`,
`after`, `strategy_version`, `config_version`, `backfilled`. A `param_change` event's `before` and
`after` hold the changed settings; `strategy_version` is the version in force after the change.
`kill_switch` events carry environment `live`. A `regime` event (symbol `BTCUSDT`, no session) is a
BTC market regime phase change: `before.phase`, and `after.phase`, `after.reasons` and
`after.metrics`; sessions it flattened or resumed get `pause`/`resume` events with reason
`btc_regime`.

**Live order audit**: `id`, `created_at`, `symbol`, `side`, `order_type`, `qty`, `price`, `notional`,
`allowed`, `reason`. It holds gate attempts only (there is no live runner), and the raw exchange
response is never exposed.

**Health** (`meta/health/`): `market_candles` (per interval, `{latest, lag_seconds}`),
`funding_rates`, `analysis_run`, `paper_session_tick` (each `{latest, lag_seconds}`) and
`current_strategy_version`.

## 7. Environments

`environment` takes `backtest`, `walk_forward`, `paper` or `live`.

- `backtests/` serves `backtest` rows (any other value gives an empty list); walk-forward folds
  come from `backtests/{id}/folds/` with `environment: "walk_forward"`.
- Session-scoped endpoints (`sessions/`, snapshots, equity, evaluations, fills) serve `paper`. With
  `environment=live` they return empty `data` with completeness `{"is_partial": true, "note": "no
  live runner"}`; with `backtest` or `walk_forward` they return empty `data`.
- `events/` filters on the stored environment: `paper` events, and `kill_switch` events with
  environment `live`.
- There is no live runner, so there are no live sessions, fills or equity.

## 8. Look-ahead guarantees

- All timestamps are UTC.
- A candle is returned only once fully closed: `close_time <= min(end, now - 5 s)`. Candles expose
  both `open_time` and `close_time`.
- Backtests expose `data_cutoff` and `holdout_start`; every fold has `test_start >= train_end`.
- Features are stamped `as_of = data_cutoff` of the analysis that computed them, so they were
  computed only from data up to that instant.

## 9. Provenance and backfilled data

Every strategy-affecting setting set and every tuned grid parameter set is a content-addressed
version (`strategy_version`, `config_version`) referenced by backtests, sessions, fills, snapshots
and events. Rows created by the one-time backfill migration carry `backfilled: true`:

- Versions were reconstructed from stored params with `code_sha` `"unknown"`.
- The migration did not create `retire` events (legacy retire logs lack a reason). It created
  `rebalance`, `pause` and `resume` events only where the old logs held complete data; nothing was
  invented.
- One `start` snapshot per existing session was written from its state at migration time.

## 10. Walkthroughs

Set the base values once:

```bash
export API=https://your-host/api/v1
export KEY=mpb_...
auth() { curl -sS -H "Authorization: Bearer $KEY" "$@"; }
```

### 10.1 Historical backfill: candles, backtests, fills

Candles need a window of at most 400 days per request and are paged by cursor (max 5000 rows).

```bash
# Candles: walk one window, following next_cursor until it is null.
url="$API/market/candles/?symbol=BTCUSDT&timeframe=1h&start=2026-01-01T00:00:00Z&end=2026-10-01T00:00:00Z&page_size=5000"
while [ -n "$url" ]; do
  page=$(auth "$url")
  echo "$page" | jq -c '.data[]' >> candles.ndjson
  cursor=$(echo "$page" | jq -r '.meta.next_cursor // empty')
  url=""
  [ -n "$cursor" ] && url="$API/market/candles/?symbol=BTCUSDT&timeframe=1h&start=2026-01-01T00:00:00Z&end=2026-10-01T00:00:00Z&page_size=5000&cursor=$cursor"
done
# Gaps inside each page are in .completeness.gaps; a gap across a page boundary is not reported.

# Backtests (all history), then each one's folds.
auth "$API/backtests/?symbol=BTCUSDT&timeframe=1h&page_size=1000" | jq -c '.data[]' > backtests.ndjson
for id in $(jq -r '.id' backtests.ndjson); do
  auth "$API/backtests/$id/folds/" | jq -c '.data[]' >> folds.ndjson
done

# Fills across all sessions, paged on created_at: follow next_cursor to the end.
base="$API/fills/?page_size=1000"
url="$base"
while [ -n "$url" ]; do
  page=$(auth "$url")
  echo "$page" | jq -c '.data[]' >> fills.ndjson
  last=$(echo "$page" | jq -r '.meta.watermark // empty')
  cursor=$(echo "$page" | jq -r '.meta.next_cursor // empty')
  url=""
  [ -n "$cursor" ] && url="$base&cursor=$cursor"
done
# Only the final page's watermark counts; skip the file if nothing came back.
[ -n "$last" ] && echo "$last" > fills.watermark
```

The watermark from the last page is your starting point for the incremental loop. If no watermark
file exists yet, the incremental loop simply does a full pull.

### 10.2 Incremental loop

```bash
WM=$(cat fills.watermark 2>/dev/null || true)  # missing file: empty, so a full pull
[ "$WM" = "null" ] && WM=""                    # never send a literal "null"
url="$API/fills/?page_size=1000${WM:+&updated_since=$WM}"
while [ -n "$url" ]; do
  page=$(auth "$url")
  echo "$page" | jq -c '.data[]' >> new_fills.ndjson
  last=$(echo "$page" | jq -r '.meta.watermark // empty')
  cursor=$(echo "$page" | jq -r '.meta.next_cursor // empty')
  url=""
  [ -n "$cursor" ] && url="$API/fills/?page_size=1000${WM:+&updated_since=$WM}&cursor=$cursor"
done
[ -n "$last" ] && echo "$last" > fills.watermark
# Inclusive updated_since repeats the boundary row: load with an upsert keyed on .id
# (or: jq -s 'unique_by(.id)' new_fills.ndjson).
```

Run it on a schedule (for example every minute). Rows newer than 5 seconds are held back and show
up on a later pull, never skipped. Use the same pattern with `events/` (key `time`) and
`sessions/` (key `updated_at`).

### 10.3 Before and after a `param_change`

```bash
# 1. Find the change.
auth "$API/events/?type=param_change&page_size=100" | jq '.data[] | {id, time, before, after, strategy_version}'
# Say event 42 happened at 2026-10-12T08:00:00Z, and its .strategy_version is the version after.

# 2. The version before it is that version's parent, and .diff says what changed.
auth "$API/strategy-versions/<after_version_id>/" | jq '.data | {parent_id, diff}'

# 3. Compare fills before and after, split by strategy_version rather than by time alone.
for v in <parent_id> <after_version_id>; do
  auth "$API/fills/?strategy_version=$v&page_size=1000" \
    | jq --arg v "$v" '{version: $v, fills: (.data | length), realized_pnl: ([.data[].realized_pnl] | add // 0)}'
done

# 4. Equity around the change for one session (the event's .session_id).
auth "$API/sessions/<session_id>/equity/?start=2026-10-11T08:00:00Z&end=2026-10-13T08:00:00Z" \
  | jq -c '.data[]'

# 5. Snapshots show the full session state at the change.
auth "$API/sessions/<session_id>/snapshots/?start=2026-10-12T00:00:00Z&end=2026-10-13T00:00:00Z" \
  | jq '.data[] | {time, trigger, state}'
```

Page through each list as in 10.1 when a window exceeds one page. Fills carry both versions
(`strategy_version`, `config_version`) so attribution survives later changes.
