"""Backtest v2: enrich historical signals with real ORATS option-chain P&L."""
from __future__ import annotations

import logging
from datetime import date

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from .backtest_incremental import BacktestObservation
from .database import HistoricalOptionQuote
from .providers.base import OptionQuote
from .providers.orats import OratsClient
from .strategy import DIRECTIONAL_THRESHOLD

log = logging.getLogger(__name__)


def _persist_quotes(session: Session, quotes: list[OptionQuote]) -> None:
    if not quotes:
        return
    # Small basket / date batches: explicit key lookup keeps this portable across
    # SQLite and Postgres without dialect-specific UPSERT code.
    for q in quotes:
        exists = session.execute(select(HistoricalOptionQuote.id).where(
            HistoricalOptionQuote.ticker == q.ticker,
            HistoricalOptionQuote.trade_date == q.trade_date,
            HistoricalOptionQuote.expiration == q.expiration,
            HistoricalOptionQuote.strike == q.strike,
            HistoricalOptionQuote.right == q.right,
            HistoricalOptionQuote.source == q.source,
        )).scalar_one_or_none()
        if exists:
            continue
        session.add(HistoricalOptionQuote(
            ticker=q.ticker, trade_date=q.trade_date, expiration=q.expiration,
            strike=q.strike, right=q.right, underlying_price=q.underlying_price,
            bid=q.bid, ask=q.ask, model_value=q.model_value, volume=q.volume,
            open_interest=q.open_interest, implied_volatility=q.iv,
            delta=q.delta, gamma=q.gamma, theta=q.theta, vega=q.vega, source=q.source,
        ))
    session.commit()


def _from_db(row: HistoricalOptionQuote) -> OptionQuote:
    return OptionQuote(
        ticker=row.ticker, trade_date=row.trade_date, expiration=row.expiration,
        strike=row.strike, right=row.right, underlying_price=row.underlying_price,
        bid=row.bid, ask=row.ask, model_value=row.model_value, volume=row.volume,
        open_interest=row.open_interest, iv=row.implied_volatility,
        delta=row.delta, gamma=row.gamma, theta=row.theta, vega=row.vega,
        source=row.source,
    )


def get_historical_chain(session: Session, client: OratsClient, ticker: str,
                         trade_date: date, min_dte: int = 21, max_dte: int = 60,
                         refresh: bool = False) -> list[OptionQuote]:
    expiry_min = trade_date + pd.Timedelta(days=min_dte)
    expiry_max = trade_date + pd.Timedelta(days=max_dte)
    if not refresh:
        rows = session.execute(select(HistoricalOptionQuote).where(
            HistoricalOptionQuote.ticker == ticker.upper(),
            HistoricalOptionQuote.trade_date == trade_date,
            HistoricalOptionQuote.expiration >= expiry_min.date(),
            HistoricalOptionQuote.expiration <= expiry_max.date(),
            HistoricalOptionQuote.source == "ORATS_EOD",
        )).scalars().all()
        if rows:
            return [_from_db(r) for r in rows]
    quotes = client.get_chain(ticker, trade_date, min_dte=min_dte, max_dte=max_dte)
    _persist_quotes(session, quotes)
    return quotes


def _liquid(q: OptionQuote) -> bool:
    if q.bid is None or q.ask is None or q.bid <= 0 or q.ask <= 0:
        return False
    if q.open_interest is not None and q.open_interest < 25:
        return False
    if q.spread_pct is not None and q.spread_pct > 0.25:
        return False
    return q.delta is not None


def _pick_leg(quotes: list[OptionQuote], right: str, target_delta: float,
              expiry: date, exclude: float | None = None) -> OptionQuote | None:
    candidates = [q for q in quotes if q.right == right and q.expiration == expiry
                  and q.strike != exclude and _liquid(q)]
    if not candidates:
        return None
    return min(candidates, key=lambda q: abs(abs(q.delta or 0) - target_delta))


def _choose_entry_structure(quotes: list[OptionQuote], score: float,
                            target_dte: int = 35) -> tuple[OptionQuote, OptionQuote] | None:
    if abs(score) < DIRECTIONAL_THRESHOLD:
        return None
    expiries = sorted({q.expiration for q in quotes})
    if not expiries:
        return None
    trade_date = quotes[0].trade_date
    expiry = min(expiries, key=lambda e: abs((e - trade_date).days - target_dte))
    right = "C" if score > 0 else "P"
    long_leg = _pick_leg(quotes, right, 0.45, expiry)
    if long_leg is None:
        return None
    short_leg = _pick_leg(quotes, right, 0.25, expiry, exclude=long_leg.strike)
    if short_leg is None:
        return None
    # Preserve vertical orientation (bull call higher short strike; bear put lower short strike).
    if score > 0 and short_leg.strike <= long_leg.strike:
        alternatives = [q for q in quotes if q.right == right and q.expiration == expiry
                        and q.strike > long_leg.strike and _liquid(q)]
        short_leg = min(alternatives, key=lambda q: abs(abs(q.delta or 0)-0.25)) if alternatives else None
    elif score < 0 and short_leg.strike >= long_leg.strike:
        alternatives = [q for q in quotes if q.right == right and q.expiration == expiry
                        and q.strike < long_leg.strike and _liquid(q)]
        short_leg = min(alternatives, key=lambda q: abs(abs(q.delta or 0)-0.25)) if alternatives else None
    return (long_leg, short_leg) if short_leg else None


def _find_contract(quotes: list[OptionQuote], template: OptionQuote) -> OptionQuote | None:
    matches = [q for q in quotes if q.expiration == template.expiration
               and q.strike == template.strike and q.right == template.right]
    return matches[0] if matches else None


def price_orats_debit_spread(session: Session, client: OratsClient, ticker: str,
                             entry_date: date, score: float,
                             holding_days: int = 21) -> dict | None:
    entry_chain = get_historical_chain(session, client, ticker, entry_date, 21, 60)
    structure = _choose_entry_structure(entry_chain, score)
    if not structure:
        return None
    long_open, short_open = structure
    if long_open.ask is None or short_open.bid is None:
        return None
    debit = long_open.ask - short_open.bid
    if debit <= 0.01:
        return None

    # Approximate N trading days with pandas business days, then walk forward up
    # to 5 weekdays for holidays / missing vendor dates.
    target = (pd.Timestamp(entry_date) + pd.offsets.BDay(holding_days)).date()
    exit_chain = []
    exit_date = target
    for offset in range(6):
        candidate = (pd.Timestamp(target) + pd.offsets.BDay(offset)).date()
        remaining = (long_open.expiration - candidate).days
        if remaining <= 0:
            break
        try:
            exit_chain = get_historical_chain(
                session, client, ticker, candidate,
                max(0, remaining - 2), remaining + 2,
            )
        except Exception:
            exit_chain = []
        if exit_chain:
            exit_date = candidate
            break
    if not exit_chain:
        return None

    long_close = _find_contract(exit_chain, long_open)
    short_close = _find_contract(exit_chain, short_open)
    if not long_close or not short_close or long_close.bid is None or short_close.ask is None:
        return None
    exit_value = long_close.bid - short_close.ask
    pnl = exit_value - debit
    return {
        "strategy": "bull_call_spread" if score > 0 else "bear_put_spread",
        "pnl_pct": pnl / debit * 100.0,
        "win": pnl > 0,
        "expiration": long_open.expiration,
        "long_strike": long_open.strike,
        "short_strike": short_open.strike,
        "entry_debit": debit,
        "exit_value": exit_value,
        "entry_date": entry_date,
        "exit_date": exit_date,
        "source": "ORATS_EOD",
    }


def enrich_backtest_with_orats(session: Session, tickers: list[str] | None = None,
                               limit: int = 100, holding_days: int = 21,
                               progress_cb=None) -> dict:
    """Replace synthetic option P&L with actual historical ORATS quotes.

    Deliberately bounded by ``limit`` because each uncached observation can require
    two vendor API calls. Run repeatedly to incrementally enrich the research set.
    """
    client = OratsClient()
    stmt = select(BacktestObservation).where(
        BacktestObservation.option_data_source.is_(None),
        func_abs(BacktestObservation.score) >= DIRECTIONAL_THRESHOLD,
    )
    if tickers:
        stmt = stmt.where(BacktestObservation.ticker.in_([t.upper() for t in tickers]))
    stmt = stmt.order_by(BacktestObservation.obs_date.desc()).limit(limit)
    rows = session.execute(stmt).scalars().all()
    enriched = failed = 0
    for row in rows:
        result = None
        try:
            result = price_orats_debit_spread(
                session, client, row.ticker, row.obs_date, row.score, holding_days
            )
            if result:
                row.strategy = result["strategy"]
                row.option_pnl_pct = round(result["pnl_pct"], 4)
                row.option_win = bool(result["win"])
                row.option_data_source = result["source"]
                row.option_expiration = result["expiration"]
                row.option_long_strike = result["long_strike"]
                row.option_short_strike = result["short_strike"]
                row.option_entry_debit = round(result["entry_debit"], 4)
                row.option_exit_value = round(result["exit_value"], 4)
                enriched += 1
            else:
                failed += 1
        except Exception as exc:
            log.warning("ORATS backtest enrichment failed for %s %s: %s", row.ticker, row.obs_date, exc)
            failed += 1
        if progress_cb:
            progress_cb("info", f"{row.ticker} {row.obs_date}: {'enriched' if result else 'no trade data'}")
    session.commit()
    return {"attempted": len(rows), "enriched": enriched, "failed": failed}


def func_abs(column):
    # Imported lazily this way to keep SQLAlchemy expression readable/testable.
    from sqlalchemy import func
    return func.abs(column)
