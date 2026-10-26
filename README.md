# Grid Suitability Engine

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
| `daily-analysis` | Daily at 00:10 UTC | Sync candles/funding, run walk-forward, score, report, create paper sessions |
| `paper-tick` | Every `PAPER_TICK_MINUTES` (default 5) | Advance active paper sessions from live public data |
| `paper-evaluate` | Every `PAPER_EVAL_INTERVAL_HOURS` (default 6) | Check promotion thresholds and hard stops |

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

**Walk-forward validation.** Up to three folds use a 45% training window and an 18% test window.
Parameters are selected only on training data from a 12-combination grid (levels 10/20/30 ×
lookback 14/30 days × rebalance 7/14 days). Test windows are untouched and stitched into a
compounded out-of-sample equity curve. The most frequently selected parameter set is reported
alongside parameter stability.

**Ranking.** Component scores are cross-sectional percentiles:

- Profitability 35% — net return, Calmar, profit factor, win rate
- Survivability 30% — drawdown, fold consistency, worst fold, no stop-out, funding cost
- Stability 20% — mean reversion, range stability, parameter stability, time in range
- Execution 15% — ADV, cost-to-edge, funding, volume variability

Hard gates require positive OOS return, sufficient trades, drawdown below the configured limit,
at least half the folds profitable, adequate history, no stop-out, and minimum liquidity.
Suitability bands: `prime` ≥ 75, `viable` ≥ 60, `watch` ≥ 45, otherwise `avoid`; failed gates
become `ineligible`. Reports are written for rankings, performance, risk, and methodology.

The test suite includes a truncation test: truncating future bars does not change any trade or
equity point before the cutoff, guarding against look-ahead bias.

## Trading stages

1. **Historical backtest** — daily analysis over the eligible top-100 universe.
2. **Paper trading** — the same `IncrementalGridStrategy` state machine as the future live path,
   driven by live public data with simulated orders, fees, slippage, funding, and position
   tracking. State is persisted in `PaperSession.grid_state`, so workers can restart safely.
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
| GET | `/api/coins/{coingecko_id}/snapshots/` | Market-cap snapshot history |
| GET | `/api/engine/dashboard/` | Dashboard aggregate |
| GET | `/api/engine/analysis/` · `/latest/` | Analysis runs |
| GET | `/api/engine/analysis/{id}/results/` | Full result payloads |
| POST | `/api/engine/analysis/trigger/` | Queue a daily analysis |
| GET | `/api/engine/results/` | Latest rankings (`compact`, `suitability`, `eligible`, `search`) |
| GET | `/api/engine/results/by_coin/?symbol=BTC` | Analysis history for one coin |
| GET | `/api/engine/reports/` · `/latest/` | Markdown reports (`kind`) |
| GET | `/api/engine/decisions/` | Decision log |
| GET | `/api/paper/sessions/` · `/{id}/` | Paper sessions |
| GET | `/api/paper/sessions/{id}/equity/` · `/fills/` · `/evaluations/` | Session history |
| POST | `/api/paper/sessions/{id}/tick/` · `/pause/` · `/resume/` · `/stop/` · `/evaluate/` | Session actions |
| POST | `/api/paper/sessions/tick/` | Queue a tick for all (or selected) sessions |
| GET | `/api/live/status/` | Gate status, approvals, audit trail |
| POST | `/api/live/approvals/request_approval/` | Request approval for a promotion-ready session |
| POST | `/api/live/approvals/{id}/approve/` · `/revoke/` | Review approval |
| POST | `/api/live/status/kill-switch/` | Engage/release the kill switch |
| GET | `/api/jobs/summary/` · `/runs/` · `/schedules/` | Queue and schedule state |
| POST | `/api/jobs/runs/run/` | Queue a job: `{"job_type": "...", "params": {}}` |
| POST | `/api/jobs/runs/{id}/cancel/` | Cancel a queued job |
| POST | `/api/jobs/schedules/{slug}/run/` · `/toggle/` | Run or enable/disable a schedule |

Example:

```bash
curl -X POST http://localhost:8000/api/jobs/runs/run/ \
  -H "Content-Type: application/json" \
  -H "X-Engine-Token: $ENGINE_API_TOKEN" \
  -d '{"job_type":"daily_analysis"}'
```

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
| `ENGINE_TOP_N_PAPER` | `8` | Paper sessions created per analysis |
| `PAPER_*` | see file | Tick cadence, evaluation thresholds, hard stop |
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
job token enforcement, scheduling cadence, and worker success/failure handling.

## Limitations

- Ranked results depend on the configured history window and only cover coins with Binance USDT
  perpetuals; listing dates and data gaps can shorten folds.
- The simulator assumes fills at grid levels with adverse slippage; real queue position, partial
  fills, and exchange outages are not modeled.
- Funding is applied from public history; future funding is unknown.
- Paper fills are simulated from live public data, not from an exchange matching engine.
- Live trading is intentionally not wired to schedulers or the API. Enabling it requires code
  changes, exchange-side risk controls, and explicit operational sign-off.
