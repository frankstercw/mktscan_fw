"""Persistence helpers for the economic calendar used by the regime layer."""
from __future__ import annotations

import logging
from datetime import datetime

from .database import MacroEvent

log = logging.getLogger(__name__)


def upsert_macro_events(session, events: list[dict]) -> int:
    saved = 0
    for ev in events:
        name = (ev.get("name") or "").strip()
        event_at = ev.get("datetime")
        source = ev.get("source") or "marketwatch"
        if not name or event_at is None:
            continue
        row = session.query(MacroEvent).filter(
            MacroEvent.source == source,
            MacroEvent.name == name,
            MacroEvent.event_at == event_at,
        ).one_or_none()

        # MarketWatch occasionally changes an event time or our parser may
        # improve its ET→UTC normalization. Match an existing same-day event
        # before creating a duplicate, then update its timestamp.
        if row is None:
            day_start = event_at.replace(hour=0, minute=0, second=0, microsecond=0)
            day_end = day_start.replace(hour=23, minute=59, second=59, microsecond=999999)
            row = session.query(MacroEvent).filter(
                MacroEvent.source == source,
                MacroEvent.name == name,
                MacroEvent.event_at >= day_start,
                MacroEvent.event_at <= day_end,
            ).one_or_none()

        if row is None:
            row = MacroEvent(source=source, name=name, event_at=event_at)
            session.add(row)
            saved += 1
        else:
            row.event_at = event_at
        row.category = ev.get("category")
        row.importance = ev.get("importance")
        row.period = ev.get("period")
        row.consensus = ev.get("consensus")
        row.prior = ev.get("prior")
        row.actual = ev.get("actual")
        row.updated_at = datetime.utcnow()
    session.commit()
    return saved


def refresh_economic_calendar(session, *, days_forward: int = 35) -> dict:
    """Refresh economic calendar with MarketWatch primary and Benzinga fallback.

    MarketWatch remains the preferred source. If it returns no rows (common
    when a cloud IP is blocked or the DOM changes), use Benzinga Economics when
    a Benzinga key/entitlement is available.
    """
    from datetime import timedelta
    from .config import get_config
    from .scrapers.marketwatch import MarketWatchScraper

    cfg = get_config()
    mw_cfg = dict(cfg.get("sources", {}).get("marketwatch", {}))
    marketwatch_events = []
    try:
        marketwatch_events = MarketWatchScraper(mw_cfg, delay=0.0).fetch_economic_calendar()
    except Exception:
        log.exception("MarketWatch economic calendar refresh failed")

    saved_mw = upsert_macro_events(session, marketwatch_events) if marketwatch_events else 0
    result = {
        "marketwatch_events": len(marketwatch_events),
        "marketwatch_new": saved_mw,
        "benzinga_events": 0,
        "benzinga_new": 0,
        "source": "marketwatch" if marketwatch_events else None,
    }

    if not marketwatch_events:
        bz_cfg = dict(cfg.get("sources", {}).get("benzinga", {}))
        api_key = bz_cfg.get("api_key")
        if api_key and not str(api_key).startswith("YOUR_"):
            try:
                from .scrapers.benzinga import BenzingaScraper
                now = datetime.utcnow()
                bz = BenzingaScraper(bz_cfg, delay=0.0, lookback_days=7)
                bz_events = bz.fetch_economic_calendar(
                    date_from=now - timedelta(days=3),
                    date_to=now + timedelta(days=days_forward),
                    country="USA",
                    min_importance=1,
                )
                saved_bz = upsert_macro_events(session, bz_events) if bz_events else 0
                result.update({
                    "benzinga_events": len(bz_events),
                    "benzinga_new": saved_bz,
                    "source": "benzinga_economics" if bz_events else None,
                })
            except Exception:
                log.exception("Benzinga economic-calendar fallback failed")

    return result
