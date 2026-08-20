# MktScan Dashboard v2.1

This update adds metric provenance/interpretation tooltips, a unified Key Events calendar, and near-real-time 10Y/30Y Treasury yield cards on Today.

No database migration is required.

## Railway
Deploy the dashboard service from `services/dashboard/railway.toml`. The dashboard watch paths must include `dashboard/**`.

## Data sources
- 10Y Treasury: Yahoo Finance / CBOE `^TNX`; fallback to persisted MktScan regime 10Y.
- 30Y Treasury: Yahoo Finance / CBOE `^TYX`.
- Economic events: existing persisted `macro_events` table, including each row's stored source.
- Earnings: existing persisted `earnings_events` table, populated by the Yahoo earnings pipeline.
