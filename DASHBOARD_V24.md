# MktScan Dashboard v2.4

## Changes

### Today
- Removed Open P&L and Capital at Risk from the Today command-center cards.
- Today now keeps only Market Regime, VIX, 10Y Treasury and 30Y Treasury at the top.

### Market Performance
- New top-level **Market Performance** page.
- Restores the old-dashboard rolling price-history experience:
  - entire configured basket
  - 14 trading-day daily-return heatmap
  - 2-week compounded return ranking
  - up/down day counts
  - best/worst day
  - average daily return
- Yahoo Finance adjusted daily prices, cached for 15 minutes.

### Key Events / Economic Calendar reliability
- MarketWatch remains the primary economic-calendar provider.
- Added a second parser for current MarketWatch table text/layout changes.
- An empty Key Events month now self-heals by refreshing the economic calendar automatically.
- Added Benzinga Economic Calendar as a fallback when a configured key has that entitlement.
- The manual button is now **Refresh Economic Calendar** and reports whether either provider returned rows.
- Scheduler full runs use the same resilient refresh function.

### Analyst Ratings reliability
- Corrected Benzinga Ratings API date filter parameters to the documented
  `parameters[date_from]` and `parameters[date_to]`.
- Batched basket/open-position symbols into Benzinga requests (up to 50 symbols).
- Added Yahoo Finance upgrades/downgrades as an explicitly labeled fallback when
  Benzinga is not configured, not entitled, or returns no events.
- Added a manual **Refresh analyst data** control and source/provider diagnostics
  inside Research → Analyst Activity.
- Scheduler now performs one forced analyst seed after deployment, then continues
  every 15 minutes during the U.S. regular session.
- Existing `analyst_rating_events` table and journal snapshot behavior are retained.

## Database
No new migration is required beyond the existing Analyst Ratings v1 migration `0005`.
