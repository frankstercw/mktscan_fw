# Deploying MktScan on Railway

Three services in one project, from one repo and **one Dockerfile**:

| Service | Role | How it's set |
|---|---|---|
| **Postgres** | Railway managed database | Add from the canvas |
| **scheduler** | Background worker. Scrapes, scores, owns the schema. | `MKTSCAN_ROLE=scheduler` |
| **dashboard** | Streamlit web UI, public URL | `MKTSCAN_ROLE=dashboard` |

Both app services build the same image. Which one a container becomes is decided at runtime by a single environment variable — no Dockerfile paths, no start commands, no config-as-code paths to get wrong.

Two things to know:

- **Postgres is mandatory.** The two services run in separate containers with separate filesystems. A SQLite file cannot be shared — each would silently get its own empty database.
- **Only the scheduler migrates.** Two containers running `alembic upgrade head` against the same Postgres at boot contend for the DDL lock, and the loser can leave a partially applied migration.

---

## 1. Push to GitHub

```
cd <your-project-folder>
git init
git add .
git commit -m "MktScan"
git branch -M main
git remote add origin https://github.com/YOURNAME/YOURREPO.git
git push -u origin main
```

Before pushing, confirm no secrets are going up:

```
git ls-files | grep -E 'config\.local\.yaml|\.env$'   # must return nothing
```

`config.yaml` ships with every credential blank — they all come from environment variables.

## 2. Create the project and database

1. **railway.app** → New Project → Deploy from GitHub repo → your repo.
   If your repo isn't listed, click **Configure GitHub App** and grant access to it, then **Refresh**.
2. Rename the created service to `scheduler` (optional, cosmetic).
3. Canvas → **New** → **Database** → **Add PostgreSQL**.

## 3. Configure the scheduler

Leave **all** build settings at their defaults. Railway will find the root `Dockerfile` on its own.

**Variables** → Raw Editor:

```
MKTSCAN_ROLE=scheduler
DATABASE_URL=${{Postgres.DATABASE_URL}}
MKTSCAN_SENTIMENT_MODEL=vader
MKTSCAN_SCHEDULE=*/30 13-21 * * 1-5
MKTSCAN_LOG_LEVEL=INFO
TZ=UTC
```

`MKTSCAN_ROLE=scheduler` is the important line. If your database service isn't named exactly `Postgres`, change that word to match — Railway autocompletes as you type `${{`.

Deploy, then open the logs.

## 4. Read the boot diagnostics

Every boot prints a `doctor` block. This is the fastest way to tell whether things are working:

```
══════════════════════════════════════════════
 MktScan — role: scheduler
══════════════════════════════════════════════
✓ DATABASE_URL is set
→ Applying database migrations...
✓ Migrations applied

── MktScan doctor ──────────────────────────
  role:            scheduler
  DATABASE_URL:    set (→ postgres.railway.internal:5432/railway)
  dialect:         postgresql
  connection:      OK
  tables:          all 9 present
  companies        19
  articles         0
  ...
  IV rank:         unavailable — run `mktscan iv --backfill`
────────────────────────────────────────────
→ Checking implied-volatility history...
  no IV history — seeding 19 tickers (several minutes)
```

What to look for:

| Line | Meaning |
|---|---|
| `DATABASE_URL: NOT SET` | The `${{Postgres.DATABASE_URL}}` reference didn't resolve. Check the database service's name. |
| `dialect: sqlite` | Same problem — it fell back to a local file. |
| `connection: FAILED` | Database not reachable. Is the Postgres service running? |
| `tables: missing ...` | Migration didn't run. See troubleshooting. |
| `role: dashboard` on the scheduler | `MKTSCAN_ROLE` isn't set. |

The IV seed takes 10–15 minutes on first boot only. Let it finish.

## 5. Add the dashboard

Canvas → **New** → **GitHub Repo** → the *same* repo again. Again leave build settings at defaults.

**Variables**:

```
MKTSCAN_ROLE=dashboard
DATABASE_URL=${{Postgres.DATABASE_URL}}
MKTSCAN_SENTIMENT_MODEL=vader
TZ=UTC
```

Then **Settings → Networking → Generate Domain** for the public URL.

## 6. API keys (all optional)

Yahoo Finance and the free RSS feeds need no keys. Add whichever you have, to the **scheduler** service:

```
MKTSCAN_AV_KEY=...            # alphavantage.co, free
MKTSCAN_FINNHUB_KEY=...       # finnhub.io, free
MKTSCAN_BENZINGA_KEY=...      # ~$50/mo
MKTSCAN_FINVIZ_COOKIE=...     # Elite session cookie
MKTSCAN_WSJ_COOKIE=...        # WSJ+ session cookie
MKTSCAN_SMTP_PASSWORD=...     # Gmail app password, for alerts
MKTSCAN_SLACK_WEBHOOK=...
```

Anything unset is blanked at load, which disables that source cleanly rather than sending the literal string `YOUR_AV_KEY` to a live API every run. Session cookies expire in 30–90 days — that's the first thing to check when FinViz or WSJ goes quiet.

## 7. Verify

On the dashboard: sidebar shows 19 companies and a non-zero article count; the Tradeability page has no orange "IV rank unavailable" banner; a Trade Setup card shows real bid/ask per leg, a dollar max loss and a breakeven.

From your terminal:

```
npm i -g @railway/cli && railway login && railway link

railway run --service scheduler python -m mktscan doctor
railway run --service scheduler python -m mktscan iv
railway run --service scheduler python -m mktscan setups
```

## 8. Seed the backtest (optional)

```
railway run --service scheduler python -c "
from mktscan.database import get_session, get_basket
from mktscan.backtest_incremental import run_incremental_backtest
s = get_session()
print(run_incremental_backtest(s, [c.ticker for c in get_basket(s)]))
"
```

First run pulls 5 years per ticker; several minutes. Read the **Excess vs buy-and-hold** column, not the raw win rate — a 55% win rate is not an edge if the universe rose on 55% of days anyway.

---

## Sentiment model

| Option | Setting | Cost | Notes |
|---|---|---|---|
| **VADER** (default) | `MKTSCAN_SENTIMENT_MODEL=vader` | Free | Weaker on financial jargon. Start here. |
| **OpenAI** | `=openai` + `MKTSCAN_OPENAI_KEY=sk-...` | ~$0.001/batch | FinBERT-grade, no heavy dependency. Best value. |
| **FinBERT** | Build with `--build-arg REQUIREMENTS=requirements.txt` | Free, ~2.5 GB image + ~2 GB RAM | Likely to exceed Railway's limits. |

`requirements-railway.txt` omits torch and transformers for that reason; `build_scorer()` falls back to VADER automatically. Sentiment is 12% of the composite weight, so the difference is smaller than it sounds.

## Cost

Roughly **$15–20/mo** across Postgres + two always-on services. To cut it, set `MKTSCAN_SCHEDULE=0 14,20 * * 1-5` — the tool records one prediction per ticker per day regardless of cadence, so more frequent runs buy fresher news and nothing else.

---

## Troubleshooting

**Start by reading the `doctor` block in the logs.** It answers most of these directly.

**Service is running the wrong role** (e.g. Streamlit starting on the scheduler) — `MKTSCAN_ROLE` isn't set on that service. It defaults to `dashboard`. This was the most common failure with the older two-Dockerfile setup.

**`tables: missing ...`** — the migration didn't run. Apply it manually:
```
railway run --service scheduler alembic upgrade head
```
The scheduler also falls back to `create_all()` automatically if alembic fails, and logs that it did, so check whether that fallback fired.

**`ValueError: Unknown format code 'f' for object of type 'str'`** — an old build. Pull the latest and redeploy; component values are no longer formatted as floats unconditionally.

**Dashboard shows "No data yet"** — the two services are probably on different databases:
```
railway variables --service dashboard | grep DATABASE_URL
railway variables --service scheduler | grep DATABASE_URL
```

**"IV rank unavailable for the whole basket"** — the backfill didn't complete:
```
railway run --service scheduler python -m mktscan iv --backfill
railway run --service scheduler python -m mktscan iv --update
```
Until it succeeds, strategy selection runs on its fallback branch — defined-risk debit spreads at half size, regardless of the actual volatility regime. Not wrong, but uninformed about whether premium is cheap or rich.

**Build fails / out of disk** — you're building the full `requirements.txt` with torch. Confirm the Dockerfile's `ARG REQUIREMENTS` still defaults to `requirements-railway.txt`.

**Yahoo rate limiting** (`YFRateLimitError`, empty results) — loosen the cadence and slow down:
```
MKTSCAN_SCHEDULE=0 14,20 * * 1-5
MKTSCAN_DELAY_SECONDS=3.0
```
Also confirm the scheduler is on 1 replica.

**Postgres migration error mentioning `boolean` and `integer`** — an old build. Fixed; pull latest.

---

## After deploying

1. **Check the IV basis after ~60 trading days.** Until then the rank is `proxy` (realised volatility), which the strategy layer deliberately treats as unknown. Real IV ranking begins once 60 daily chain snapshots exist.
2. **Watch the accuracy panel; don't act on it yet.** It needs 30 independent observations — one per ticker per trading day — before the numbers mean anything, and score adjustment is off by default.
3. **Rotate session cookies** when FinViz/WSJ go quiet.

Standing caveat: this generates research signals from delayed public data. Verify every quote with your broker before trading.
