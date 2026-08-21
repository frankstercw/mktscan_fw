"""Upcoming earnings-calendar persistence used by Key Events."""
from __future__ import annotations

from datetime import datetime
from sqlalchemy import select

from .config import get_config
from .database import EarningsEvent
from .scrapers.yahoo import YahooScraper


def _upsert(session, events: list[dict]) -> tuple[int, int]:
    inserted = 0
    updated = 0
    for ev in events:
        ticker = str(ev.get("ticker") or "").upper()
        period = ev.get("period")
        if not ticker or not period:
            continue
        row = session.execute(
            select(EarningsEvent).where(
                EarningsEvent.ticker == ticker,
                EarningsEvent.period == period,
            )
        ).scalar_one_or_none()

        if row is None:
            row = EarningsEvent(ticker=ticker, period=period)
            session.add(row)
            inserted += 1
        else:
            updated += 1

        for field in (
            "report_date",
            "eps_estimate",
            "eps_actual",
            "revenue_estimate",
            "revenue_actual",
            "surprise_pct",
        ):
            value = ev.get(field)
            if value is not None:
                setattr(row, field, value)

        if ev.get("eps_actual") is not None:
            row.is_upcoming = False
        elif "is_upcoming" in ev:
            row.is_upcoming = bool(ev["is_upcoming"])
        row.updated_at = datetime.utcnow()

    session.commit()
    return inserted, updated


def refresh_earnings_calendar(session, tickers: list[str]) -> dict:
    """Refresh Yahoo upcoming earnings for the requested MktScan tickers."""
    cfg = get_config()
    yahoo_cfg = dict(cfg.get("sources", {}).get("yahoo_finance", {}))
    yahoo_cfg["enabled"] = True
    scraper = YahooScraper(yahoo_cfg, delay=0.0, lookback_days=7)

    events: list[dict] = []
    errors: list[str] = []
    for ticker in sorted({str(t).upper() for t in tickers if t}):
        try:
            events.extend(scraper.fetch_earnings_calendar(ticker))
        except Exception as exc:
            errors.append(f"{ticker}: {exc}")

    inserted, updated = _upsert(session, events) if events else (0, 0)
    upcoming = sum(
        1 for e in events
        if e.get("is_upcoming") and e.get("report_date") is not None
    )
    return {
        "tickers": len(set(tickers)),
        "events": len(events),
        "upcoming": upcoming,
        "inserted": inserted,
        "updated": updated,
        "errors": errors,
        "source": "yahoo",
    }
