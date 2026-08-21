# MktScan Analyst Ratings v1

## Included
- `analyst_rating_events` PostgreSQL/SQLAlchemy table.
- Benzinga Analyst Ratings API provider using `/api/v2/calendar/ratings`.
- Scheduler refresh every 15 minutes during the 09:30–16:00 America/New_York regular session.
- Scheduler universe is limited to the configured basket plus tickers with OPEN Trade Journal positions.
- Research → Analyst Activity with recent firm actions, rating changes, target changes, Benzinga importance, and a transparent 30-day Analyst Momentum state.
- Analyst events appear in Today → What changed.
- New Trade Journal entries freeze Analyst Momentum at entry.
- Validation can attribute closed-trade performance by Analyst Momentum at entry.

## Analyst Momentum v1
- Upgrade: +2
- Downgrade: -2
- Bullish/Bearish initiation or reinstatement: ±1.5
- Price-target raise: +1
- Price-target cut: -1

States:
- >= +4: STRONGLY POSITIVE
- >= +1.5: POSITIVE
- between -1.5 and +1.5: NEUTRAL
- <= -1.5: NEGATIVE
- <= -4: STRONGLY NEGATIVE

The score is deliberately not part of Tradeability yet. Store and validate it first.

## Railway setup
Set `MKTSCAN_BENZINGA_KEY` on the **scheduler** service. The dashboard reads analyst events from PostgreSQL and does not need the API key.

Optional:
`MKTSCAN_ANALYST_SCHEDULE=*/15`

A database migration (`0005_analyst_ratings.py`) is included and should be applied with the existing `alembic upgrade head` deployment step.
