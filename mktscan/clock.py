"""
mktscan/clock.py
──────────────────────────────────────────────────────────────────────────────
Market-time helpers.

The codebase used ``datetime.utcnow()`` everywhere and compared the result against
dates that are implicitly US/Eastern (earnings report dates, trading days, option
expiries). Those two disagree for 4–5 hours every day, which produced real bugs:

  • At 23:00 UTC on a Monday it is still Monday 19:00 in New York, but
    ``utcnow().date()`` already says Tuesday. "Earnings in 0 days" then meant
    "earnings tomorrow", and the ``days_to_earn <= 3`` avoid-rule fired a day early
    or a day late depending on the hour the scheduler happened to run.

  • The 7-day article freshness cutoff and the outcome-resolution cutoff drifted
    for the same reason.

Everything that compares against a *market* date should go through
``market_date()`` / ``market_now()``. Pure durations (a 48h decay half-life) can
stay in UTC — they are timezone-agnostic.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

try:                                    # Python 3.9+
    from zoneinfo import ZoneInfo
    MARKET_TZ = ZoneInfo("America/New_York")
except Exception:                       # pragma: no cover - fallback for odd builds
    MARKET_TZ = timezone(timedelta(hours=-5))

UTC = timezone.utc

# Regular US equity session, Eastern time.
MARKET_OPEN  = time(9, 30)
MARKET_CLOSE = time(16, 0)


def utc_now() -> datetime:
    """Timezone-aware UTC now. Prefer this over the deprecated ``utcnow()``."""
    return datetime.now(UTC)


def market_now() -> datetime:
    """Current time in US/Eastern, timezone-aware."""
    return datetime.now(MARKET_TZ)


def market_date() -> date:
    """
    Today's date *as the market sees it*.

    This is the value to compare against earnings report dates, option expiries
    and trading days — not ``datetime.utcnow().date()``.
    """
    return market_now().date()


def to_market(dt: datetime | None) -> datetime | None:
    """
    Convert a datetime to US/Eastern.

    Naive datetimes are assumed to be UTC, which is what every naive timestamp in
    this codebase actually is (they all originate from ``utcnow()``).
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(MARKET_TZ)


def as_date(value) -> date | None:
    """
    Coerce a date / datetime / ISO string to a plain ``date``, with **no**
    timezone conversion.

    Use this for *calendar dates* — earnings report dates, option expiries,
    trading-session dates. These are already dates in market terms; they are not
    instants. Running them through a timezone conversion is actively wrong: an
    earnings date stored as midnight (``datetime.combine(d, time.min)``) becomes
    20:00 on the *previous* day in Eastern, shifting the whole date back one and
    throwing off "days to earnings" — and with it the earnings blackout rule.

    For a genuine UTC instant, such as ``predicted_at`` from ``utcnow()``, use
    :func:`as_market_date` instead.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if hasattr(value, "to_pydatetime"):          # pandas Timestamp
        try:
            return value.to_pydatetime().date()
        except Exception:
            return None
    if isinstance(value, str):
        text = value.strip()
        for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"):
            try:
                return datetime.strptime(text[:len(fmt) + 2].strip(), fmt).date()
            except ValueError:
                continue
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
        except ValueError:
            return None
    return None


def as_market_date(value) -> date | None:
    """
    Calendar date **in market time** for a UTC instant.

    Use this for timestamps produced by ``utcnow()`` — ``predicted_at``,
    ``scraped_at``, ``snapped_at``. At 23:00 UTC on a Monday it is still Monday
    in New York, so ``utcnow().date()`` would report Tuesday and put the record
    on the wrong trading day.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        converted = to_market(value)
        return converted.date() if converted else None
    return as_date(value)


def is_weekend(d: date) -> bool:
    return d.weekday() >= 5


def market_is_open(now: datetime | None = None) -> bool:
    """
    Rough check for whether the regular session is open.

    Does not know about market holidays — callers that need exact trading days
    should use the index returned by yfinance, which only contains real sessions.
    """
    now = now or market_now()
    if is_weekend(now.date()):
        return False
    return MARKET_OPEN <= now.time() <= MARKET_CLOSE


def previous_trading_day(d: date | None = None) -> date:
    """Most recent weekday strictly before ``d`` (holidays not considered)."""
    d = d or market_date()
    d -= timedelta(days=1)
    while is_weekend(d):
        d -= timedelta(days=1)
    return d


def next_trading_day(d: date | None = None) -> date:
    """Next weekday strictly after ``d`` (holidays not considered)."""
    d = d or market_date()
    d += timedelta(days=1)
    while is_weekend(d):
        d += timedelta(days=1)
    return d


def trading_days_between(start: date, end: date) -> int:
    """Count weekdays in (start, end]. Negative if end precedes start."""
    if end < start:
        return -trading_days_between(end, start)
    days = 0
    cursor = start
    while cursor < end:
        cursor += timedelta(days=1)
        if not is_weekend(cursor):
            days += 1
    return days
