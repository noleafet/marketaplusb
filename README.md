# Marketa+b - marketa+ bot

A staged grid-trading research and paper-trading system. A Django engine ingests the monthly
top-100 crypto universe, backtests a long-only grid strategy against Binance USDⓈ-M public
market data, ranks coins by risk-adjusted suitability, runs paper trading on live data, and
exposes every decision through a Next.js dashboard. Live trading is implemented as a guarded,
separately isolated adapter and is **disabled by default**.

> This is research tooling, not investment advice. Backtest and paper results do not imply
> future profitability.

## Stack

- **Backend** — Django 5.2, Django REST Framework, psycopg 3, numpy, APScheduler.
- **Database** — PostgreSQL 17, run through Podman locally. TimescaleDB is optional and
  disabled by default, so the app also runs on managed Postgres (Neon, Render, RDS).
- **Frontend** — Next.js 16 (App Router), React 19, shadcn/Base UI, TanStack Query, Recharts.
- **Queue** — a database-backed `JobRun` queue. No Redis or Celery is required.

## Architecture

```
CoinGecko top-100 ──► coins_refresh (monthly) ──► Coin + market snapshot
                                                       │
Binance klines/funding ──► daily_analysis (00:10 UTC) ─┤
                                                       ├─► walk-forward backtests
                                                       ├─► cross-sectional scoring
                                                       ├─► reports + decision log
                                                       └─► paper sessions (top N)

Binance live public data ──► paper_tick (every 5 min) ──► simulated fills + equity
                              paper_evaluate (6 h)     ──► promotion threshold check
                                                            │
                                                explicit approval + released kill switch
                                                            ▼
                                                 guarded live adapter (unwired)
```

Backend apps:

| App | Responsibility |
| --- | --- |
| `apps.coins` | Non-destructive top-100 ingestion, Binance contract mapping, snapshots |
| `apps.engine` | Feature extraction, grid simulator, walk-forward, scoring, reports |
| `apps.trading` | Paper strategy/adapter, evaluation, live gates, approvals, audit |
| `apps.jobs` | DB queue, worker, scheduler, job/schedule API |
| `apps.common` | Public Binance client and API token permission |

Time-series tables (created by migration via raw SQL): `market_candles`, `funding_rates`,
`coin_market_snapshots`, `paper_equity_points`. With `TIMESCALE_ENABLED=true` and the extension
installed, the migration converts them into hypertables; otherwise they stay regular Postgres
tables with the same indexes and queries.

## Quick start (Podman Desktop)

Requirements: Podman Desktop (or Podman CLI + Compose), Python 3.13, Node.js 20+.

```bash
cp .env.example .env
# Set ENGINE_API_TOKEN to a strong value before exposing anything.
podman compose up -d
```

This starts the database, API (`:8000`), background worker, and scheduler. The API container
runs migrations on boot.

Frontend:

```bash
cd frontend
cp env.example.txt .env.local      # set ENGINE_API_TOKEN to the same value as backend/.env
npm install
npm run dev
```

Open **http://localhost:3000** (use `localhost`, not `127.0.0.1`; Next.js dev blocks
cross-origin dev assets on the latter).

### Local backend development

Start only the database in Podman, then run Django processes directly:

```bash
podman compose up -d db

cd backend
python -m venv .venv
.venv/Scripts/activate          # Windows; use source .venv/bin/activate elsewhere
pip install -r requirements.txt
python manage.py migrate
python manage.py runserver
```

In separate terminals:

```bash
python manage.py runworker       # executes queued jobs
python manage.py runscheduler   # enqueues due monthly/daily/interval jobs
python manage.py runengine       # both loops in one process (single-task hosts)
python manage.py runjob coins_refresh       # run one job synchronously
```

The scheduler stores schedules in the database. It is safe to restart: due slots are claimed
with row locks, and stale running jobs are reaped after 60 minutes.

## Scheduled jobs

| Slug | Cadence | Purpose |
| --- | --- | --- |
| `coins-refresh` | Monthly, day 1 at 00:00 UTC | Fetch top 100 by market cap, update in place, remap Binance contracts |
| `daily-analysis` | Daily at 00:10 UTC | Sync candles/funding, then run the active trading mode's analysis first (walk-forward, score, report; it creates and retires paper sessions), then the other mode as a comparison when enabled |
| `paper-tick` | Every `PAPER_TICK_MINUTES` (default 5) | Advance active paper sessions from live public data |
| `paper-evaluate` | Every `PAPER_EVAL_INTERVAL_HOURS` (default 6) | Check promotion thresholds and hard stops |
| `paper-risk` | Every `RISK_INTERVAL_SECONDS` (default 5 min) | Run the overbought guard and resume sessions whose recovery confirmed |
| `btc-open-interest` | Hourly | Store BTC 1h open interest so the BTC regime's OI flush signal can be backtested |

Only one grid suitability analysis (`daily_analysis`) can be queued or running at a time. A
partial unique index on the job table enforces it for every path (API, schedule, `runjob`), and the
analysis itself holds a Postgres advisory lock so runs started outside the queue cannot overlap
either. A refused API request returns HTTP 409 with the blocking job's id; a scheduled slot that
finds one active is skipped and logged. Running analyses are only reaped as abandoned after 6 hours.

No job is ever queued twice: a second job with the same type and identical params is refused while
the first is still waiting (HTTP 409, enforced by a partial unique index). A schedule whose previous
job has not started yet skips its slot instead of stacking another, so a stopped worker never builds
a backlog of identical ticks.

All jobs can be triggered manually from the Jobs page or the API. Coin ingestion never deletes
rows: coins that leave the top 100 are kept with `in_top100=False`.

## Analysis methodology

**Universe and data.** The top-100 snapshot is refreshed monthly. Analysis uses Binance USDT
perpetual contracts; stablecoins, missing contracts, and non-trading symbols are excluded.
Candles are 4-hour bars (520-day lookback by default) with public funding-rate history.

**Features.** Liquidity (median ADV), realized volatility/ATR, range stability, mean reversion,
trend risk, funding cost for longs, and execution cost relative to expected edge.

**Grid simulation.** Long-only, flat start by default, geometric levels built from a percentile
range padded by ATR, periodic re-centering, maker fees on grid fills, taker fees on forced exits,
adverse slippage, funding charged on open inventory, isolated-margin constraints, and a stop /
liquidation check using each bar's worst print. Intrabar ordering is conservative (open → nearer
extreme → farther extreme → close); gap-crossed orders fill at the open; orders created by a fill
cannot recycle within the same bar.

Three range constructions are available through `range_mode`:

- `percentile` (default) — 5th/95th percentile of highs/lows over the lookback, padded by
  `atr_multiplier` ATRs.
- `price_centered` — symmetric band of `3 × atr_multiplier` ATRs around the live price.
- `atr_support` — floor at 30-day structural support plus an ATR buffer, ceiling at the recent
  swing high plus an ATR pad, so breakdowns do not buy into the floor and breakouts still have
  sell rungs. Only the top is clipped by `max_range_atr`.

The width cap scales with the ATR multiplier (`max(max_range_atr, 6 × atr_multiplier)` ATRs), so
comparing 2x/3x/4x pads changes the ladder instead of collapsing every variant onto the same band.

**Adaptive overlays.** Five risk rules sit on top of the grid and can only reduce exposure,
never increase it. Every threshold is a `GridParams` field, so it is fitted alongside the grid
shape instead of being hard-coded:

- **Structural stop.** The grid floor is kept above 30-day structural support. The stop sits
  `stop_atr_multiple` ATRs below the epoch floor and only ratchets upward while inventory is
  open. A trigger cancels the book and market-closes to USDT (`stop_loss` or `liquidation`).
- **Trailing up.** A close above the structural range high by `trail_up_atr_multiple` ATR with a
  confirmed uptrend shifts the ladder upward and raises the epoch stop. Ladder width is capped at
  `max(max_range_atr, 6 × atr_multiplier)` ATRs so a trend cannot stretch the range indefinitely.
- **Trailing take-profit.** Arms once equity reaches
  `capital × (1 + tp_activate_net_return)`; the exit trails the peak by `tp_atr_multiple` ATR and
  a triggered exit buys nothing for `TP_COOLDOWN_DAYS`.
- **Range position.** `pos = (price − low_30d) / (high_30d − low_30d)`. `pos ≥ 0.9` combined with
  a weakening trend blocks new buys; `pos < 0.1` is recorded as a lower-range signal.
- **Funding z-score.** `z = (funding − mean) / std` over the prior 90 prints (zero std → 0).
  `z ≥ 2` with weakening momentum cuts open exposure to `funding_cut_scale` of its allowance and
  blocks new buys until the crowd unwinds.

**Risk caps.** `max_inventory_pct` (default 1.0) bounds total position notional to a multiple of
equity and is always at least as tight as `leverage × max_utilization`, so one rung can never
exceed capital. `max_total_drawdown` (default 0 = off) stops and closes the book on a
peak-to-trough equity drawdown before the liquidation price is reached; the stop reason is
`drawdown_stop`.

**Adaptive rebalancing.** `rebalance_mode="fixed"` keeps the scheduled rebuild. `"adaptive"`
reviews the range once per `range_review_hours` (default 24) and rebuilds only when ATR has moved
`volatility_shift_ratio` (default 50%) from the baseline, price has closed
`breakout_atr_multiple` ATRs outside the active band, or the EMA20/50 trend regime flipped. Every
rebuild respects `rebalance_cooldown_hours` (default 12), and a weekly rebuild remains the
fallback for quiet markets. Rebuild reasons are counted (`rebalances`, `adaptive_rebalances`) and
persisted per session.

**Walk-forward validation.** Up to three folds use a 45% training window and an 18% test window.
Parameters are selected only on training data from a 96-combination grid (levels 10/20/30 ×
lookback 14/30 days × rebalance 7/14 days × stop 1.5/2.5 ATR × take-profit 1.5/3.0 ATR ×
fixed/adaptive rebalancing), so stop distance, trailing-profit retrace, and the rebalance mode
are validated rather than assumed. Test windows are untouched and stitched into a compounded
out-of-sample equity curve. The most frequently selected parameter set is reported alongside
parameter stability, plus the per-fold adaptive selections; paper sessions inherit the selected
mode.

Each untouched test window is also classified as `trending`, `ranging`, `high_volatility`, or
`mixed` from its own ADX, trend fit, realized volatility, and range containment. Per-regime
returns drive a regime-robustness score and a dependence penalty when nearly all profit comes
from one regime. Two further diagnostics rerun the same test windows without touching parameter
selection: a cost-stress run doubles maker/taker fees and slippage, and a sensitivity run varies
grid levels (±25%), range lookback, and spacing. These produce `cost_stress_*`,
`parameter_sensitivity`, and `regimes_covered` metrics, all persisted with the analysis.

**Hybrid trend-gated backtest.** The daily job runs the active trading mode's analysis first and
the other mode second as a comparison (`run_comparison_analysis`, default on; a failed comparison
does not fail the job). A `mode="hybrid"` run uses the full lookback (`ENGINE_LOOKBACK_DAYS`) and
the holdout like a normal run. Its 1h and 15m trend-gate inputs come from stored `market_candles`,
synced per coin with back-fill, and it replays the same rule paper/live hybrid mode executes: new
buys need the last closed 1h close above EMA(100), two consecutive closed 15m closes below that
EMA confirm an exit and flatten the book at the next bar open, and the ladder re-arms after two
closed 15m closes back above when the 1h trend still holds (ADX ≥ 25 with +DI > −DI when the ADX
preference is enabled). The simulator records `hybrid_blocked_bars` and `hybrid_exits` per fold,
and the gate statistics (`blocked_pct`, `confirmed_exits`) persist on each coin result plus the run
summary. Only the run for the active mode drives sessions (`drives_sessions`): it builds reports
and opens and retires paper sessions, so with hybrid mode on, sessions come from the hybrid
analysis. The other run is a comparison and leaves sessions and reports alone.

**Risk and execution metrics.** Open inventory is marked to market on every equity record, so
unrealized PnL is inside equity and drawdown. Each fold reports peak and average exposure,
maximum unrealized loss, max drawdown duration, recovery time, buy/sell fill counts, completed
buy-sell-cycle ratio, realized slippage, and the share of gross profit from the best trade and
best five trades. The analysis also snapshots the live order book (25 bps band by default) to
measure spread and whether visible depth covers one grid slot.

**Comparison sweep.** `apps.engine.compare` re-runs a fixed holdout window (default 90 days,
32-day warmup) across 24 variants: levels 10/15/20 × ATR 2/3/4 × fixed/adaptive, the three range
modes, and drawdown limits 5/10/15%. Each row reports net return, max drawdown, Calmar, profit
factor, trades, fees, funding, time in/out of range, rebalances, and stop/liquidation. Run it
against stored candles with:

```bash
python manage.py compare_grid BTCUSDT --holdout-days 90
python manage.py compare_grid BTC --output results.json
```

If the holdout never draws down as far as the tightest limit (5%), the three drawdown rows are
identical — the report still records the realized drawdown and any `drawdown_stop` trigger.

**Eligibility gates (run before ranking).** Every gate is a boolean with its observed value and
configured threshold. Defaults live in `ENGINE_*` and can be tuned per deployment:

- 120 days of candle history (`ENGINE_MIN_DATA_DAYS`)
- at least one completed walk-forward fold
- median 24h liquidity ≥ `ENGINE_MIN_ADV_USD`
- ≥ `ENGINE_MIN_OOS_TRADES` OOS fills
- positive OOS net return
- OOS max drawdown ≤ `ENGINE_MAX_DRAWDOWN`
- at least half the folds profitable
- no stop-out or liquidation
- ATR inside `ENGINE_MIN_ATR_PCT`–`ENGINE_MAX_ATR_PCT`
- spread ≤ `ENGINE_MAX_SPREAD_BPS` and depth cover ≥ `ENGINE_MIN_DEPTH_COVER` (when an
  order-book snapshot exists)
- the cost-stressed return stays above `ENGINE_MIN_STRESSED_RETURN`
- profit factor > `ENGINE_PF_VALIDATE` requires ≥ 24 fills, no stop-out, and a profitable stress
  run; profit factor > `ENGINE_PF_REVIEW` is flagged for manual review either way

Only eligible coins are ranked. Ineligible coins keep their score for comparison and carry the
full list of rejection reasons.

**Ranking.** Eligible scores blend eight cross-sectional percentile factors so correlated
measurements are grouped rather than double-counted:

- Profitability 18% — profit factor, win rate
- Risk-adjusted return 18% — net return, return-to-drawdown
- Drawdown control 15% — max DD, recovery time, stop-out, unrealized loss, 1% tail loss
- Fold consistency 14% — profitable-fold share, worst fold, return dispersion, parameter stability
- Regime robustness 10% — regime coverage/profitability, time in range
- Structural stability 10% — mean reversion, range stability, parameter sensitivity/stability
- Execution quality 10% — ADV, cost-to-edge, spread, depth cover, funding, stressed return
- Diversification 5% — daily-return correlation with eligible peers

Penalties then subtract points for stop-outs, unprofitable folds, thin samples, failed cost
stress, single-regime dependence, sharp parameter sensitivity, trade concentration, high
correlation, slow recovery, and excessive inventory exposure. The legacy four-component formula
is still computed as `baseline_score`, so `score_delta` shows what the enhanced ranking changed
for each coin.

**Final holdout.** The newest `holdout_days` (default 90, Admin → Strategies) never take part in
parameter search, fold scoring, or ranking. After everything else is fixed, the deployed parameters
(the most recent fold's selection) are simulated once over that window. Holdout returns are
reported and drive score validation; they never change a rank.

**Fill model.** `trade_through` (default, 2 bps) fills a resting order only when price trades
through the level; `touch` fills on contact. Every coin reports both, and the dashboard flags coins
that are profitable only under touch fills.

**Score validation.** Each run computes the Spearman rank correlation (IC) between the score, each
factor, and the holdout return, plus a pooled IC over the last runs. Admin → Strategies turns these
and the strategy-correlation diagnostics into strengths and weaknesses.

**Correlation.** Diversification is measured on the grid strategies' own daily returns (walk-forward
test days plus the holdout, flat after a stop-out), not on coin prices: every long-only grid is
partly long the market, so strategy returns are what the book holds. Each strategy's correlation
and beta to BTC are reported, and price correlation is kept for reference. The admin
`max_strategy_correlation` (default 0.70) drives the penalty and paper selection, which also
rejects coins correlated with sessions already open.

**Price correlation (reference).** Daily closes aligned on common dates give pairwise correlations over
`ENGINE_CORR_LOOKBACK_DAYS`. Coins above `ENGINE_MAX_CORRELATION` with an eligible peer are
flagged, penalized, and skipped when paper sessions are created, so the paper book is not a
stack of the same trade. The greedy selector always keeps the first candidate.

Suitability bands: `prime` ≥ 75, `viable` ≥ 60, `watch` ≥ 45, otherwise `avoid`; failed gates
become `ineligible`. Reports are written for rankings, performance, risk, and methodology.

The dashboard exposes the hard gates, review flags, factor scores/weights/contributions,
baseline-vs-enhanced delta, regime, cost-stress, sensitivity, exposure, recovery, correlation,
and return-concentration diagnostics. The original OOS return, max drawdown, profit factor,
trades, ADV, ATR%, and funding columns remain on the rankings table.

The test suite includes a truncation test: truncating future bars does not change any trade or
equity point before the cutoff, guarding against look-ahead bias.

## Admin strategy settings

`/admin/strategies` holds one `StrategySettings` row (`GET`/`POST /api/strategy/settings/`):

| Setting | Default | Effect |
| --- | --- | --- |
| Signal interval | `1h` | Candles for both the daily backtest and paper trading. Changing it can queue a fresh analysis. |
| Fill model | trade-through 2 bps | Used by backtest and paper; the other model is always reported |
| Holdout | 90 days | Untouched final window (0 = off) |
| Overbought guard | on | RSI/MACD/VWAP/open-interest exit on overbought tops; not simulated by the backtest. When switched off, sessions it paused are resumed |
| BTC regime protection | on | Global BTC check (see below): blocks buys and moves sells to the nearest levels on every coin; modelled in the backtest |
| BTC trailing stop / window | 8% / 7 days | Bearish when BTC is this far below its high over the window (2–30%, 1–30 days) |
| BTC OI flush / near high | 5% / 3% | Bearish when 1h OI falls this much over 6 readings while BTC is within this of its 24h high (1–30%, 0.5–10%) |
| BTC funding crowding | z 2.0 | Warning when funding z-score over 90 prints reaches this while momentum < 0 or the trend is not up (0.5–5.0) |
| BTC minimum bearish | 4 hours | Earliest recovery after entering bearish (0–72) |
| Book capital / max sessions | $8,000 / 8 | Each new session gets book ÷ sessions |
| Exposure cap | 1.0× book | New buys stop above this total open notional |
| Daily loss limit | 3% of book | Halts new buys until the next UTC day |
| Retire after | 2 runs | Close a session whose coin fails to qualify this many analyses in a row |
| Max strategy correlation | 0.70 | Correlation penalty and paper selection |
| Run comparison analysis | on (`run_comparison_analysis`) | Also run the inactive trading mode's analysis after the active one, for the dashboard comparison |

Book limits only block new buys; stops and exits always run. `GET /api/strategy/validation/`
returns the score ICs, strategy/paper correlation, BTC beta, paper-vs-holdout drift, and findings.

**Backtest/paper parity.** Paper trading replays each completed candle through
`IncrementalGridStrategy.step`, which rebuilds context from strictly earlier candles and applies
gates, rebalancing, fills, stops, and exits in the same order as `simulate_grid`. A test suite
asserts identical fills on 1h and 4h data, with adaptive rebalancing, initial inventory, and a
breakdown. The daily analysis runs coins in `ENGINE_WORKERS` processes and caches indicator series
across the 96 candidates, so a 1h run takes roughly 20 seconds of CPU per coin.

**Interval-independent ATR.** Every ATR multiple (stop, take-profit trail, range pad, width caps,
ATR% gate) was calibrated on 4h bars. Per-bar true range grows with about the square root of the
bar length (measured 4h/1h ratio across the top-100 universe: 2.05), so ATR is converted to
4h-equivalent units (`× √(bars_per_day / 6)`). Without this, switching to 1h halved every stop
distance and 54 of 60 coins stopped out in all three folds.

## BTC market regime protection

Sell-offs are usually led by BTC, so one BTC check protects every paper session at once instead
of waiting for each coin's own gate. BTC is evaluated on its last closed 15m candle at the start
of every paper tick (before any session ticks), and each closed bar is evaluated once.

| Phase | Entered on | Effect on every coin |
| --- | --- | --- |
| `normal` | no warning or bearish signal | none |
| `warning` | BTC 15m RSI > 75 (BTC overbought guard armed), or funding crowding | New buys blocked; sells moved to the 2 nearest levels |
| `bearish` | BTC guard trailing or OI-flush exit (not its momentum-decay exit), trailing stop, open-interest flush near the 24h high, or a 1h close below 30-day support âˆ’ 0.25 ATR | Same as `warning`; the daily analysis also opens no new sessions |

While the phase is not `normal`, every grid stops buying and offers its whole inventory on the two
grid levels just above the price, so it sheds inventory on the first bounce instead of waiting for
the top of the range. Nothing is sold at market and no session is paused; stops, take-profits and
each coin's own overbought guard keep running. When BTC returns to `normal` the full ladder is
rebuilt.

`bearish` is left only when **all** recovery checks pass: the guard's recovery (RSI above 35 and
rising, 15m close above VWAP, OI turning up or 60 minutes without OI data), back above the support
line, inside the trailing stop, no OI flush, and the minimum bearish duration. The new phase is
`warning` if a warning signal still holds, else `normal`. If BTC data cannot be fetched, the phase
does not change and the error is shown. It applies identically in normal and hybrid mode (no
EMA100 gate on BTC).

**Why this design.** It was chosen by measuring alternatives on the full universe (58 coins, 520
days), comparing each coin's walk-forward with and without the regime:

| Bearish behaviour | Mean return effect | Mean drawdown effect | Coins with lower drawdown |
| --- | --- | --- | --- |
| Sell everything and pause | âˆ’7.0 pts | 0.0 | 41% |
| Block buys only | âˆ’0.8 pts | 0.4 pts lower | 48% |
| Block buys, and keep blocking while BTC cools after a fading rally | âˆ’3.5 pts | 0.7 pts higher | 35% |
| **Block buys and sell at the nearest levels (current)** | âˆ’0.9 pts | 1.1 pts lower | 62% |

The guard's momentum-decay exit (MACD shrinking with a close below VWAP) does not turn the regime
bearish: on BTC it followed almost every overbought spike (163 of 229 bearish entries) and only
churned the book.

**Backtest.** The daily analysis syncs BTC 15m and 1h candles, replays the same evaluator over the
window once per run and gates every coin's simulations with it: buys
disarmed and sells moved to the nearest levels while not `normal`. Test folds and the holdout also
run without the regime, reported per coin as `regime_off_return`, `regime_effect_return` and
`regime_effect_drawdown`; validation turns the averages into a strength or weakness finding.
Parity tests check that the live controller produces the same phase changes as the backtest
timeline, and that the paper strategy fills exactly like the simulator through a de-risk window.

Every phase change is a `regime` grid event (symbol `BTCUSDT`) plus a decision-log entry.
`GET /api/strategy/regime/` returns the phase, readings, thresholds, recovery checklist and recent
transitions, shown on Admin > Strategies with banners on Overview and Paper sessions.

## Trading stages

1. **Historical backtest** — daily analysis over the eligible top-100 universe.
2. **Paper trading** — the same `IncrementalGridStrategy` state machine as the future live path,
   driven by live public data with simulated orders, fees, slippage, funding, and position
   tracking. The tick job runs every 5 minutes but signals only on completed candles at
   `PAPER_SIGNAL_INTERVAL` (default `1h`), so a forming bar can never trigger a fill. Every
   initialize/rebalance persists a per-level plan (price, side, qty, and why a rung was skipped:
   `risk_gate`, `no_capacity`, `no_inventory`, `derisk` (BTC regime: sells kept to the nearest
   levels), or `at_price`) into `PaperSession.grid_state` and
   the decision log, and state survives worker restarts.
3. **Evaluation** — promotion requires at least 7 days, +1% net return, max 12% drawdown, and
   20 fills (configurable). A 20% hard drawdown stop stops the session independently.
4. **Live approval** — only a promotion-ready session can request approval; an operator must
   explicitly approve it, then release the kill switch.
5. **Live execution** — `LiveExecutionAdapter` shares strategy logic but is a separate adapter.
   No scheduled job calls it and no HTTP endpoint places live orders.

## Live safety gates

`assert_live_allowed()` denies unless **all** of the following hold:

- `LIVE_TRADING_ENABLED=true`
- Binance API key and secret are configured (withdrawals must be disabled on the key)
- The kill switch is released
- An approved `LiveApproval` record is active
- The symbol is in `LIVE_SYMBOL_ALLOWLIST` (when the list is non-empty)
- Notional and leverage are within configured limits

Every denial is written to `LiveOrderAudit` with the reason. Revoking an approval re-engages the
kill switch automatically. `BINANCE_FUTURES_TESTNET=true` keeps the live client pointed at the
testnet.

## API

Reads are public when `ENGINE_ALLOW_PUBLIC_READS=true`. Every mutation requires the
`X-Engine-Token` header. The Next.js server proxy injects the token, so it never reaches the
browser.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/health/` | Liveness probe |
| GET | `/api/coins/` | Coin universe (`in_top100`, `eligible`, `search`) |
| POST | `/api/coins/add/` | Add a coin manually (Binance contract required) and queue a solo analysis run |
| GET | `/api/coins/{coingecko_id}/snapshots/` | Market-cap snapshot history |
| GET | `/api/engine/dashboard/` | Dashboard aggregate |
| GET | `/api/engine/analysis/` · `/latest/` | Analysis runs (`?mode=normal|hybrid`) |
| GET | `/api/engine/analysis/{id}/results/` | Full result payloads |
| GET | `/api/engine/results/` | Coin results for the latest run of the active trading mode (`?mode=` selects normal or hybrid) |
| POST | `/api/engine/analysis/trigger/` | Queue a daily analysis |
| GET | `/api/engine/results/` | Latest rankings (`compact`, `suitability`, `eligible`, `search`) |
| GET | `/api/engine/results/by_coin/?symbol=BTC` | Analysis history for one coin |
| GET | `/api/engine/reports/` · `/latest/` | Markdown reports (`kind`) |
| GET | `/api/engine/decisions/` | Decision log |
| GET | `/api/paper/sessions/` · `/{id}/` | Paper sessions |
| GET | `/api/paper/sessions/{id}/equity/` · `/fills/` · `/evaluations/` | Session history |
| POST | `/api/paper/sessions/{id}/tick/` · `/pause/` · `/resume/` · `/stop/` · `/evaluate/` · `/bias/` | Session actions (`bias` is `bullish` or `bearish`) |
| POST | `/api/paper/sessions/tick/` | Queue a tick for all (or selected) sessions |
| GET | `/api/live/status/` | Gate status, approvals, audit trail |
| POST | `/api/live/approvals/request_approval/` | Request approval for a promotion-ready session |
| POST | `/api/live/approvals/{id}/approve/` · `/revoke/` | Review approval |
| POST | `/api/live/status/kill-switch/` | Engage/release the kill switch |
| GET | `/api/jobs/summary/` · `/runs/` · `/schedules/` | Queue and schedule state |
| POST | `/api/jobs/runs/run/` | Queue a job: `{"job_type": "...", "params": {}}` |
| POST | `/api/jobs/runs/{id}/cancel/` | Cancel a queued job |
| POST | `/api/jobs/schedules/{slug}/run/` · `/toggle/` | Run or enable/disable a schedule |
| GET · POST | `/api/strategy/settings/` | Strategy and portfolio settings |
| GET | `/api/strategy/validation/` | Score, correlation and drift validation |
| GET | `/api/strategy/regime/` | BTC market regime: phase, readings, recovery checklist, transitions |

Example:

```bash
curl -X POST http://localhost:8000/api/jobs/runs/run/ \
  -H "Content-Type: application/json" \
  -H "X-Engine-Token: $ENGINE_API_TOKEN" \
  -d '{"job_type":"daily_analysis"}'
```

## ETL API (v1)

A read-only, versioned REST API for external ETL and analytics tools lives under `/api/v1/`. It
uses its own Bearer keys (`manage.py etl_api_key create|revoke|list`), cursor pagination with
watermarks, and serves an OpenAPI 3 document at `/api/v1/meta/schema/`. See
[docs/etl-api-v1.md](docs/etl-api-v1.md) for authentication, pagination, errors, every endpoint
and worked curl examples.

## Configuration

Copy `.env.example` to `.env`. Key settings:

| Variable | Default | Notes |
| --- | --- | --- |
| `DATABASE_URL` | localhost grid DB | Any PostgreSQL 14+; append `?sslmode=require` for Neon |
| `TIMESCALE_ENABLED` | `false` | `true` converts time-series tables to hypertables when available |
| `DB_CONN_MAX_AGE` | `60` | Set `0` for Neon/serverless Postgres that autosuspends |
| `ENGINE_API_TOKEN` | dev token | Required for all mutations |
| `ENGINE_ALLOW_PUBLIC_READS` | `true` | Set `false` in production |
| `ENGINE_LOOKBACK_DAYS` | `520` | Backtest history |
| `ENGINE_INTERVAL` | `4h` | Candle interval |
| `ENGINE_MIN_ADV_USD` | `5,000,000` | Liquidity gate |
| `ENGINE_MIN_OOS_TRADES` | `12` | Minimum out-of-sample fills |
| `ENGINE_MAX_DRAWDOWN` | `0.35` | OOS drawdown gate |
| `ENGINE_MIN_DATA_DAYS` | `120` | Minimum candle history |
| `ENGINE_MIN_ATR_PCT`, `ENGINE_MAX_ATR_PCT` | `0.0`, `0.12` | 14-day ATR band; `0` disables a side |
| `ENGINE_PF_REVIEW`, `ENGINE_PF_VALIDATE` | `10`, `30` | Profit-factor review / validation thresholds |
| `ENGINE_COST_STRESS_MULTIPLIER` | `2.0` | Fee/slippage multiplier for stress run |
| `ENGINE_ENFORCE_COST_STRESS` | `true` | Reject strategies that fail the stress run |
| `ENGINE_MAX_SPREAD_BPS`, `ENGINE_MIN_DEPTH_COVER` | `10`, `1.0` | Order-book gates |
| `ENGINE_MAX_CORRELATION` | `0.85` | Correlation penalty and paper diversification |
| `ENGINE_MAX_TOP_TRADE_SHARE` | `0.60` | Return-concentration flag |
| `ENGINE_TOP_N_PAPER` | `8` | Paper sessions created per analysis |
| `PAPER_*` | see file | Tick cadence, 1h signal interval (`PAPER_SIGNAL_INTERVAL`), evaluation thresholds, hard stop |
| `LIVE_TRADING_ENABLED` | `false` | Must stay false until explicitly approved |
| `BINANCE_FUTURES_TESTNET` | `true` | Live adapter target |
| `LIVE_MAX_NOTIONAL_USD`, `LIVE_MAX_LEVERAGE` | `250`, `2` | Live limits |
| `LIVE_SYMBOL_ALLOWLIST` | empty | Comma-separated symbols |

The frontend needs `ENGINE_BACKEND_URL` and `ENGINE_API_TOKEN` in `frontend/.env.local`
(server-side only).

## Deployment

See [DEPLOYMENT.md](DEPLOYMENT.md) for Northflank (Django API, worker, scheduler, and Postgres)
and Vercel (Next.js dashboard) setup, including the free-tier limits that matter for this
workload.

## Tests and build

```bash
cd backend
.venv/Scripts/python.exe manage.py test apps -v 2

cd ../frontend
npm run typecheck
npm run lint
npm run build
```

Backend tests cover non-destructive ingestion, contract-multiplier mapping, eligibility
exclusions, grid determinism and look-ahead resistance, walk-forward fold integrity, scoring
gates/ranking, live-gate denial + audit, paper session transitions, first-tick grid persistence,
per-level plan auditing, rung replenishment and gate re-arming, adaptive/fixed rebalancing with
cooldown, 1h completed-candle signals on 5-minute ticks, range modes, inventory/drawdown caps,
the 24-variant comparison sweep, job token enforcement, scheduling cadence, and worker
success/failure handling.

## Limitations

- Ranked results depend on the configured history window and only cover coins with Binance USDT
  perpetuals; listing dates and data gaps can shorten folds.
- The simulator assumes fills at grid levels with adverse slippage; real queue position, partial
  fills, and exchange outages are not modeled.
- Funding is applied from public history; future funding is unknown.
- The BTC regime's open-interest flush is only backtested where stored OI history exists (the
  first `btc-open-interest` run back-fills 500 hours, about 21 days, then it grows hourly). If
  Binance publishes an hourly OI row late, the backtest can use a row live trading could not yet
  see at that moment.
- The regime reacts on closed 15m candles, so paper trading responds within one 15m close plus up
  to one tick, and applies the phase at tick time to the candles that tick replays. The backtest
  applies the phase at each coin bar's open, so on 1h/4h signals the two can differ by up to one
  bar around a phase change.
- Paper fills are simulated from live public data, not from an exchange matching engine.
- Live trading is intentionally not wired to schedulers or the API. Enabling it requires code
  changes, exchange-side risk controls, and explicit operational sign-off.
