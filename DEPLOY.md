# Deploying MktScan on Railway

Three Railway services in one project, from one repo:

| Service | What it is | Dockerfile | Replicas |
|---|---|---|---|
| **postgres** | Railway's managed Postgres | — | 1 |
| **scheduler** | Background worker. Scrapes, scores, owns the schema. | `Dockerfile.scheduler` | **1 — do not scale** |
| **dashboard** | Streamlit web UI, public URL | `Dockerfile` | 1 |

Two things to know before you start, because they cause most first-deploy failures:

- **Postgres is mandatory, not optional.** The two services run in separate containers with separate filesystems. A SQLite file cannot be shared between them — each would silently get its own empty database and the dashboard would show nothing while the scheduler happily scraped into the void.
- **Only the scheduler runs migrations.** If both services ran `alembic upgrade head` at boot they would contend for the same Postgres DDL lock, and the loser can end up with a half-applied migration. `start-dashboard.sh` waits for the schema instead.

Budget roughly **20 minutes**, plus 10–15 minutes of unattended IV backfill on first boot.

---

## Step 1 — Push the repo to GitHub

```bash
cd mktscan-main
git init
git add .
git commit -m "MktScan"
git remote add origin git@github.com:<you>/mktscan.git
git push -u origin main
```

Before pushing, confirm you are not committing secrets:

```bash
git ls-files | grep -E 'config\.local\.yaml|\.env$'   # must return nothing
grep -n 'password\|webhook\|api_key' config.yaml      # must show only empty strings
```

`config.yaml` ships with every credential blank; they all come from environment variables. If you had previously pasted a real key in there, rotate it — it is in your git history now.

---

## Step 2 — Create the project and Postgres

1. Go to **railway.app** → **New Project** → **Deploy from GitHub repo** → pick your repo.
2. Railway creates one service. Rename it **`scheduler`** (Settings → Service Name).
3. In the project canvas: **New** → **Database** → **Add PostgreSQL**.

Railway now exposes `DATABASE_URL` as a project variable. `mktscan/config.py` reads it, rewrites the legacy `postgres://` prefix that SQLAlchemy 2.x rejects, and switches storage to Postgres automatically. You do not need to set `storage.type`.

---

## Step 3 — Configure the scheduler service

The repo already contains the build and deploy settings as code, in `services/scheduler/railway.toml`. Point the service at it:

**Settings → Config-as-code → Railway Config File**

```
/services/scheduler/railway.toml
```

The leading slash matters. Railway only auto-detects `railway.toml` at the repo root, and the config path deliberately does **not** follow the Root Directory setting — so a file in a subdirectory is ignored unless you give the absolute path. Config as code overrides whatever is set in the dashboard UI, so once this is set you configure the build and deploy behaviour by editing the toml, not by clicking.

That file sets:

| Field | Value |
|---|---|
| Builder | `dockerfile` |
| Dockerfile Path | `Dockerfile.scheduler` |
| Start Command | `./scripts/start-scheduler.sh` |
| Restart Policy | On failure, max 3 |
| Replicas | **1** |

Keep replicas at 1. The per-day unique constraints would stop duplicate rows, but two schedulers would still hit Yahoo on the same cadence and get you rate limited.

*If you would rather not use config as code*, leave the Railway Config File field blank and set Builder, Dockerfile Path and Start Command manually under **Settings → Build** and **Settings → Deploy** using the same values.

**Variables** — click *Raw Editor* and paste:

```bash
DATABASE_URL=${{Postgres.DATABASE_URL}}
MKTSCAN_SENTIMENT_MODEL=vader
MKTSCAN_SCHEDULE=*/30 13-21 * * 1-5
MKTSCAN_LOG_LEVEL=INFO
TZ=UTC
```

`${{Postgres.DATABASE_URL}}` is Railway's reference syntax — it links to the database service so the URL updates automatically if credentials rotate. If your database service is named something other than `Postgres`, match that name.

The schedule is UTC and covers US market hours on weekdays only. Overnight and weekend runs re-fetch data that has not changed.

---

## Step 4 — Add your API keys

All optional — Yahoo Finance and the free RSS feeds work with no keys at all. Add whichever you have:

```bash
MKTSCAN_AV_KEY=...            # alphavantage.co, free tier
MKTSCAN_FINNHUB_KEY=...       # finnhub.io, free tier
MKTSCAN_BENZINGA_KEY=...      # Benzinga Pro, ~$50/mo
MKTSCAN_FINVIZ_COOKIE=...     # FinViz Elite session cookie
MKTSCAN_WSJ_COOKIE=...        # WSJ+ session cookie
MKTSCAN_SMTP_PASSWORD=...     # Gmail app password, for alerts
MKTSCAN_SLACK_WEBHOOK=...
```

Anything left unset is blanked at load time, which disables that source cleanly rather than sending the string `YOUR_AV_KEY` to a live API and logging a failure every run.

Session cookies expire in 30–90 days. When FinViz or WSJ stops returning articles, that is the first thing to check.

---

## Step 5 — Deploy the scheduler and watch the first boot

Trigger a deploy and open the logs. Expect this sequence:

```
── MktScan scheduler starting ──────────────────────────────
→ Applying database migrations...
INFO  [alembic.runtime.migration] Running upgrade  -> 0001, Options pipeline fixes
→ Seeding basket if empty...
→ Checking implied-volatility history...
   no IV history — seeding 19 tickers (this takes a few minutes)
[IV] Backfilling proxy history for 19 tickers (365d)...
[IV]   AAPL: stored 251 proxy snapshots
...
[IV] snapshots updated for 19/19 tickers
→ Starting scheduler
MktScan Scheduler — cron: */30 13-21 * * 1-5 (UTC)
  + daily IV snapshot at 21:15 UTC (weekdays)
  + weekly backtest Sunday 02:00 UTC
Running initial scrape on startup...
```

The IV seed runs once and takes 10–15 minutes — it pulls a year of price history plus a live option chain per ticker. Subsequent boots skip it.

If you see `WARNING: DATABASE_URL is not set`, the variable reference did not resolve. Go back to Step 3.

---

## Step 6 — Add the dashboard service

In the project canvas: **New** → **GitHub Repo** → select the *same* repo again. Rename it **`dashboard`**.

**Settings → Config-as-code → Railway Config File**

```
/services/dashboard/railway.toml
```

That file sets:

| Field | Value |
|---|---|
| Builder | `dockerfile` |
| Dockerfile Path | `Dockerfile` |
| Start Command | `./scripts/start-dashboard.sh` |
| Health Check Path | `/_stcore/health` |
| Health Check Timeout | `300` |

The healthcheck is `/_stcore/health`, not `/`. Streamlit serves the SPA shell at `/` before the app has finished starting, so Railway would mark the service healthy while it is still booting.

**Variables**

```bash
DATABASE_URL=${{Postgres.DATABASE_URL}}
MKTSCAN_SENTIMENT_MODEL=vader
TZ=UTC
```

Then **Settings → Networking → Generate Domain** to get a public URL.

---

## Step 7 — Verify

Open the dashboard URL and check, in order:

1. **Sidebar** — "Companies" shows 19, "Total Articles" is non-zero, "Last Run" is recent. If articles are 0, the scheduler has not completed a scrape yet; wait for the next 30-minute tick.
2. **Tradeability page** — no orange *"IV rank unavailable for the whole basket"* banner. If it appears, Step 5's IV seed did not finish; see troubleshooting.
3. **Trade Setups** — expand a card. It should show real bid/ask per leg, a dollar max loss, a breakeven and a probability of profit. Any leg showing `—` for bid/ask means the chain fetch failed.

From your terminal, using the Railway CLI:

```bash
npm i -g @railway/cli && railway login && railway link

railway run --service scheduler python -m mktscan iv          # IV rank table
railway run --service scheduler python -m mktscan scores      # sentiment scores
railway run --service scheduler python -m mktscan setups      # priced setups
```

In the IV table, `basis` should read `chain` or `proxy`. If it reads `none`, there is no history.

---

## Step 8 — Seed the backtest (optional)

The weekly backtest runs Sunday 02:00 UTC. To populate it immediately:

```bash
railway run --service scheduler python -c "
from mktscan.database import get_session, get_basket
from mktscan.backtest_incremental import run_incremental_backtest
s = get_session()
print(run_incremental_backtest(s, [c.ticker for c in get_basket(s)]))
"
```

First run pulls 5 years per ticker and takes several minutes. Read the **Excess vs buy-and-hold** column, not the raw win rate — a 55% win rate is not an edge if the universe rose on 55% of days anyway.

---

## Sentiment model: pick one

| Option | Setting | Cost | Notes |
|---|---|---|---|
| **VADER** (default) | `MKTSCAN_SENTIMENT_MODEL=vader` | Free | Weaker on financial jargon. **Recommended to start.** |
| **OpenAI** | `MKTSCAN_SENTIMENT_MODEL=openai` + `MKTSCAN_OPENAI_KEY=sk-...` | ~$0.001/batch | FinBERT-grade quality, no heavy dependency. Best value. |
| **FinBERT** | Build with `--build-arg REQUIREMENTS=requirements.txt` | Free, but ~2.5 GB image + ~2 GB RAM | Slow builds, likely to exceed Railway's limits. |

`requirements-railway.txt` omits torch and transformers for exactly this reason. `build_scorer()` falls back to VADER automatically when transformers is missing, so nothing breaks.

Worth keeping in perspective: sentiment carries 12% of the composite weight. Going from VADER to FinBERT moves the final score less than most people expect.

---

## Cost

Railway bills by usage. Rough monthly estimate:

| Service | Est. |
|---|---|
| Postgres (small volume) | ~$5 |
| Scheduler (always on, small) | ~$5–8 |
| Dashboard (always on, idles cheap) | ~$5 |
| **Total** | **~$15–20/mo** |

To cut it: narrow `MKTSCAN_SCHEDULE` to `0 14,20 * * 1-5` (twice daily). The tool records one prediction per ticker per day regardless of how often it runs, so a 30-minute cadence buys fresher news and nothing else.

---

## Troubleshooting

**`relation "companies" does not exist`** — Migrations have not run. Only the scheduler runs them; check its logs for the alembic line. To apply manually:
```bash
railway run --service scheduler alembic upgrade head
```

**Dashboard shows "No data yet"** — Usually the two services are pointed at different databases. Confirm both have `DATABASE_URL` set and that they resolve to the same value:
```bash
railway variables --service dashboard | grep DATABASE_URL
railway variables --service scheduler | grep DATABASE_URL
```

**"IV rank unavailable for the whole basket"** — The backfill did not complete. Run it manually:
```bash
railway run --service scheduler python -m mktscan iv --backfill
railway run --service scheduler python -m mktscan iv --update
```
Until this succeeds, strategy selection runs on its fallback branch: defined-risk debit spreads at half size, regardless of the actual volatility regime. The suggestions are not wrong, but they are uninformed about whether premium is cheap or rich.

**Build fails, out of disk / times out** — You are building the full `requirements.txt` with torch. Confirm the Dockerfile's `ARG REQUIREMENTS` still defaults to `requirements-railway.txt`.

**Yahoo rate limiting (`YFRateLimitError`, empty results)** — Loosen the cadence and add a delay:
```bash
MKTSCAN_SCHEDULE=0 14,20 * * 1-5
MKTSCAN_DELAY_SECONDS=3.0
```
Also confirm the scheduler is on 1 replica.

**Scheduler restart loop** — Check the logs for the failing step. A common cause is the IV backfill exceeding the container's start-period; the healthcheck allows 300s, but if Yahoo is slow the process can be killed mid-seed. Run the backfill manually via `railway run` and redeploy.

**Health check failing on the scheduler** — `scheduler.py` serves plain text on `$PORT`. Railway only injects `$PORT` for services with a domain; without one it falls back to 8080, which is fine. If Railway insists on a healthcheck, leave the path blank for a worker service.

---

## After deploying

Three things worth doing in the first fortnight:

1. **Check the IV basis after ~60 trading days.** Until then the rank is `proxy` (realised volatility), which the strategy layer deliberately treats as unknown. Real IV ranking only begins once 60 daily chain snapshots have accumulated.
2. **Watch the accuracy panel, do not act on it yet.** It needs 30 independent observations — one per ticker per trading day — before the numbers mean anything, and score adjustment is off by default.
3. **Rotate session cookies** when FinViz/WSJ go quiet.

And the standing caveat: this generates research signals from delayed public data. Verify every quote with your broker before trading.
