"""Trade Journal v1 helpers.

Manual trade capture plus immutable MktScan context at entry.  The journal is
kept separate from the model/backtest tables so discretionary execution can be
analysed without contaminating signal validation.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .database import (
    AnalystRatingEvent,
    MarketRegimeSnapshot,
    OptionsMarketSnapshot,
    TradeJournalEntry,
    TradeabilityOutcome,
)
from .analyst_ratings import get_analyst_momentum


@dataclass(frozen=True)
class TradeMetrics:
    pnl: float | None
    return_on_risk_pct: float | None
    holding_days: float | None


def _native_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def capture_entry_context(session: Session, ticker: str, opened_at: datetime) -> dict[str, Any]:
    """Return the best context known *at or before* entry time.

    Falling back to the latest row is deliberately avoided: doing so could leak
    future information into an older manually-entered trade.
    """
    ticker = ticker.upper().strip()
    d = opened_at.date()

    pred = session.execute(
        select(TradeabilityOutcome)
        .where(
            TradeabilityOutcome.ticker == ticker,
            TradeabilityOutcome.predicted_at <= opened_at,
        )
        .order_by(TradeabilityOutcome.predicted_at.desc())
        .limit(1)
    ).scalar_one_or_none()

    regime = session.execute(
        select(MarketRegimeSnapshot)
        .where(MarketRegimeSnapshot.snapshot_date <= d)
        .order_by(MarketRegimeSnapshot.snapshot_date.desc())
        .limit(1)
    ).scalar_one_or_none()

    options = session.execute(
        select(OptionsMarketSnapshot)
        .where(
            OptionsMarketSnapshot.ticker == ticker,
            OptionsMarketSnapshot.snapshot_date <= d,
        )
        .order_by(OptionsMarketSnapshot.snapshot_date.desc())
        .limit(1)
    ).scalar_one_or_none()

    analyst = get_analyst_momentum(session, ticker, as_of=opened_at, days=30)

    return {
        "tradeability_score": _native_float(pred.score_at_prediction) if pred else None,
        "tradeability_label": pred.label_at_prediction if pred else None,
        "tradeability_prediction_id": pred.id if pred else None,
        "regime_score": _native_float(regime.regime_score) if regime else None,
        "regime_label": regime.regime_label if regime else None,
        "regime_snapshot_id": regime.id if regime else None,
        "atm_iv": _native_float(options.atm_iv) if options else None,
        "iv_rank": _native_float(options.iv_rank_1y) if options else None,
        "iv_percentile": _native_float(options.iv_percentile_1y) if options else None,
        "iv_30d": _native_float(options.iv_30d) if options else None,
        "iv_60d": _native_float(options.iv_60d) if options else None,
        "iv_90d": _native_float(options.iv_90d) if options else None,
        "put_skew": _native_float(options.put_skew) if options else None,
        "call_skew": _native_float(options.call_skew) if options else None,
        "expected_move_pct": _native_float(options.expected_move_pct) if options else None,
        "options_source": options.source if options else None,
        "options_snapshot_id": options.id if options else None,
        "analyst_snapshot_at": opened_at,
        "analyst_momentum_score": _native_float(analyst.get("score")),
        "analyst_momentum_state": analyst.get("state"),
        "analyst_events_30d": int(analyst.get("events") or 0),
        "analyst_upgrades_30d": int(analyst.get("upgrades") or 0),
        "analyst_downgrades_30d": int(analyst.get("downgrades") or 0),
        "analyst_pt_raises_30d": int(analyst.get("pt_raises") or 0),
        "analyst_pt_cuts_30d": int(analyst.get("pt_cuts") or 0),
    }


def create_trade(session: Session, **values: Any) -> TradeJournalEntry:
    opened_at = values.get("opened_at") or datetime.utcnow()
    ticker = str(values["ticker"]).upper().strip()
    context = capture_entry_context(session, ticker, opened_at)
    trade = TradeJournalEntry(ticker=ticker, opened_at=opened_at, **{k: v for k, v in values.items() if k not in {"ticker", "opened_at"}}, **context)
    session.add(trade)
    session.commit()
    session.refresh(trade)
    return trade


def trade_metrics(trade: TradeJournalEntry, use_current: bool = False) -> TradeMetrics:
    """Calculate net P&L and return on risk from manually recorded marks/exits."""
    exit_value = trade.current_value if use_current and trade.status == "OPEN" else trade.exit_value
    if exit_value is None or trade.entry_value is None:
        return TradeMetrics(None, None, None)

    qty = float(trade.quantity or 1)
    multiplier = float(trade.multiplier or (100 if trade.instrument_type == "OPTION" else 1))
    entry = float(trade.entry_value)
    exit_ = float(exit_value)

    if trade.instrument_type == "STOCK" and trade.direction == "BEARISH":
        gross = (entry - exit_) * qty * multiplier
    elif trade.entry_type == "CREDIT":
        gross = (entry - exit_) * qty * multiplier
    else:
        gross = (exit_ - entry) * qty * multiplier

    fees = float(trade.entry_fees or 0) + (0.0 if use_current and trade.status == "OPEN" else float(trade.exit_fees or 0))
    pnl = gross - fees

    risk = _native_float(trade.planned_max_loss)
    if not risk or risk <= 0:
        if trade.entry_type == "DEBIT":
            risk = entry * qty * multiplier
        else:
            risk = None
    ror = (pnl / risk * 100.0) if risk else None

    end = trade.marked_at if use_current and trade.status == "OPEN" else trade.closed_at
    holding = None
    if end and trade.opened_at:
        holding = max(0.0, (end - trade.opened_at).total_seconds() / 86400.0)

    return TradeMetrics(round(float(pnl), 2), round(float(ror), 2) if ror is not None else None, round(float(holding), 2) if holding is not None else None)


def close_trade(
    session: Session,
    trade: TradeJournalEntry,
    *,
    closed_at: datetime,
    underlying_exit: float | None,
    exit_value: float,
    exit_fees: float = 0.0,
    exit_reason: str | None = None,
    notes: str | None = None,
) -> TradeJournalEntry:
    trade.status = "CLOSED"
    trade.closed_at = closed_at
    trade.underlying_exit = _native_float(underlying_exit)
    trade.exit_value = float(exit_value)
    trade.exit_fees = float(exit_fees or 0)
    trade.exit_reason = exit_reason
    if notes is not None:
        trade.notes = notes
    metrics = trade_metrics(trade)
    trade.realized_pnl = metrics.pnl
    trade.return_on_risk_pct = metrics.return_on_risk_pct
    trade.holding_days = metrics.holding_days
    trade.updated_at = datetime.utcnow()
    session.commit()
    session.refresh(trade)
    return trade


def mark_trade(session: Session, trade: TradeJournalEntry, current_value: float, marked_at: datetime | None = None) -> TradeJournalEntry:
    trade.current_value = float(current_value)
    trade.marked_at = marked_at or datetime.utcnow()
    trade.updated_at = datetime.utcnow()
    session.commit()
    session.refresh(trade)
    return trade


def iv_bucket(value: float | None) -> str:
    if value is None:
        return "Unknown"
    v = float(value)
    if v < 20: return "<20"
    if v < 40: return "20–40"
    if v < 60: return "40–60"
    if v < 80: return "60–80"
    return "80+"


def score_bucket(value: float | None) -> str:
    if value is None:
        return "Unknown"
    v = abs(float(value))
    if v < .20: return "<0.20"
    if v < .30: return "0.20–0.30"
    if v < .40: return "0.30–0.40"
    if v < .50: return "0.40–0.50"
    if v < .60: return "0.50–0.60"
    return "0.60+"
