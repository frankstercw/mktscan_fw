# MktScan — Market Intelligence & Options Setup Engine

A self-hosted system that scrapes stock news, earnings and market data, scores a
composite tradeability signal, and turns that signal into concrete options trade
setups priced against the live option chain.

---

## Architecture

```
config.yaml + env vars      ← schedule, basket (secrets come from the environment)
    │
    ▼
ScrapeEngine                ← orchestrates all sources per ticker
    ├── YahooScraper            (yfinance, free)
    ├── AlphaVantageScraper     (REST API, free tier)
    ├── BenzingaScraper         (Pro API, ~$50/mo)
    ├── FinVizScraper           (HTML scrape, Elite ~$40/mo)
    ├── WSJScraper              (session cookie, WSJ+ ~$39/mo)
    └── wire RSS feeds          (WSJ / NYT / MarketWatch, free)
           │
           ▼
    SentimentEngine         ← FinBERT / VADER / OpenAI, deduped by headline
           │
           ▼
    SQLite / Postgres       ← articles, scores, prices, earnings, IV history
           │
           ▼
    cross_section.py        ← ranks each raw feature within the basket
           │
           ▼
    tradeability.py         ← 9 weighted signal categories → composite score
           │
           ├── iv_rank.py       ← IV rank from stored option-chain history
           ▼
    strategy.py             ← direction × IV regime → strategy spec
           │
           ▼
    options.py + pricing.py ← live chain, liquidity filter, delta-based strikes,
           │                   Black-Scholes greeks, real premium / max loss / R/R
    ┌──────┴──────┐
    ▼             ▼
Streamlit       CLI (run / schedule / scores / iv / setups / dashboard)
Dashboard
```

---

## What the score is, and is not

The composite is a **cross-sectional ranking tool**. It scores each name relative
to the rest of the basket on the day, not against fixed thresholds — a stock at
the 90th percentile of 52-week position scores the same whether the whole market
is at highs or in a drawdown.

Two things it deliberately does not claim:

- **The weights are not fitted.** Nine weights cannot be estimated on nineteen
  tickers without overfitting, so they encode a prior about relative reliability
  and nothing more. Treat differential weighting as unvalidated until the
  backtest shows out-of-sample evidence for it.
- **Categories with no data are excluded, not treated as neutral.** Each result
  carries a `coverage` figure — the fraction of model weight that actually had
  data behind it. A score built on 40% coverage deserves less confidence than one
  built on 90%, and the dashboard shows which is which.

---

## Quick Start

### 1. Install dependencies

```bash
pip install -r requirements.txt

# For FinBERT (downloads ~440MB model on first run — cached after):
# Happens automatically when you first run with model: finbert

# For Playwright (JS-heavy pages):
playwright install chromium
```

### 2. Configure

Non-secret settings live in `config.yaml`. **Secrets go in the environment** —
never in the YAML, which is committed:

```bash
export MKTSCAN_AV_KEY="..."          # free at alphavantage.co
export MKTSCAN_BENZINGA_KEY="..."    # benzinga.com/apis
export MKTSCAN_FINNHUB_KEY="..."     # free at finnhub.io
export MKTSCAN_FINVIZ_COOKIE="..."   # FinViz Elite session cookie
export MKTSCAN_WSJ_COOKIE="..."      # WSJ+ session cookie
export MKTSCAN_OPENAI_KEY="sk-..."   # only if sentiment.model = openai
export MKTSCAN_SMTP_PASSWORD="..."   # Gmail app password, for alerts
export MKTSCAN_SLACK_WEBHOOK="..."
export DATABASE_URL="postgresql://…" # overrides storage.* entirely
```

Any credential left unset is blanked at load time, which disables its source
rather than sending a placeholder string to a live API.

Behavioural settings in `config.yaml`:

```yaml
scraper:
  schedule: "*/30 13-21 * * 1-5"   # every 30 min during US market hours
sentiment:
  model: finbert                   # finbert | vader | openai
storage:
  type: sqlite                     # or postgres
```

### 2b. Apply migrations

```bash
alembic upgrade head
```

Required on any database created before this version. `create_all()` only ever
creates whole tables — it cannot add a column — so without this an existing
database silently lacks every column added since it was first created, and the
code reads those columns through `getattr(..., None)`, which returns `None`
instead of raising. That is how the IV-rank pipeline stayed broken.

### 3. Add companies to your basket

```bash
# Add individual companies
python -m mktscan add AAPL "Apple Inc." --sector Technology --keywords "Apple, iPhone, Tim Cook"
python -m mktscan add NVDA "NVIDIA Corp." --sector Semiconductors --keywords "NVIDIA, H100, Blackwell"

# Or manage via the dashboard (see step 5)
```

### 4. Run a scrape

```bash
# Single run (all sources + sentiment scoring)
python -m mktscan run

# News only (faster)
python -m mktscan run --mode news

# Prices + earnings only
python -m mktscan run --mode earnings

# View results in terminal
python -m mktscan scores
```

### 5. Seed implied-volatility history — do this before trusting any setup

```bash
python -m mktscan iv --check      # verify/repair schema
python -m mktscan iv --backfill   # seed history (run once)
python -m mktscan iv --update     # today's ATM IV from the option chain
```

IV rank is what decides between **buying** premium (debit spreads when options
are cheap) and **selling** it (credit spreads when they are rich). Without
history there is no rank, and the strategy selector falls back to its
least-informed branch — defined-risk debit spreads at reduced size — regardless
of what the volatility regime actually is. The dashboard warns when this is
happening.

The backfill seeds from 30-day realised volatility, because yfinance exposes only
the *current* option chain and historical IV cannot be reconstructed. Those rows
are marked `source="proxy"` and are **never** ranked against true chain IV: IV
sits structurally above realised vol, so mixing them would pin every reading near
the top of the range. After ~60 daily `--update` runs the rank switches to
ranking real IV against real IV.

### 6. Generate trade setups

```bash
python -m mktscan setups              # whole basket
python -m mktscan setups --ticker NVDA
```

### 7. Launch the dashboard

```bash
streamlit run dashboard/app.py    # http://localhost:8501
```

### 8. Schedule recurring runs

```bash
python -m mktscan schedule
```

Runs three jobs: the scrape on the configured cron, a **daily** IV snapshot at
21:15 UTC (just after the US close), and a weekly backtest on Sunday.

---

## Data Sources

| Source | What you get | Cost | Method |
|--------|-------------|------|--------|
| **Yahoo Finance** | Prices, news, earnings calendar | Free | yfinance library |
| **Alpha Vantage** | Prices, earnings, news sentiment | Free (25 req/day) / Premium | REST API |
| **Benzinga Pro** | News, earnings, analyst ratings | ~$50/mo | REST API |
| **FinViz** | Screener, news, analyst actions | Elite ~$40/mo for full | HTML scrape |
| **Wall Street Journal** | Long-form news and analysis | WSJ+ ~$39/mo | Session cookie scrape |

**Recommended minimum:** Yahoo Finance + Alpha Vantage (free) gives you a solid
baseline. Add Benzinga Pro for earnings quality. Add FinViz Elite for screener data.

---

## Sentiment Models

| Model | Quality | Speed | Cost | Notes |
|-------|---------|-------|------|-------|
| **FinBERT** | ★★★★★ | Medium | Free | Best for financial text. Downloads 440MB model. Needs torch. |
| **VADER** | ★★★☆☆ | Fast | Free | No download. Weaker on jargon. Good for testing. |
| **OpenAI GPT-4o-mini** | ★★★★★ | Fast | ~$0.001/article | Highest quality. Requires API key. |

### Sentiment Score Scale

```
+1.0  ━━━━━━━━━━━ Very Bullish
+0.2  ─────────── BULLISH threshold
 0.0  ─────────── Neutral
-0.2  ─────────── BEARISH threshold
-1.0  ━━━━━━━━━━━ Very Bearish
```

The band is symmetric. It used to be +0.3 / -0.1, which labelled a -0.15 reading
BEARISH while its mirror image at +0.15 was NEUTRAL — a systematic bearish bias
that propagated into every count of "how many tickers are bearish today".

Articles are deduplicated by **normalised headline**, not just URL. A wire story
republished by six outlets under six URLs previously counted six times — moving
the weighted mean six times over *and* inflating the source-diversity bonus that
is supposed to reward genuinely independent confirmation.

Source weights applied during aggregation:
- WSJ: 1.5× (editorial quality)
- Reuters-tagged wire feeds: 1.3×
- Benzinga / Finnhub: 1.2×
- MarketWatch: 1.1×
- Yahoo / Alpha Vantage: 1.0×
- FinViz: 0.9× (shorter snippets)

---

## Options strategy selection

Direction comes from the composite score; structure comes from the IV regime.

| Direction | IV rank | Strategy | Why |
|---|---|---|---|
| Bullish | < 30 | Bull call spread | Premium cheap — buy it, cap the cost |
| Bullish | 30–70 | Bull call spread | Defined risk, short leg cuts the breakeven |
| Bullish | > 70 | Bull put spread | Premium rich — sell it; wins if the stock merely holds |
| Bearish | < 30 | Bear put spread | Puts cheap — debit spread caps the cost of being early |
| Bearish | 30–70 | Bear put spread | Defined risk |
| Bearish | > 70 | Bear call spread | Collect elevated premium; wider win zone than a long put |
| Neutral | > 70 | Iron condor | No side, but premium worth selling |
| Neutral | ≤ 70 | No trade | Nothing to buy, nothing worth selling |
| Any | Any | **Avoid** | Earnings within 3 days |
| Any | unknown | Debit spread, half size | Cannot tell if premium is cheap or rich |

Strikes are selected by **target delta** from the live chain, not by a fixed OTM
percentage — "2% OTM" is a 0.45 delta on a low-vol name and a 0.30 delta on a
high-vol one, so a fixed percentage produces structures with completely different
risk profiles while calling them the same trade.

Default expiry is 30–45 DTE, and it is pushed past any known earnings date.

Every leg must clear a liquidity screen (open interest ≥ 100, bid ≥ $0.05,
bid/ask spread ≤ 10% of mid) or the setup is rejected as untradeable rather than
quoted at a price you could not get filled at.

**Every reported figure is option-level**: premium, max loss, max profit,
breakeven, probability of profit, net delta/theta/vega, and a risk/reward that
reprices the structure with time decay at the target and stop.

---

## CLI Reference

```bash
# Run scraper
python -m mktscan run [--mode all|news|earnings|prices] [--config path/to/config.yaml]

# Start scheduler (scrape + daily IV snapshot + weekly backtest; blocks)
python -m mktscan schedule

# Implied volatility history — required for IV rank
python -m mktscan iv --check       # inspect / repair schema
python -m mktscan iv --backfill    # seed history (once)
python -m mktscan iv --update      # today's ATM IV
python -m mktscan iv --ticker NVDA # show one ticker's rank

# Priced options trade setups
python -m mktscan setups [--ticker NVDA]

# Show latest sentiment scores
python -m mktscan scores [--days 30]

# Manage basket
python -m mktscan add TICKER "Company Name" [--sector S] [--keywords "kw1, kw2"]
python -m mktscan basket

# Launch dashboard
python -m mktscan dashboard
```

---

## Project Structure

```
mktscan/
├── config.yaml                ← template config (copy to config.local.yaml)
├── requirements.txt
├── setup.py
├── pytest.ini
│
├── mktscan/
│   ├── __init__.py
│   ├── __main__.py            ← enables python -m mktscan
│   ├── clock.py               ← market-time helpers (US/Eastern vs UTC)
│   ├── config.py              ← config loader, secrets from env
│   ├── database.py            ← SQLAlchemy models + query helpers
│   ├── engine.py              ← scrape orchestration + persistence
│   ├── sentiment.py           ← FinBERT / VADER / OpenAI, headline dedup
│   ├── cross_section.py       ← rank features within the basket
│   ├── tradeability.py        ← 9 signal categories → composite score
│   ├── iv_rank.py             ← IV history + IV rank
│   ├── strategy.py            ← the single strategy selector
│   ├── pricing.py             ← Black-Scholes pricing and greeks
│   ├── options.py             ← chain access, liquidity, priced setups
│   ├── feedback.py            ← daily prediction tracking + accuracy
│   ├── backtest_incremental.py← historical replay of the real signal
│   ├── scheduler.py           ← APScheduler jobs
│   ├── alerts.py              ← email + Slack alerting
│   ├── cli.py                 ← Click CLI
│   └── scrapers/              ← yahoo, alphavantage, benzinga, finviz,
│                                wsj, marketwatch, reuters, finnhub
│
├── dashboard/app.py           ← Streamlit dashboard (9 pages)
├── migrations/                ← Alembic
├── data/                      ← SQLite database
├── logs/
└── tests/
    ├── conftest.py
    ├── test_scrapers.py       ← scraper + sentiment unit tests
    └── test_options_pipeline.py ← pricing, strategy, ranking, signal tests
```

---

## Database Schema

```
companies             — watch basket (ticker, name, sector, keywords)
articles              — headlines + snippets, deduped by URL and headline hash
sentiment_scores      — aggregated score per ticker per run
price_snapshots       — EOD price, fundamentals, IV rank
earnings_events       — upcoming + historical earnings, surprise as a real %
iv_snapshots          — daily ATM IV per ticker (the basis for IV rank)
tradeability_outcomes — one prediction per ticker per day + realised outcome
backtest_observations — historical ticker-days with forward and option returns
scraper_runs          — audit log of every execution
```

Use PostgreSQL for production (`storage.type: postgres`, or set `DATABASE_URL`).
Schema changes go through Alembic — `alembic upgrade head`.

SQLite runs in WAL mode with a 30-second busy timeout, because the dashboard and
scheduler processes share the file and would otherwise deadlock on writes.

---

## Getting Session Cookies (FinViz / WSJ)

### FinViz Elite
1. Log into `finviz.com` with your Elite account
2. Open DevTools → Network → reload any page → click the request → Headers
3. Copy the entire `Cookie:` header value
4. Paste into `config.local.yaml` → `sources.finviz.session_cookie`

### Wall Street Journal
1. Log into `wsj.com` with your WSJ+ account
2. Open DevTools → Application → Cookies → `wsj.com`
3. Copy the full cookie string (all cookies as `key=value; key2=value2`)
4. Paste into `config.local.yaml` → `sources.wsj.session_cookie`

Session cookies typically expire after 30–90 days and need to be refreshed.

---

## Alerts

```yaml
alerts:
  enabled: true
  sentiment_threshold_bull: 0.6   # fires when score > 0.6
  sentiment_threshold_bear: -0.3  # fires when score < -0.3

  email:
    smtp_host: "smtp.gmail.com"
    smtp_port: 587
    from_addr: "you@gmail.com"
    to_addr:   "you@gmail.com"
    password:  "your-app-password"  # use Gmail App Password, not main password

  slack_webhook: "https://hooks.slack.com/services/..."
```

---

## Running Tests

```bash
# Install test dependencies
pip install pytest responses

# Run all tests
pytest tests/ -v

# Run a specific test class
pytest tests/test_scrapers.py::TestSentiment -v

# Run with coverage
pip install pytest-cov
pytest tests/ --cov=mktscan --cov-report=term-missing
```

---

## Environment Variables

All secrets can be passed as environment variables instead of hardcoding in config:

```bash
export MKTSCAN_AV_KEY="your-alphavantage-key"
export MKTSCAN_BENZINGA_KEY="your-benzinga-key"
export MKTSCAN_FINVIZ_COOKIE="your-finviz-cookie"
export MKTSCAN_WSJ_COOKIE="your-wsj-cookie"
export MKTSCAN_OPENAI_KEY="sk-..."
export MKTSCAN_CONFIG="/path/to/config.yaml"
```

---

## Interpreting the numbers honestly

**The backtest** replays the production signal functions over historical bars, so
it can no longer silently diverge from the live model. But categories that need
live data — news sentiment, analyst targets, short interest — cannot be
reconstructed historically, so each observation records its `coverage`. It also
simulates option P&L on the structure the strategy layer would have picked, net
of an assumed 4% round-trip spread, using realised volatility as the IV estimate.
That last substitution ignores the variance risk premium and any IV change over
the holding period, so it will tend to flatter debit spreads. Read the
`excess_return_pct` column, not the raw win rate: a 55% win rate is not an edge
if the universe rose on 55% of days anyway.

**The accuracy panel** records one prediction per ticker per trading day and
resolves it over five trading days. Direction accuracy is shown with its binomial
standard error — on 30 observations that is roughly ±9 percentage points, so
treat anything inside 41–59% as indistinguishable from chance. Score adjustment
based on this feedback is **disabled by default**; the statistics are displayed
but do not alter the scores you see.

**Known limitations.** Historical option chains are unavailable, so IV rank needs
~60 days of live snapshots before it is measuring implied rather than realised
volatility. The basket is survivorship-biased (it is today's list). Black-Scholes
assumes European exercise, lognormal returns and constant volatility — fine for
strike selection and approximate P&L, not for judging whether an option is
mispriced.

---

## Legal & ToS Notes

- **Yahoo Finance / yfinance**: Public data, no scraping required. Personal use OK.
- **Alpha Vantage**: Fully licensed API with official terms.
- **Benzinga Pro API**: Official paid API with permissive terms for subscribers.
- **FinViz Elite**: Elite subscribers may access data programmatically for personal use.
  Review their current ToS before commercial deployment.
- **Wall Street Journal**: WSJ+ subscribers may access content they pay for.
  Scraping at scale may violate ToS — use responsibly and at low frequency.

Always add polite delays (`delay_seconds: 2.5+`) and respect `robots.txt`.
