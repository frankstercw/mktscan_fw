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
