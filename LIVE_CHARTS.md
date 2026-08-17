# Live Charts v1

MktScan's live stock-chart layer is intentionally separate from the daily
research database.

## Architecture

```text
Alpaca Market Data REST
        |
        v
mktscan/providers/alpaca.py
        |
        v
LiveStockQuote + normalized OHLCV DataFrame
        |
        +--> mktscan/live_charts.py (EMA 9/20, VWAP, RVOL)
        |
        v
Streamlit / Live Charts
```

No database migration is required. Intraday bars remain transient/cached; the
existing historical/daily data remains the source for backtesting.

## Data ranges

| UI range | Alpaca aggregation |
|---|---|
| 1D | 1Min |
| 5D | 5Min |
| 1M | 30Min |
| 3M | 1Hour |
| 6M | 1Day |
| 1Y | 1Day |

The 1D request looks back several calendar days and then keeps the most recent
trading session, so weekends and holidays do not leave the chart empty.

## Railway

Add these variables to the **dashboard** service:

```text
ALPACA_API_KEY=<key>
ALPACA_SECRET_KEY=<secret>
ALPACA_DATA_FEED=iex
```

Use `sip` only if the account has SIP market-data entitlement. The chart page
uses Alpaca's stock snapshot and historical-bars REST endpoints.

The release raises the Streamlit minimum to `>=1.37.0` because auto-refresh uses
`st.fragment(run_every=...)`. Auto refresh is user-controlled and defaults to 15
seconds.
