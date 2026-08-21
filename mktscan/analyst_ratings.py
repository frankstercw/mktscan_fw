"""Analyst Ratings v1.

Benzinga analyst-ratings ingestion, 30-day momentum state, watched ticker
selection and persistence.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from .config import get_config
from .database import AnalystRatingEvent, TradeJournalEntry, get_basket

_BULLISH = ("BUY", "STRONG BUY", "OVERWEIGHT", "OUTPERFORM", "POSITIVE", "ACCUMULATE")
_BEARISH = ("SELL", "STRONG SELL", "UNDERWEIGHT", "UNDERPERFORM", "NEGATIVE", "REDUCE")


def _norm(value: Any) -> str:
    return str(value or "").strip().upper()


def _rating_bias(rating: str | None) -> float:
    value = _norm(rating)
    if any(x in value for x in _BULLISH):
        return 1.0
    if any(x in value for x in _BEARISH):
        return -1.0
    return 0.0


def score_analyst_event(event: AnalystRatingEvent | dict[str, Any]) -> float:
    """Transparent event score used by 30-day Analyst Momentum."""
    getv = (lambda k: event.get(k)) if isinstance(event, dict) else (lambda k: getattr(event, k, None))
    action_company = _norm(getv("action_company"))
    action_pt = _norm(getv("action_pt"))
    current = getv("rating_current")

    score = 0.0
    if "UPGRADE" in action_company:
        score += 2.0
    elif "DOWNGRADE" in action_company:
        score -= 2.0
    elif any(x in action_company for x in ("INITIAT", "REINST")):
        score += 1.5 * _rating_bias(current)

    if "RAISE" in action_pt or "INCREASE" in action_pt:
        score += 1.0
    elif "LOWER" in action_pt or "DECREASE" in action_pt or "CUT" in action_pt:
        score -= 1.0
    return score


def analyst_momentum_from_events(events: list[AnalystRatingEvent | dict[str, Any]]) -> dict[str, Any]:
    score = sum(score_analyst_event(e) for e in events)

    def g(e, k):
        return e.get(k) if isinstance(e, dict) else getattr(e, k, None)

    upgrades = sum("UPGRADE" in _norm(g(e, "action_company")) for e in events)
    downgrades = sum("DOWNGRADE" in _norm(g(e, "action_company")) for e in events)
    pt_raises = sum(
        ("RAISE" in _norm(g(e, "action_pt")) or "INCREASE" in _norm(g(e, "action_pt")))
        for e in events
    )
    pt_cuts = sum(
        any(x in _norm(g(e, "action_pt")) for x in ("LOWER", "DECREASE", "CUT"))
        for e in events
    )

    if score >= 4:
        state = "STRONGLY POSITIVE"
    elif score >= 1.5:
        state = "POSITIVE"
    elif score <= -4:
        state = "STRONGLY NEGATIVE"
    elif score <= -1.5:
        state = "NEGATIVE"
    else:
        state = "NEUTRAL"

    return {
        "score": round(float(score), 2),
        "state": state,
        "events": len(events),
        "upgrades": int(upgrades),
        "downgrades": int(downgrades),
        "pt_raises": int(pt_raises),
        "pt_cuts": int(pt_cuts),
    }


def get_analyst_momentum(
    session: Session,
    ticker: str,
    *,
    as_of: datetime | None = None,
    days: int = 30,
) -> dict[str, Any]:
    as_of = as_of or datetime.utcnow()
    start = as_of - timedelta(days=days)
    events = session.execute(
        select(AnalystRatingEvent)
        .where(
            AnalystRatingEvent.ticker == ticker.upper().strip(),
            AnalystRatingEvent.published_at >= start,
            AnalystRatingEvent.published_at <= as_of,
        )
        .order_by(desc(AnalystRatingEvent.published_at))
    ).scalars().all()
    result = analyst_momentum_from_events(events)
    result["as_of"] = as_of
    result["lookback_days"] = days
    return result


def analyst_watch_tickers(session: Session) -> list[str]:
    """Basket + open journal positions; no global analyst firehose."""
    tickers = {c.ticker.upper() for c in get_basket(session)}
    open_tickers = session.execute(
        select(TradeJournalEntry.ticker).where(TradeJournalEntry.status == "OPEN")
    ).scalars().all()
    tickers.update(str(t).upper() for t in open_tickers if t)
    return sorted(tickers)


def _external_id(item: dict[str, Any]) -> str:
    raw = item.get("external_id") or item.get("id")
    if raw:
        return str(raw)
    basis = "|".join([
        str(item.get("ticker") or ""),
        str(item.get("published_at") or ""),
        str(item.get("firm") or ""),
        str(item.get("analyst_name") or ""),
        str(item.get("action_company") or ""),
        str(item.get("action_pt") or ""),
        str(item.get("rating_current") or ""),
        str(item.get("pt_current") or ""),
    ])
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()


def upsert_analyst_events(session: Session, items: list[dict[str, Any]]) -> dict[str, int]:
    inserted = 0
    updated = 0
    for item in items:
        external_id = _external_id(item)
        row = session.execute(
            select(AnalystRatingEvent).where(AnalystRatingEvent.external_id == external_id)
        ).scalar_one_or_none()
        if row is None:
            row = AnalystRatingEvent(external_id=external_id)
            session.add(row)
            inserted += 1
        else:
            updated += 1

        row.ticker = str(item.get("ticker") or "").upper()
        row.published_at = item.get("published_at") or datetime.utcnow()
        row.firm = item.get("firm")
        row.analyst_name = item.get("analyst_name")
        row.action_company = item.get("action_company")
        row.action_pt = item.get("action_pt")
        row.rating_prior = item.get("rating_prior")
        row.rating_current = item.get("rating_current")
        row.pt_prior = item.get("pt_prior")
        row.pt_current = item.get("pt_current")
        row.importance = item.get("importance")
        row.url = item.get("url")
        row.source = item.get("source") or "benzinga"
        row.raw_json = json.dumps(item.get("raw") or {}, default=str)
        row.updated_at = datetime.utcnow()

    session.commit()
    return {"inserted": inserted, "updated": updated}


def fetch_yahoo_analyst_events(ticker: str, *, lookback_days: int = 45) -> list[dict[str, Any]]:
    """Fallback analyst actions from yfinance when Benzinga is unavailable.

    This keeps the dashboard useful without pretending Yahoo is Benzinga:
    persisted rows carry source='yahoo'. Yahoo does not reliably expose
    historical price-target revisions, so those fields can be empty.
    """
    try:
        import pandas as pd
        import yfinance as yf

        frame = yf.Ticker(ticker).upgrades_downgrades
        if frame is None or frame.empty:
            return []
        df = frame.reset_index()
        date_col = next((c for c in df.columns if str(c).lower() in {"gradedate", "date", "index"}), df.columns[0])
        cutoff = datetime.utcnow() - timedelta(days=lookback_days)
        out = []
        for _, row in df.iterrows():
            raw_dt = row.get(date_col)
            try:
                published = pd.to_datetime(raw_dt, utc=True).to_pydatetime().replace(tzinfo=None)
            except Exception:
                continue
            if published < cutoff:
                continue
            firm = row.get("Firm") or row.get("firm") or ""
            action_raw = str(row.get("Action") or row.get("action") or "")
            action_map = {
                "up": "Upgrades",
                "down": "Downgrades",
                "init": "Initiates",
                "reit": "Reiterates",
                "main": "Maintains",
            }
            action = action_map.get(action_raw.strip().lower(), action_raw)
            from_grade = row.get("FromGrade") or row.get("fromGrade") or ""
            to_grade = row.get("ToGrade") or row.get("toGrade") or ""
            key = hashlib.sha1(
                f"yahoo|{ticker}|{published}|{firm}|{action}|{from_grade}|{to_grade}".encode()
            ).hexdigest()
            out.append({
                "external_id": key,
                "ticker": ticker.upper(),
                "published_at": published,
                "firm": str(firm),
                "analyst_name": "",
                "action_company": str(action),
                "action_pt": "",
                "rating_prior": str(from_grade),
                "rating_current": str(to_grade),
                "pt_prior": None,
                "pt_current": None,
                "importance": None,
                "url": "",
                "source": "yahoo",
                "raw": {str(k): (None if getattr(v, "__class__", None).__name__ == "NAType" else str(v)) for k, v in row.items()},
            })
        return out
    except Exception:
        return []


def refresh_analyst_ratings(
    session: Session,
    tickers: list[str] | None = None,
    *,
    lookback_days: int = 35,
) -> dict[str, Any]:
    """Refresh analyst events.

    Benzinga is primary. If the key is absent, the entitlement rejects the
    request, or it returns no rows, Yahoo upgrades/downgrades are used as an
    explicitly-labeled fallback so the dashboard does not silently stay empty.
    """
    cfg = get_config()
    bz_cfg = dict(cfg.get("sources", {}).get("benzinga", {}))
    api_key = bz_cfg.get("api_key")
    wanted = tickers or analyst_watch_tickers(session)

    all_items: list[dict[str, Any]] = []
    errors: list[str] = []
    provider = "benzinga"

    if api_key and not str(api_key).startswith("YOUR_"):
        try:
            from .scrapers.benzinga import BenzingaScraper
            scraper = BenzingaScraper(bz_cfg, delay=0.10, lookback_days=lookback_days)
            # Benzinga supports up to 50 comma-separated tickers, which is
            # faster and less rate-limit-prone than one request per ticker.
            for i in range(0, len(wanted), 50):
                chunk = wanted[i:i + 50]
                try:
                    all_items.extend(
                        scraper.fetch_ratings(",".join(chunk), lookback_days=lookback_days, max_items=1000)
                    )
                except Exception as exc:
                    errors.append(f"Benzinga {','.join(chunk)}: {exc}")
        except Exception as exc:
            errors.append(f"Benzinga initialization: {exc}")
    else:
        errors.append("MKTSCAN_BENZINGA_KEY is not configured")

    # A valid Benzinga key can still lack the Analyst Ratings entitlement. The
    # scraper logs that HTTP error; if no rows came back, fall back to Yahoo.
    if not all_items:
        provider = "yahoo_fallback"
        for ticker in wanted:
            items = fetch_yahoo_analyst_events(ticker, lookback_days=max(lookback_days, 45))
            all_items.extend(items)

    stats = upsert_analyst_events(session, all_items) if all_items else {"inserted": 0, "updated": 0}
    return {
        "enabled": True,
        "provider": provider,
        "tickers": len(wanted),
        "events": len(all_items),
        "inserted": stats["inserted"],
        "updated": stats["updated"],
        "errors": errors,
        "benzinga_configured": bool(api_key and not str(api_key).startswith("YOUR_")),
    }
