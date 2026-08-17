"""
mktscan/backtest_incremental.py
──────────────────────────────────────────────────────────────────────────────
Incremental signal backtester.

What was wrong before
─────────────────────
The old ``_composite_score`` reconstructed **3 of the 9 categories** — 39% of the
model's weight — and used *different formulas* from production while doing it.
The RSI mapping is the clearest example:

    RSI band     production        backtest
    ─────────    ──────────        ────────
    < 25         +0.8              +0.7 / +0.8
    40-60         0.0              +0.3      ← neutral vs the most common positive
    60-70        +0.4              +0.1
    >= 80        -0.3              -0.6

So the win rates and Sharpe ratios on the Backtest page described a signal that
was not the one generating the recommendations. Beyond that:

  • Forward returns were **stock** returns. Even a correct reconstruction would
    not tell you what the options P&L was, and options are what this tool
    recommends.
  • No transaction costs and no bid/ask spread.
  • No benchmark, so a 55% win rate looked like an edge when the underlying
    universe rose on 55% of days anyway.

What it does now
────────────────
1. Reconstructs each category using the **actual production functions** from
   tradeability.py, driven from historical bars. Categories that cannot be
   reconstructed from price history alone (news sentiment, analyst targets,
   short interest) are reported as excluded rather than silently reweighted, and
   ``coverage`` records how much of the model was live on each observation.
2. Applies cross-sectional ranking across the basket per date, exactly as
   production does — that is a large part of the signal and cannot be
   reconstructed one ticker at a time.
3. Models **option P&L** on the structure the strategy layer would have picked,
   including the bid/ask spread, using realised volatility as the IV estimate.
4. Reports a **benchmark**: buy-and-hold on the same names over the same
   horizons, so the signal's numbers can be read against the alternative of not
   having a signal.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any, Optional

import numpy as np
import pandas as pd
from sqlalchemy import (
    Column, String, Float, Integer, Date, DateTime,
    Boolean, Index, select, func,
)
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

try:
    from .database import Base
except ImportError:                      # pragma: no cover - direct script use
    from database import Base

# Round-trip cost assumption for a two-leg spread, as a fraction of the debit.
# Real multi-leg retail fills land somewhere between mid and the natural price;
# 4% of the debit per round trip is a moderate, defensible assumption. Setting
# this to zero is what makes most published backtests unreproducible.
ROUND_TRIP_COST_PCT = 0.04

MIN_HISTORY_BARS = 260      # ~1 trading year, so the 252-day 52w range is valid


# ── Models ────────────────────────────────────────────────────────────────────

class BacktestObservation(Base):
    """One ticker-day: reconstructed score plus the forward outcome."""
    __tablename__ = "backtest_observations"

    id         = Column(Integer, primary_key=True, autoincrement=True)
    ticker     = Column(String(10), nullable=False)
    obs_date   = Column(Date,       nullable=False)
    score      = Column(Float,      nullable=False)
    label      = Column(String(20), nullable=False)
    coverage   = Column(Float)               # fraction of model weight with data

    # Underlying forward returns, %
    fwd_5d     = Column(Float)
    fwd_10d    = Column(Float)
    fwd_21d    = Column(Float)
    fwd_63d    = Column(Float)

    # Option-level outcome for the structure the strategy layer would pick
    strategy       = Column(String(30))
    option_pnl_pct = Column(Float)           # % return on capital at risk
    option_win     = Column(Boolean)
    realized_vol   = Column(Float)
    # Backtest v2: exact historical option structure from ORATS when available.
    option_data_source = Column(String(30))
    option_expiration  = Column(Date)
    option_long_strike = Column(Float)
    option_short_strike = Column(Float)
    option_entry_debit = Column(Float)
    option_exit_value  = Column(Float)

    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_bt_ticker_date", "ticker", "obs_date", unique=True),
        Index("ix_bt_label", "label"),
    )


class BacktestSummary(Base):
    """Summary statistics per label × holding period."""
    __tablename__ = "backtest_summary"

    id               = Column(Integer, primary_key=True, autoincrement=True)
    label            = Column(String(20), nullable=False)
    holding_days     = Column(Integer,    nullable=False)
    n_observations   = Column(Integer, default=0)
    avg_return_pct   = Column(Float)
    median_return_pct = Column(Float)
    win_rate_pct     = Column(Float)
    sharpe           = Column(Float)
    best_return_pct  = Column(Float)
    worst_return_pct = Column(Float)

    # Benchmark over the identical observation set
    benchmark_avg_return_pct = Column(Float)
    benchmark_win_rate_pct   = Column(Float)
    excess_return_pct        = Column(Float)   # signal minus benchmark

    # Option-level results
    option_avg_pnl_pct = Column(Float)
    option_win_rate    = Column(Float)

    updated_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_bt_summary_label_hp", "label", "holding_days", unique=True),
    )


# ── Historical reconstruction ─────────────────────────────────────────────────

def _price_data_at(hist: pd.DataFrame, i: int) -> dict[str, Any]:
    """
    Build the ``price_data`` dict the production signal functions expect, using
    only information available on bar ``i``.

    Strictly backward-looking: every rolling window ends at ``i``. Fields the
    tool sources live from Yahoo (analyst ratings, short interest, P/E) cannot be
    reconstructed historically and are left absent, which makes those categories
    report zero confidence — so they are excluded from the weighting rather than
    silently imputed.
    """
    window_52w = hist.iloc[max(0, i - 251): i + 1]
    close      = float(hist["Close"].iloc[i])
    prev_close = float(hist["Close"].iloc[i - 1]) if i > 0 else close

    vol_window = hist["Volume"].iloc[max(0, i - 29): i + 1]
    avg_volume = float(vol_window.mean()) if len(vol_window) else None
    volume     = float(hist["Volume"].iloc[i])

    return {
        "price":        close,
        "change_pct":   (close / prev_close - 1) * 100 if prev_close else 0.0,
        "week_52_high": float(window_52w["High"].max()),
        "week_52_low":  float(window_52w["Low"].min()),
        "volume":       volume,
        "avg_volume_30d": avg_volume,
        "volume_ratio": (volume / avg_volume) if avg_volume else None,
        # Not reconstructable from price history:
        "pe_ratio": None, "analyst_rating": None, "analyst_mean_score": None,
        "target_price": None, "short_ratio": None, "short_pct_float": None,
        "implied_volatility": None, "beta": None,
    }


def _returns_up_to(hist: pd.DataFrame, i: int, bars: int = 120) -> list[float]:
    """Daily percentage returns ending at bar ``i``, oldest first."""
    start = max(1, i - bars + 1)
    closes = hist["Close"].iloc[start - 1: i + 1].to_numpy(dtype=float)
    if len(closes) < 2:
        return []
    return [float(v) for v in (np.diff(closes) / closes[:-1] * 100)]


def _realized_vol(hist: pd.DataFrame, i: int, window: int = 30) -> float | None:
    """Annualised realised volatility as a decimal, ending at bar ``i``."""
    closes = hist["Close"].iloc[max(0, i - window): i + 1].to_numpy(dtype=float)
    if len(closes) < 10:
        return None
    log_ret = np.diff(np.log(closes))
    return float(np.std(log_ret, ddof=1) * np.sqrt(252))


def reconstruct_score(
    hist: pd.DataFrame,
    i: int,
    xs: dict[str, float] | None = None,
) -> dict[str, Any] | None:
    """
    Run the **production** signal functions against historical bar ``i``.

    This is the core fix: the backtest no longer reimplements the model. If
    tradeability.py changes, this changes with it, and the two can no longer
    silently diverge.
    """
    from .tradeability import (
        MIN_CATEGORY_CONFIDENCE, _normalise_weights, DEFAULT_WEIGHTS,
        calc_technical_signal, calc_price_momentum_signal,
        calc_volume_signal, tradeability_label,
    )

    if i < MIN_HISTORY_BARS:
        return None

    price_data    = _price_data_at(hist, i)
    daily_returns = _returns_up_to(hist, i, bars=120)
    if len(daily_returns) < 30:
        return None

    xs = xs or {}
    categories = {
        "technical":      calc_technical_signal(price_data, xs),
        "price_momentum": calc_price_momentum_signal(daily_returns, xs),
        "volume":         calc_volume_signal(price_data, xs),
    }

    weights = _normalise_weights(DEFAULT_WEIGHTS)
    total_w = weighted = 0.0
    for key, result in categories.items():
        confidence = result.get("confidence", 0.0)
        if confidence < MIN_CATEGORY_CONFIDENCE:
            continue
        eff = weights[key] * confidence
        weighted += result["score"] * eff
        total_w  += eff

    if total_w <= 0:
        return None

    score = max(-1.0, min(1.0, weighted / total_w))
    # Fraction of the *full* model's weight that was actually available. The old
    # version renormalised the three reconstructable weights to 1.0 and reported
    # the result as if the whole model had spoken.
    coverage = sum(weights[k] for k in categories)

    return {
        "score":    round(score, 4),
        "label":    tradeability_label(score).replace(" ", "_"),
        "coverage": round(coverage, 3),
        "rsi":      categories["price_momentum"].get("rsi"),
        "annual_vol": categories["price_momentum"].get("annual_vol"),
    }


# ── Option P&L simulation ─────────────────────────────────────────────────────

def simulate_option_pnl(
    score:      float,
    spot:       float,
    spot_later: float,
    realized_vol: float | None,
    holding_days: int = 21,
    dte:          int = 35,
) -> dict[str, Any] | None:
    """
    Model the P&L of the structure the strategy layer would have chosen.

    Uses realised volatility as the IV estimate, because historical option chains
    are not available from yfinance. That is a real limitation: it ignores the
    variance risk premium (IV is usually above RV) and any IV change over the
    holding period, so it will tend to *understate* the cost of buying premium
    and overstate the return on debit spreads. It is nonetheless far closer to
    the truth than treating a 2% stock move as a 2% position return.

    Returns P&L as a percentage of capital at risk, net of the round-trip spread.
    """
    from .pricing import bs_price, years_to_expiry
    from .strategy import DIRECTIONAL_THRESHOLD

    if not realized_vol or realized_vol <= 0 or spot <= 0:
        return None
    if abs(score) < DIRECTIONAL_THRESHOLD:
        return None                       # the model says no trade; do not trade

    is_bullish = score > 0
    right      = "C" if is_bullish else "P"
    vol        = realized_vol
    t_open     = years_to_expiry(dte)
    t_close    = years_to_expiry(max(1, dte - holding_days))

    # Debit vertical, strikes at roughly 0.45 / 0.25 delta — the same shape the
    # strategy grid selects. Approximated by moneyness offsets rather than a
    # delta solve, since this runs across hundreds of thousands of bars.
    offset = vol * np.sqrt(t_open) * spot
    if is_bullish:
        k_long, k_short = spot + 0.10 * offset, spot + 0.90 * offset
    else:
        k_long, k_short = spot - 0.10 * offset, spot - 0.90 * offset

    debit_open = (bs_price(spot, k_long, t_open, vol, right)
                  - bs_price(spot, k_short, t_open, vol, right))
    if debit_open <= 0.01:
        return None

    value_close = (bs_price(spot_later, k_long, t_close, vol, right)
                   - bs_price(spot_later, k_short, t_close, vol, right))

    cost = debit_open * ROUND_TRIP_COST_PCT
    pnl  = value_close - debit_open - cost

    return {
        "strategy":  "bull_call_spread" if is_bullish else "bear_put_spread",
        "pnl_pct":   round(pnl / debit_open * 100, 3),
        "win":       bool(pnl > 0),
        "debit":     round(debit_open, 4),
    }


# ── Core driver ───────────────────────────────────────────────────────────────

def _init_tables(session: Session) -> None:
    from sqlalchemy import inspect as sa_inspect
    engine   = session.get_bind()
    existing = set(sa_inspect(engine).get_table_names())
    if not {"backtest_observations", "backtest_summary"} <= existing:
        Base.metadata.create_all(
            engine, tables=[BacktestObservation.__table__, BacktestSummary.__table__]
        )
        log.info("Created backtest tables")


def _last_obs_date(session: Session, ticker: str) -> Optional[date]:
    return session.execute(
        select(func.max(BacktestObservation.obs_date))
        .where(BacktestObservation.ticker == ticker)
    ).scalar()


def _fetch_price_data(ticker: str, start: date, end: date) -> Optional[pd.DataFrame]:
    try:
        import yfinance as yf
        hist = yf.download(ticker, start=str(start), end=str(end + timedelta(days=1)),
                           progress=False, auto_adjust=True)
        if hist is None or hist.empty or len(hist) < MIN_HISTORY_BARS:
            return None
        if isinstance(hist.columns, pd.MultiIndex):
            hist.columns = hist.columns.get_level_values(0)
        return hist
    except Exception as e:
        log.error(f"  {ticker}: yfinance fetch failed - {e}")
        return None


def run_incremental_backtest(
    session: Session,
    tickers: list[str],
    full_lookback_days: int = 365 * 5,
    holding_periods: list[int] = (5, 10, 21, 63),
    progress_cb=None,
) -> dict:
    """
    Run (or extend) the backtest across the basket.

    Fetches all tickers first so cross-sectional ranking can be applied per date,
    matching how production scores the basket. The previous version processed
    each ticker in isolation, which cannot reproduce a cross-sectional model.
    """
    from .cross_section import build_cross_sectional_scores
    from .tradeability import extract_features

    holding_periods = list(holding_periods)

    def _log(msg: str):
        log.info(msg)
        if progress_cb:
            progress_cb("info", msg)

    _init_tables(session)
    today = date.today()

    # ── Load history ──────────────────────────────────────────────────────────
    histories: dict[str, pd.DataFrame] = {}
    start_from: dict[str, date] = {}
    skipped = failed = 0

    for ticker in tickers:
        last_date = _last_obs_date(session, ticker)
        if last_date is not None and (today - last_date).days < 2:
            skipped += 1
            continue

        start = (today - timedelta(days=full_lookback_days + 400)
                 if last_date is None
                 else last_date - timedelta(days=400))
        hist = _fetch_price_data(ticker, start, today)
        if hist is None:
            _log(f"  {ticker}: insufficient data, skipping")
            failed += 1
            continue

        histories[ticker]  = hist
        start_from[ticker] = (last_date + timedelta(days=1)) if last_date else date.min
        _log(f"  {ticker}: loaded {len(hist)} bars")

    if not histories:
        _log("No tickers to process")
        _refresh_summary(session, holding_periods)
        return {"new_observations": 0, "tickers_skipped": skipped,
                "tickers_failed": failed, "tickers_processed": 0}

    # ── Align dates across the basket for cross-sectional ranking ─────────────
    all_dates = sorted(set().union(*[
        {d.date() if hasattr(d, "date") else d for d in h.index} for h in histories.values()
    ]))
    index_of = {
        ticker: {(d.date() if hasattr(d, "date") else d): i for i, d in enumerate(h.index)}
        for ticker, h in histories.items()
    }

    _log(f"Scoring {len(all_dates)} dates across {len(histories)} tickers…")

    new_rows = 0
    existing_keys = {
        (t, d) for t, d in session.execute(
            select(BacktestObservation.ticker, BacktestObservation.obs_date)
        ).all()
    }

    pending: list[BacktestObservation] = []

    for obs_date in all_dates:
        if obs_date >= today:
            continue

        # Features for every ticker that traded on this date.
        features: dict[str, dict] = {}
        bar_index: dict[str, int] = {}
        for ticker, hist in histories.items():
            i = index_of[ticker].get(obs_date)
            if i is None or i < MIN_HISTORY_BARS:
                continue
            bar_index[ticker] = i
            features[ticker] = extract_features(
                _price_data_at(hist, i), _returns_up_to(hist, i, 120), None, None
            )

        if not features:
            continue

        xs_scores = build_cross_sectional_scores(features)

        for ticker, i in bar_index.items():
            if obs_date < start_from[ticker] or (ticker, obs_date) in existing_keys:
                continue

            hist   = histories[ticker]
            result = reconstruct_score(hist, i, xs_scores.get(ticker, {}))
            if result is None:
                continue

            closes = hist["Close"]
            spot   = float(closes.iloc[i])
            fwd: dict[int, float | None] = {}
            for hp in holding_periods:
                j = i + hp
                fwd[hp] = (float(closes.iloc[j]) / spot - 1) * 100 if j < len(closes) else None

            rv = _realized_vol(hist, i)
            option = None
            j21 = i + 21
            if j21 < len(closes) and rv:
                option = simulate_option_pnl(
                    score=result["score"], spot=spot, spot_later=float(closes.iloc[j21]),
                    realized_vol=rv, holding_days=21, dte=35,
                )

            pending.append(BacktestObservation(
                ticker=ticker, obs_date=obs_date,
                score=result["score"], label=result["label"],
                coverage=result["coverage"],
                fwd_5d=_r(fwd.get(5)), fwd_10d=_r(fwd.get(10)),
                fwd_21d=_r(fwd.get(21)), fwd_63d=_r(fwd.get(63)),
                strategy=option["strategy"] if option else None,
                option_pnl_pct=option["pnl_pct"] if option else None,
                option_win=option["win"] if option else None,
                realized_vol=round(rv, 4) if rv else None,
            ))
            existing_keys.add((ticker, obs_date))
            new_rows += 1

            if len(pending) >= 2000:
                session.add_all(pending)
                session.commit()
                pending = []

    if pending:
        session.add_all(pending)
        session.commit()

    _log(f"Stored {new_rows} new observations; refreshing summary…")
    _refresh_summary(session, holding_periods)

    return {
        "new_observations": new_rows,
        "tickers_skipped":  skipped,
        "tickers_failed":   failed,
        "tickers_processed": len(histories),
    }


def _r(value: float | None) -> float | None:
    return round(value, 4) if value is not None else None


# ── Summary ───────────────────────────────────────────────────────────────────

def _refresh_summary(session: Session, holding_periods: list[int] = (5, 10, 21, 63)) -> None:
    """
    Recompute summary statistics, including a benchmark on the same observations.

    The benchmark is the unconditional mean return across *all* observations for
    that holding period — i.e. what you would have got holding the basket without
    any signal. ``excess_return_pct`` is the only number here that says whether
    the signal added anything.
    """
    label_order = ["STRONG_BUY", "BULLISH", "NEUTRAL", "BEARISH", "STRONG_SELL"]
    col_map     = {5: "fwd_5d", 10: "fwd_10d", 21: "fwd_21d", 63: "fwd_63d"}

    rows = session.execute(
        select(
            BacktestObservation.label,
            BacktestObservation.fwd_5d, BacktestObservation.fwd_10d,
            BacktestObservation.fwd_21d, BacktestObservation.fwd_63d,
            BacktestObservation.option_pnl_pct, BacktestObservation.option_win,
        )
    ).all()
    if not rows:
        return

    df = pd.DataFrame(rows, columns=[
        "label", "fwd_5d", "fwd_10d", "fwd_21d", "fwd_63d",
        "option_pnl_pct", "option_win",
    ])

    for hp in holding_periods:
        col = col_map[hp]
        universe = df[col].dropna()
        bench_avg = float(universe.mean()) if len(universe) else None
        bench_win = float((universe > 0).mean() * 100) if len(universe) else None

        for label in label_order:
            subset = df[df["label"] == label]
            series = subset[col].dropna()
            n      = len(series)
            if n == 0:
                continue

            avg_ret  = float(series.mean())
            std_ret  = float(series.std())
            sharpe   = float(avg_ret / std_ret * np.sqrt(252 / hp)) if std_ret > 0 else 0.0

            opt = subset["option_pnl_pct"].dropna()
            opt_avg = float(opt.mean()) if len(opt) else None
            opt_win = float((opt > 0).mean() * 100) if len(opt) else None

            payload = dict(
                n_observations   = n,
                avg_return_pct   = round(avg_ret, 3),
                median_return_pct = round(float(series.median()), 3),
                win_rate_pct     = round(float((series > 0).mean() * 100), 2),
                sharpe           = round(sharpe, 3),
                best_return_pct  = round(float(series.max()), 3),
                worst_return_pct = round(float(series.min()), 3),
                benchmark_avg_return_pct = round(bench_avg, 3) if bench_avg is not None else None,
                benchmark_win_rate_pct   = round(bench_win, 2) if bench_win is not None else None,
                excess_return_pct = round(avg_ret - bench_avg, 3) if bench_avg is not None else None,
                option_avg_pnl_pct = round(opt_avg, 3) if opt_avg is not None else None,
                option_win_rate    = round(opt_win, 2) if opt_win is not None else None,
                updated_at = datetime.utcnow(),
            )

            existing = session.execute(
                select(BacktestSummary)
                .where(BacktestSummary.label == label)
                .where(BacktestSummary.holding_days == hp)
            ).scalar_one_or_none()

            if existing:
                for key, value in payload.items():
                    setattr(existing, key, value)
            else:
                session.add(BacktestSummary(label=label, holding_days=hp, **payload))

    session.commit()


def get_summary(session: Session) -> list[dict]:
    try:
        rows = session.execute(
            select(BacktestSummary)
            .order_by(BacktestSummary.holding_days, BacktestSummary.label)
        ).scalars().all()
    except Exception:
        session.rollback()
        return []

    return [
        {
            "label": r.label, "holding_days": r.holding_days,
            "n_observations": r.n_observations,
            "avg_return_pct": r.avg_return_pct,
            "median_return_pct": r.median_return_pct,
            "win_rate_pct": r.win_rate_pct, "sharpe": r.sharpe,
            "best_return_pct": r.best_return_pct,
            "worst_return_pct": r.worst_return_pct,
            "benchmark_avg_return_pct": r.benchmark_avg_return_pct,
            "benchmark_win_rate_pct": r.benchmark_win_rate_pct,
            "excess_return_pct": r.excess_return_pct,
            "option_avg_pnl_pct": r.option_avg_pnl_pct,
            "option_win_rate": r.option_win_rate,
            "updated_at": r.updated_at,
        }
        for r in rows
    ]


def get_total_observations(session: Session) -> int:
    try:
        return session.execute(select(func.count(BacktestObservation.id))).scalar() or 0
    except Exception:
        session.rollback()
        return 0
