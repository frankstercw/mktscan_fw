"""
mktscan/iv_rank.py
──────────────────────────────────────────────────────────────────────────────
Implied volatility history and IV rank.

IV rank is the primary strategy selector for the options recommendations: it is
what decides between buying premium (long calls/puts, debit spreads) and selling
it (credit spreads, cash-secured puts, condors). It therefore has to actually
work.

What was wrong before
─────────────────────
1. ``IVSnapshot`` was declared on its own ``declarative_base()``, so ``init_db()``
   never created the table. Every ``update_iv_snapshot`` call raised, and the
   exception was swallowed by the scheduler.
2. ``compute_iv_rank()`` was never called from anywhere. The tradeability signal
   instead read ``iv_52w_low`` / ``iv_52w_high`` off ``PriceSnapshot`` through
   ``getattr(..., None)`` — columns that did not exist — so the rank was always
   ``None`` and the whole strategy grid collapsed to its "unknown" fallback.
3. ``check_and_migrate`` used ``engine.execute()``, removed in SQLAlchemy 2.0.
4. The backfill stored 30-day *realised* volatility for historical dates but true
   ATM *implied* volatility for today, then ranked one against the other. IV sits
   structurally above RV (it embeds a variance risk premium), so today's real IV
   pinned near the top of a proxy-built range and ranked ~90 every single day.

The fixes: the model now lives on the shared ``Base`` (in database.py), the rank
is computed and threaded into the signal, and every snapshot records whether it
came from the chain or the proxy so the two are never mixed inside one range.
"""
from __future__ import annotations

import argparse
import logging
import math
import os
from datetime import date, datetime, timedelta
from typing import Any, Optional

from sqlalchemy import select, func
from sqlalchemy.orm import Session

from .database import Base, IVSnapshot, get_engine, get_session, init_db

log = logging.getLogger(__name__)

# Ranking real IV against proxy IV is meaningless, so we require this many true
# chain observations before the rank is reported as chain-based.
MIN_CHAIN_DAYS_FOR_RANK = 60

# Below this many observations of any kind the rank is too noisy to act on.
MIN_DAYS_FOR_ANY_RANK = 20

# Target days-to-expiry when sampling ATM IV. ~30d is the market convention
# (it is what VIX-style measures anchor on) and avoids the wild readings you get
# from expiries with only a few days left.
TARGET_DTE = 30
MIN_DTE    = 7
MAX_DTE    = 60


# ── Schema ────────────────────────────────────────────────────────────────────

def check_and_migrate(engine=None) -> dict:
    """
    Inspect the live schema and create anything missing.

    Returns a report dict. Unlike the previous version this actually *applies*
    the missing columns rather than printing ALTER statements for you to run by
    hand, and it uses the SQLAlchemy 2.0 connection API.
    """
    from sqlalchemy import inspect as sa_inspect
    from .database import ensure_schema, PriceSnapshot

    engine    = engine or get_engine()
    inspector = sa_inspect(engine)
    existing  = set(inspector.get_table_names())
    report: dict[str, Any] = {}

    if "price_snapshots" in existing:
        cols = {c["name"] for c in inspector.get_columns("price_snapshots")}
        report["price_snapshots_cols"] = sorted(cols)
        report["missing_from_price_snapshots"] = [
            c.name for c in PriceSnapshot.__table__.columns if c.name not in cols
        ]
    else:
        report["price_snapshots"] = "TABLE NOT FOUND"

    init_db()                       # creates iv_snapshots via the shared Base
    report["applied_ddl"] = ensure_schema()

    inspector = sa_inspect(engine)  # refresh after DDL
    if "iv_snapshots" in set(inspector.get_table_names()):
        with Session(engine) as s:
            report["iv_snapshots_rows"] = s.execute(
                select(func.count(IVSnapshot.id))
            ).scalar() or 0
        report["iv_snapshots"] = "OK"
    else:
        report["iv_snapshots"] = "MISSING"

    return report


# ── IV fetching ───────────────────────────────────────────────────────────────

def fetch_atm_iv(ticker_symbol: str) -> dict[str, Any] | None:
    """
    Sample ATM implied volatility from the live option chain.

    Picks the expiry closest to ``TARGET_DTE`` (rather than simply the first one
    with >= 7 days, which biased the sample toward short-dated, noisy expiries),
    then interpolates the ATM straddle IV from the call and put bracketing spot.

    Returns ``{"iv": float, "dte": int, "expiry": str}`` or None.
    """
    try:
        import yfinance as yf
        import numpy as np

        tkr  = yf.Ticker(ticker_symbol)
        hist = tkr.history(period="5d")
        if hist.empty:
            return None
        spot = float(hist["Close"].iloc[-1])

        expirations = tkr.options
        if not expirations:
            return None

        from .clock import market_date
        today = market_date()

        # Choose the expiry nearest TARGET_DTE within the acceptable band.
        candidates = []
        for exp_str in expirations:
            try:
                exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
            except ValueError:
                continue
            dte = (exp_date - today).days
            if MIN_DTE <= dte <= MAX_DTE:
                candidates.append((abs(dte - TARGET_DTE), dte, exp_str))
        if not candidates:
            return None
        _, dte, target_expiry = min(candidates)

        chain = tkr.option_chain(target_expiry)
        ivs: list[float] = []

        for frame in (chain.calls, chain.puts):
            if frame is None or frame.empty or "impliedVolatility" not in frame:
                continue
            df = frame.copy()
            df = df[df["impliedVolatility"].notna()]
            # Discard the deep-ITM/OTM garbage yfinance reports as 0.000001 or 5.0
            df = df[(df["impliedVolatility"] > 0.01) & (df["impliedVolatility"] < 3.0)]
            if df.empty:
                continue
            df["distance"] = (df["strike"] - spot).abs()
            nearest = df.nsmallest(2, "distance")     # bracket the spot
            ivs.extend(float(v) for v in nearest["impliedVolatility"].tolist())

        if not ivs:
            return None

        return {
            "iv":     float(np.median(ivs)),   # median resists a single bad quote
            "dte":    dte,
            "expiry": target_expiry,
        }

    except Exception as e:
        log.debug(f"[IV] Option chain failed for {ticker_symbol}: {e}")
        return None


def realized_vol_series(ticker_symbol: str, period: str = "2y", window: int = 30):
    """
    Annualised trailing realised volatility, as a decimal (0.28 = 28%).

    Used only to seed history so IV rank has *something* to work with on day one.
    It is stored under ``source="proxy"`` and never mixed into a range alongside
    true chain IV.
    """
    try:
        import numpy as np
        import yfinance as yf

        hist = yf.Ticker(ticker_symbol).history(period=period)
        if hist is None or len(hist) < window + 5:
            return None

        log_ret = np.log(hist["Close"] / hist["Close"].shift(1))
        rv = log_ret.rolling(window).std() * math.sqrt(252)
        return rv.dropna()
    except Exception as e:
        log.debug(f"[IV] Realised vol failed for {ticker_symbol}: {e}")
        return None


# ── Backfill / daily update ───────────────────────────────────────────────────

def backfill_iv_history(session: Session, tickers: list[str], days: int = 365) -> dict:
    """
    Seed ``iv_snapshots`` with realised-volatility history.

    yfinance exposes only the *current* option chain, so genuine historical IV
    cannot be reconstructed. The realised-vol proxy gives the rank a usable range
    on day one; as real daily chain samples accumulate, ``compute_iv_rank``
    switches to ranking IV against IV and stops using the proxy entirely.
    """
    log.info(f"[IV] Backfilling proxy history for {len(tickers)} tickers ({days}d)...")
    cutoff  = date.today() - timedelta(days=days)
    summary = {"tickers": 0, "rows": 0, "failed": []}

    for ticker_symbol in tickers:
        rv = realized_vol_series(ticker_symbol)
        if rv is None or rv.empty:
            summary["failed"].append(ticker_symbol)
            continue

        existing_dates = {
            d for (d,) in session.execute(
                select(IVSnapshot.snapshot_date).where(IVSnapshot.ticker == ticker_symbol)
            ).all()
        }

        stored = 0
        for idx, value in rv.items():
            row_date = idx.date() if hasattr(idx, "date") else idx
            if row_date < cutoff or row_date in existing_dates:
                continue
            session.add(IVSnapshot(
                ticker        = ticker_symbol,
                snapshot_date = row_date,
                iv_atm        = None,
                iv_proxy      = round(float(value), 6),
                iv_used       = round(float(value), 6),
                source        = "proxy",
            ))
            existing_dates.add(row_date)
            stored += 1

        try:
            session.commit()
        except Exception as e:
            session.rollback()
            log.error(f"[IV] {ticker_symbol}: backfill commit failed — {e}")
            summary["failed"].append(ticker_symbol)
            continue

        summary["tickers"] += 1
        summary["rows"]    += stored
        log.info(f"[IV]   {ticker_symbol}: stored {stored} proxy snapshots")

    log.info(f"[IV] Backfill complete — {summary['rows']} rows")
    return summary


def update_iv_snapshot(session: Session, tickers: list[str]) -> int:
    """
    Record today's true ATM IV for each ticker. Idempotent within a day.

    Should run *once daily* after the close. It was previously wired into the
    15-minute scrape loop, which pulled full option chains 96 times a day per
    ticker — slow, and a fast route to a Yahoo rate limit.
    """
    from .clock import market_date

    today   = market_date()
    updated = 0

    for ticker_symbol in tickers:
        try:
            sample = fetch_atm_iv(ticker_symbol)
            if sample is None:
                log.debug(f"[IV] {ticker_symbol}: no chain IV available today")
                continue

            existing = session.execute(
                select(IVSnapshot).where(
                    IVSnapshot.ticker == ticker_symbol,
                    IVSnapshot.snapshot_date == today,
                )
            ).scalar_one_or_none()

            if existing:
                existing.iv_atm  = sample["iv"]
                existing.iv_used = sample["iv"]
                existing.source  = "chain"
                existing.dte     = sample["dte"]
            else:
                session.add(IVSnapshot(
                    ticker        = ticker_symbol,
                    snapshot_date = today,
                    iv_atm        = sample["iv"],
                    iv_used       = sample["iv"],
                    source        = "chain",
                    dte           = sample["dte"],
                ))
            updated += 1

        except Exception as e:
            session.rollback()
            log.error(f"[IV] {ticker_symbol}: update failed — {e}")

    try:
        session.commit()
    except Exception as e:
        session.rollback()
        log.error(f"[IV] commit failed — {e}")
        return 0

    log.info(f"[IV] snapshots updated for {updated}/{len(tickers)} tickers")
    return updated


# ── IV rank ───────────────────────────────────────────────────────────────────

def compute_iv_rank(
    session: Session,
    ticker_symbol: str,
    lookback_days: int = 365,
) -> dict:
    """
    IV rank and percentile for one ticker.

        IV rank = (current - low) / (high - low) * 100

    Ranks chain IV against chain IV once there are ``MIN_CHAIN_DAYS_FOR_RANK``
    real observations; before that it falls back to ranking the realised-vol
    proxy against proxy history and flags the result as such. The two are never
    mixed in one range — that was the bug that pinned every rank near 90.

    ``basis`` in the result is "chain", "proxy" or "none" so callers (and the
    dashboard) can be honest about what the number means.
    """
    cutoff = date.today() - timedelta(days=lookback_days)

    rows = session.execute(
        select(IVSnapshot)
        .where(
            IVSnapshot.ticker == ticker_symbol,
            IVSnapshot.snapshot_date >= cutoff,
            IVSnapshot.iv_used.isnot(None),
        )
        .order_by(IVSnapshot.snapshot_date.desc())
    ).scalars().all()

    if not rows:
        return _empty_rank()

    chain_rows = [r for r in rows if r.source == "chain"]
    proxy_rows = [r for r in rows if r.source != "chain"]

    if len(chain_rows) >= MIN_CHAIN_DAYS_FOR_RANK:
        series, basis = chain_rows, "chain"
    elif len(proxy_rows) >= MIN_DAYS_FOR_ANY_RANK:
        # Not enough true IV history yet. Rank the proxy against proxy history —
        # a realised-vol regime read, which is a defensible stand-in, clearly
        # labelled so nobody mistakes it for a real IV rank.
        series, basis = proxy_rows, "proxy"
    else:
        return _empty_rank()

    values  = [r.iv_used for r in series if r.iv_used is not None]
    current = series[0].iv_used
    if current is None or len(values) < MIN_DAYS_FOR_ANY_RANK:
        return _empty_rank()

    low, high = min(values), max(values)
    iv_rank   = 50.0 if high <= low else max(0.0, min(100.0, (current - low) / (high - low) * 100))
    iv_pct    = sum(1 for v in values if v < current) / len(values) * 100

    n = len(values)
    # Full confidence needs a year of daily observations; a proxy-based rank is
    # capped well below that because it is measuring the wrong quantity.
    confidence = min(1.0, n / 252.0)
    if basis == "proxy":
        confidence = min(confidence, 0.45)

    return {
        "iv_current":  round(current, 4),
        "iv_52w_low":  round(low, 4),
        "iv_52w_high": round(high, 4),
        "iv_rank":     round(iv_rank, 1),
        "iv_pct":      round(iv_pct, 1),
        "data_days":   n,
        "basis":       basis,
        "confidence":  round(confidence, 3),
    }


def _empty_rank() -> dict:
    return {
        "iv_current": None, "iv_52w_low": None, "iv_52w_high": None,
        "iv_rank": None, "iv_pct": None, "data_days": 0,
        "basis": "none", "confidence": 0.0,
    }


def compute_basket_iv_ranks(session: Session, tickers: list[str]) -> dict[str, dict]:
    """IV rank for every ticker in one pass."""
    return {t: compute_iv_rank(session, t) for t in tickers}


# ── CLI ───────────────────────────────────────────────────────────────────────

def _basket_tickers(session: Session) -> list[str]:
    from .database import get_basket
    return [c.ticker for c in get_basket(session)]


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="IV rank history tool")
    parser.add_argument("--check",    action="store_true", help="Inspect and repair schema")
    parser.add_argument("--backfill", action="store_true", help="Seed proxy IV history (run once)")
    parser.add_argument("--update",   action="store_true", help="Record today's chain IV")
    parser.add_argument("--rank",     type=str, metavar="TICKER", help="Show IV rank for a ticker")
    args = parser.parse_args()

    init_db()

    if args.check:
        report = check_and_migrate()
        print("\n── Schema report ──────────────────────────────")
        for k, v in report.items():
            print(f"  {k}: {v}")
        return

    session = get_session()
    try:
        if args.backfill:
            print(backfill_iv_history(session, _basket_tickers(session)))
        elif args.update:
            update_iv_snapshot(session, _basket_tickers(session))
        elif args.rank:
            rank = compute_iv_rank(session, args.rank.upper())
            print(f"\nIV rank for {args.rank.upper()}:")
            for k, v in rank.items():
                print(f"  {k}: {v}")
        else:
            parser.print_help()
    finally:
        session.close()


if __name__ == "__main__":
    main()
