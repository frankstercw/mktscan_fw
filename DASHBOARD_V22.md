# MktScan Dashboard v2.2

## New
1. **MarketWatch major economic events**
   - Economic-calendar parser updated for the current MarketWatch table schema:
     `Time (ET) | Report | Period | Actual | Forecast | Previous`.
   - MarketWatch ET timestamps are normalized to UTC before persistence.
   - Key Events defaults to High/Medium macro events plus basket earnings.
   - A manual **Refresh MarketWatch** action is available on Key Events.
   - Scheduler refresh remains in place.

2. **Analyze any ticker**
   - Sidebar accepts an arbitrary supported ticker.
   - Runs an ephemeral MktScan review without adding the ticker to the scheduled basket.
   - Review includes Yahoo price/fundamental/earnings data, Yahoo + MarketWatch news
     sentiment, tradeability categories, daily momentum, technical opportunity,
     live Yahoo option-surface analytics when options exist, trade construction,
     Alpaca charting, ChatGPT research handoff, and Advanced diagnostics.
   - Basket-relative cross-sectional ranking is omitted for ad-hoc symbols because
     they are intentionally outside the basket.
   - IV Rank/Percentile can be unavailable on first review because those metrics
     require stored historical IV observations.

## Database
No migration is required.

## Railway
The dashboard service should use `services/dashboard/railway.toml` and watch
`dashboard/**` and `mktscan/**`, because v2.2 changes both paths.
