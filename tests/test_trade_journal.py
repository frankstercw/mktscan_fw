from datetime import date, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from mktscan.database import Base, MarketRegimeSnapshot, OptionsMarketSnapshot, TradeJournalEntry, TradeabilityOutcome
from mktscan.trade_journal import capture_entry_context, close_trade, create_trade, trade_metrics


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def test_entry_context_uses_only_information_at_or_before_trade():
    s = _session()
    entry = datetime(2026, 8, 17, 15, 0)
    s.add(TradeabilityOutcome(ticker="AAPL", score_at_prediction=.4, label_at_prediction="BULLISH", predicted_at=entry-timedelta(hours=1), prediction_date=date(2026,8,17)))
    s.add(TradeabilityOutcome(ticker="AAPL", score_at_prediction=-.9, label_at_prediction="BEARISH", predicted_at=entry+timedelta(hours=1), prediction_date=date(2026,8,18)))
    s.add(MarketRegimeSnapshot(snapshot_date=date(2026,8,17), regime_score=.5, regime_label="RISK_ON"))
    s.add(OptionsMarketSnapshot(ticker="AAPL", snapshot_date=date(2026,8,17), source="yahoo", iv_percentile_1y=32, atm_iv=.28))
    s.commit()
    ctx = capture_entry_context(s, "AAPL", entry)
    assert ctx["tradeability_score"] == .4
    assert ctx["tradeability_label"] == "BULLISH"
    assert ctx["regime_label"] == "RISK_ON"
    assert ctx["iv_percentile"] == 32


def test_debit_option_trade_pnl_and_close():
    s = _session()
    t = create_trade(s, ticker="NVDA", instrument_type="OPTION", direction="BULLISH", strategy="Bull Call Spread",
                     opened_at=datetime(2026,8,1,15), quantity=2, multiplier=100, entry_type="DEBIT", entry_value=4.0,
                     entry_fees=2.0, planned_max_loss=800.0, status="OPEN")
    t.current_value = 5.0
    metrics = trade_metrics(t, use_current=True)
    assert metrics.pnl == 198.0
    close_trade(s, t, closed_at=datetime(2026,8,5,15), underlying_exit=190, exit_value=6.0, exit_fees=2.0, exit_reason="Profit target")
    assert t.status == "CLOSED"
    assert t.realized_pnl == 396.0
    assert t.return_on_risk_pct == 49.5


def test_credit_spread_pnl_direction():
    s = _session()
    t = TradeJournalEntry(ticker="MSFT", instrument_type="OPTION", direction="BULLISH", strategy="Bull Put Spread",
                          status="CLOSED", opened_at=datetime(2026,8,1), closed_at=datetime(2026,8,4), quantity=1,
                          multiplier=100, entry_type="CREDIT", entry_value=2.0, exit_value=.5, planned_max_loss=300,
                          entry_fees=1, exit_fees=1)
    m = trade_metrics(t)
    assert m.pnl == 148.0
    assert round(m.return_on_risk_pct, 2) == 49.33
