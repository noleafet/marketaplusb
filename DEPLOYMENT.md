# Deployment: Northflank + Vercel

This setup runs the whole backend on **Northflank's free Sandbox** — API, background engine, and
PostgreSQL — and the dashboard on **Vercel**.

```
Browser ──► Vercel (Next.js dashboard)
                │  /api/engine/*  (server-side proxy, X-Engine-Token)
                ▼
        Northflank service: api (gunicorn + Django)
                │
        Northflank service: worker (runengine = scheduler + job worker)
                │
                ▼
        Northflank PostgreSQL addon
```

Northflank's free Sandbox includes **2 always-on services, 2 jobs, and 1 database**, with up to
**$20/month of resources**, free SSL, and no card required. That is exactly enough for the API,
the background engine, and the database.

The database add-on is billed as provisioned compute, not metered by query hours, so the
always-on worker can keep paper trading running 24/7 without the compute-quota problem a
serverless Postgres such as Neon Free has.

TimescaleDB is **not used**. It is disabled by default (`TIMESCALE_ENABLED=false`), so the
time-series tables are ordinary Postgres tables.

---

## 0. Cost and limits

| Component | Plan | Estimated cost |
| --- | --- | --- |
| API service | nf-compute-20: 0.2 vCPU, 512 MB | ~$5.47/month |
| Worker service | nf-compute-20: 0.2 vCPU, 512 MB | ~$5.47/month |
| PostgreSQL addon | nf-compute-20: 0.2 vCPU, 512 MB, 1 GB storage | ~$5.47/month + storage |
| Vercel | Hobby | $0, non-commercial |

At Northflank's current rates ($0.01667/vCPU-hour and $0.00833/GB-hour), the three workloads cost
about **$16.41/month**, inside the $20 Sandbox credit. The API can use `nf-compute-10`
(0.1 vCPU / 256 MB) to free up more headroom if dashboard queries stay light.

The daily analysis consumed **~989 CPU-seconds** locally. On 0.2 vCPU, expect roughly 75–90
minutes of wall time. Keep `ENGINE_SYMBOL_LIMIT` around 40 for the first runs; paper ticks are
cheap.

The Sandbox is intended for development and evaluation. If this becomes a production service
with an SLA, move to Northflank's pay-as-you-go plan.

---

## 1. Push the repository to GitHub

Northflank builds from a Git repository. The workspace is not currently initialized as one:

```bash
cd /path/to/maketaplusb
git init
git add .
git status          # confirm .env, .venv, and node_modules are not staged
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/YOUR_USER/maketaplusb.git
git push -u origin main
```

If the repository is private, connect GitHub to Northflank and grant access to that repository.

---

## 2. Create the Northflank project and database

1. Sign in at <https://app.northflank.com>. The free Sandbox requires no card.
2. **Create project** → name it `maketaplusb` → choose the region closest to you.
3. Open the project → **Add-ons** → **Create add-on** → **PostgreSQL**.
4. Configure:
   - Name: `grid-db`
   - Compute plan: `nf-compute-20` (0.2 vCPU / 512 MB)
   - Storage: 1 GB (enough for the candle/funding history; increase later if needed)
   - Region: the same region as the services
5. Create the add-on. Northflank provisions a persistent database with backups.
6. Link the database to a secret group:
   - Create a secret group named `grid-secrets` in the project.
   - Under **Linked add-ons**, select `grid-db`.
   - Services that inherit `grid-secrets` receive the connection variables, including
     `DATABASE_URL`.

The settings also accept `POSTGRES_URI` as a fallback, so either injected name works. If you
prefer not to use secret groups, copy the add-on connection string and set `DATABASE_URL`
manually.

No local migration step is required: the API service will run migrations inside Northflank in
section 4.

---

## 3. Create the API service

1. In the project: **Services** → **Create service** → **Combined service** (build from Git).
2. Select your repository and the `main` branch.
3. Build settings:
   - Builder: **Dockerfile**
   - Dockerfile location: `/backend/Dockerfile`
   - Build context: `/backend`
4. Runtime:
   - Service type: **Web service**
   - Instance: `nf-compute-20` (0.2 vCPU / 512 MB)
   - Port: **8000**, public HTTP
   - Health check: HTTP `GET /api/health/` on port 8000
   - Command: leave empty to use the Dockerfile's gunicorn command
5. Environment variables. Inherit `grid-secrets`, then add:

   ```dotenv
   DEBUG=false
   SECRET_KEY=GENERATE_A_LONG_RANDOM_VALUE
   DJANGO_ALLOWED_HOSTS=.code.run,localhost,127.0.0.1
   CORS_ALLOWED_ORIGINS=https://YOUR_APP.vercel.app
   TIME_ZONE=UTC

   TIMESCALE_ENABLED=false
   DB_CONN_MAX_AGE=60

   ENGINE_API_TOKEN=GENERATE_A_LONG_RANDOM_VALUE
   ENGINE_ALLOW_PUBLIC_READS=false
   ENGINE_SYMBOL_LIMIT=40
   ENGINE_LOOKBACK_DAYS=365

   LIVE_TRADING_ENABLED=false
   BINANCE_FUTURES_TESTNET=true
   ```

   `DATABASE_URL` comes from `grid-secrets`; do not hard-code the database password.

   `.code.run` as an `ALLOWED_HOSTS` entry matches every Northflank public domain. After the
   service is live, you can replace it with the exact URL from the service page.
6. Deploy. Wait for the health check, then copy the public `https://…code.run` URL.

If the HTTP health check fails with a 400, Northflank is sending a host Django does not accept.
Check the service logs, add that host to `DJANGO_ALLOWED_HOSTS`, and redeploy. A TCP health check
on port 8000 is a fallback that avoids the Host header.

---

## 4. Migrate the database

The health endpoint does not query the database, so the API can become healthy before the schema
exists. Open the API service's **Console** (or use the Northflank CLI/API to execute a command)
and run:

```bash
python manage.py migrate --noinput
python manage.py check
```

Verify in the database add-on's console or query editor:

```sql
SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY 1;
```

You should see the Django tables plus `market_candles`, `funding_rates`,
`coin_market_snapshots`, and `paper_equity_points`.

For later schema changes, run the same migration command before deploying code that depends on
them. Northflank's free plan also includes 2 jobs, which can run one-off commands, but the
service console is simpler.

---

## 5. Create the worker service

The scheduler and job worker must run continuously. Both fit in one service because `runengine`
runs the scheduler loop in a thread alongside the worker loop.

1. **Services** → **Create service** → **Combined service** → same repository and `main`.
2. Build settings: the same Dockerfile and build context as the API.
3. Runtime:
   - Service type: **Worker**
   - Instance: `nf-compute-20` (0.2 vCPU / 512 MB). Use `nf-compute-50` (0.5 vCPU / 1 GB) if you
     want the daily analysis to finish faster, but watch the $20 credit.
   - Command:

     ```bash
     python manage.py runengine --worker-poll 2 --scheduler-poll 30
     ```

   - No port and no HTTP health check; the process must keep running.
4. Inherit `grid-secrets` and add the same application environment variables as the API.
5. Deploy and check the logs. On first start it creates the default schedules; within a few
   minutes it enqueues paper ticks, and at 00:10 UTC it enqueues the daily analysis.

The two services now consume the Sandbox's two-service allowance. Do not create a separate
scheduler service.

---

## 6. Seed the universe and run a smoke test

Wait until the worker is healthy, then queue the monthly refresh:

```bash
TOKEN=YOUR_ENGINE_API_TOKEN
BASE=https://YOUR_API.code.run

curl -s -X POST $BASE/api/jobs/runs/run/ \
  -H "Content-Type: application/json" \
  -H "X-Engine-Token: $TOKEN" \
  -d '{"job_type":"coins_refresh"}'
```

Then queue a small analysis to verify the pipeline:

```bash
curl -s -X POST $BASE/api/jobs/runs/run/ \
  -H "Content-Type: application/json" \
  -H "X-Engine-Token: $TOKEN" \
  -d '{"job_type":"daily_analysis","params":{"limit":10,"force":true}}'
```

Both calls only enqueue work. Watch the worker logs for execution. The full analysis will take
substantially longer on 0.2 vCPU; raise or remove `ENGINE_SYMBOL_LIMIT` only after checking the
first run's duration and Northflank's resource usage.

---

## 7. Deploy the dashboard on Vercel

1. Vercel → **Add New → Project** → import the repository.
2. Set **Root Directory** to `frontend`.
3. Framework Preset: **Next.js**. Leave the build command as `next build`.
4. `frontend/.gitignore` ignores `package-lock.json` but `bun.lock` is tracked, so Vercel installs
   with Bun. Leave the install command on the default. If you prefer npm, commit a lockfile and
   set the install command to `npm ci`.
5. Environment variables for Production and Preview:

   | Variable | Value |
   | --- | --- |
   | `ENGINE_BACKEND_URL` | the Northflank API URL, e.g. `https://api-maketaplusb.code.run` (no trailing slash) |
   | `ENGINE_API_TOKEN` | the same token as the Northflank services |
   | `NEXT_PUBLIC_APP_URL` | `https://YOUR_APP.vercel.app` |
   | `NEXT_PUBLIC_SENTRY_DISABLED` | `true` |

6. Deploy. Then update `CORS_ALLOWED_ORIGINS` on the Northflank services with the final Vercel
   domain if it differs, and restart/redeploy them.

`ENGINE_BACKEND_URL` and `ENGINE_API_TOKEN` are server-only. They are read by
`frontend/src/app/api/engine/[...path]/route.ts`, which forwards requests to Django and injects
`X-Engine-Token`. Do not rename the token to `NEXT_PUBLIC_*`.

---

## 8. Post-deployment checks

```bash
curl -s $BASE/api/health/

curl -s -H "X-Engine-Token: $TOKEN" $BASE/api/engine/results/ | head

curl -i $BASE/api/engine/results/     # expect 403 when public reads are disabled
```

Then open the Vercel dashboard:

- `/dashboard` loads aggregates;
- `/dashboard/jobs` lists the schedules and recent runs;
- triggering **Monthly coin refresh** creates a queued job that the worker executes.

Schedules run in UTC: monthly on day 1 at 00:00, daily analysis at 00:10, paper ticks every 5
minutes, paper evaluation every 6 hours.

---

## 9. Operational notes

- **Keep the $20 credit in mind.** Three `nf-compute-20` workloads are ~$16.41/month before
  storage and bandwidth. Do not add replicas or larger plans without checking usage.
- **CPU is the limiting factor.** `ENGINE_SYMBOL_LIMIT` is the main control. Start at 40; a
  10-coin smoke test is cheap.
- **Paper trading runs continuously.** The database add-on is provisioned compute, so the
  5-minute tick and the worker's 2-second queue poll do not consume a metered query quota. There
  is no equivalent of Neon Free's 100 CU-hour limit; the cost is the fixed add-on price.
- **Database access:** the linked secret group injects `DATABASE_URL`; the app also accepts
  `POSTGRES_URI`. Keep `DB_CONN_MAX_AGE=60` for Northflank's persistent database.
- **State lives in Postgres**, including `PaperSession.grid_state`, so restarting services is
  safe.
- **Live trading stays off.** `LIVE_TRADING_ENABLED=false` and no scheduler or endpoint places
  live orders.
- **Backups:** Northflank's managed Postgres includes backups; verify the retention period in the
  add-on settings and export a logical backup before large changes.
- **Storage:** 1 GB is enough for the default 40-symbol, 520-day history. Raising
  `ENGINE_SYMBOL_LIMIT` and `ENGINE_LOOKBACK_DAYS` together multiplies the candle volume; watch
  the add-on's storage usage.

If you would rather use Neon for the database, set `DATABASE_URL` to the Neon direct connection
string with `?sslmode=require` and `DB_CONN_MAX_AGE=0`. The always-on worker keeps Neon awake, so
Neon Free's 100 CU-hours last only about 16 days of continuous paper trading.
