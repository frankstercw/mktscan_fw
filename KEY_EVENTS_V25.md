# Key Events v2.5

The Key Events page now explicitly restores both legacy MktScan calendars:

- **Economic Calendar**
  - MarketWatch primary
  - Benzinga Economics fallback when available
  - Date, UTC time, event, category, importance, consensus, prior, actual, source
  - Full calendar by default; optional High/Medium filter

- **Earnings Calendar**
  - Yahoo Finance for the configured MktScan basket
  - Upcoming earnings date and EPS estimate
  - Manual refresh independent of the full scraper

Views:
1. Combined Calendar
2. Economic Calendar
3. Earnings Calendar

No database migration is required.
