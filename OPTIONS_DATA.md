# MktScan Options Data v1 / Backtest v2 / Options Market v2

## Provider contract

MktScan now normalizes vendor payloads before they reach strategy/backtest code.
`mktscan/providers/base.py` defines `OptionQuote`; `mktscan/providers/orats.py`
implements ORATS historical EOD data. This keeps the future Intrinio live adapter
independent from ORATS research data.

## Secret

Set one of these (do not commit it):

```bash
export ORATS_API_TOKEN="..."
# or
export MKTSCAN_ORATS_TOKEN="..."
```

On Railway add `ORATS_API_TOKEN` to the scheduler and dashboard service variables
if you want the dashboard Refresh button to call ORATS. If you prefer the
dashboard to remain read-only, add it only to the scheduler and refresh through
CLI/scheduled jobs.

## Database migration

```bash
alembic upgrade head
```

Migration `0003` adds:

- `historical_option_quotes` — normalized cached ORATS EOD contracts.
- `options_market_snapshots` — IV rank/percentile, 30/60/90d term structure,
  25-delta skew, expected move.
- Backtest v2 provenance/economics columns on `backtest_observations`.

## Options Data v1

Fetch and cache a historical chain:

```bash
python -m mktscan orats-chain AAPL 2024-01-02 --min-dte 21 --max-dte 60
```

The adapter uses ORATS `/hist/strikes` and expands every strike row into a
vendor-neutral call and put `OptionQuote`.

## Backtest v2

Run the normal signal backtest first:

```bash
python -m mktscan backtest
```

Then incrementally replace the synthetic option P&L with actual historical ORATS
quotes:

```bash
python -m mktscan backtest-orats --limit 100
python -m mktscan backtest-orats --ticker NVDA --limit 50
```

Backtest v2 uses the same 0.45-delta long / 0.25-delta short debit-vertical shape
as production's unknown/normal-IV directional fallback. Entry uses the natural
price (long ask minus short bid); exit is conservative (long bid minus short ask).
The exact expiry, strikes, debit, exit value and source are saved on each
observation. Cached chains are reused on later runs.

`--limit` is intentionally required as a bounded batch default because historical
option enrichment can consume two ORATS chain requests per uncached observation.

## Options Market v2

Refresh one ticker or the basket:

```bash
python -m mktscan options-market --ticker NVDA --refresh              # Yahoo
python -m mktscan options-market --refresh                            # Yahoo basket
python -m mktscan options-market --ticker NVDA --refresh --source orats
```

The dashboard has a new **Options Market** page. Yahoo is the default current
source, so you do not need to buy ORATS live data merely to populate the page.
If your ORATS plan includes current/delayed Data API access, choose ORATS instead.
Values are persisted rather than fetched on every Streamlit rerender.

Current fields:

- ATM / 30d / 60d / 90d IV
- 1-year IV rank and percentile
- 30→60 and 60→90 term slopes + state
- approximate 25-delta put/call skew using ORATS delta buckets
- option-implied expected move in percent and dollars
- provider provenance and completeness confidence

## Automatic refresh

Paid API use is off by default. To allow a normal `run --mode all/prices` to
refresh ORATS market snapshots, set in `config.yaml`:

```yaml
options_data:
  enabled: true
  auto_refresh_options_market: true
  live_provider: "yahoo"   # use "orats" only if your ORATS plan includes current data
```

Keep this disabled until you understand your ORATS request quota/cost.

## Future Intrinio live adapter

Do not replace these models when Intrinio is added. Implement a live provider
that normalizes Intrinio contracts into the same internal quote concepts. ORATS
remains the historical/backtest authority; Intrinio becomes the live quote/Greek
source. Provider source fields preserve which feed produced every observation.
