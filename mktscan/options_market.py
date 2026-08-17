"""Options Market v2: IV regime, term structure, skew and expected move.

ORATS is the preferred source because its summary and IV-rank endpoints expose a
clean historical/current volatility surface.  When ORATS is not configured the
module deliberately returns no fabricated surface; the existing IVSnapshot
pipeline remains available elsewhere as the proxy/live-chain fallback.
"""
from __future__ import annotations

import json
import math
from datetime import date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .database import OptionsMarketSnapshot
from .providers.orats import OratsClient, OratsError


def _f(row: dict[str, Any] | None, key: str) -> float | None:
    if not row:
        return None
    value = row.get(key)
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _term_state(iv30: float | None, iv60: float | None, iv90: float | None) -> str | None:
    if iv30 is None or iv60 is None:
        return None
    s1 = iv60 - iv30
    s2 = (iv90 - iv60) if iv90 is not None else 0.0
    if s1 < -0.015:
        return "BACKWARDATION"
    if s1 > 0.015 and s2 >= -0.01:
        return "CONTANGO"
    return "FLAT"


def build_orats_options_market(ticker: str, trade_date: date | None = None,
                               client: OratsClient | None = None) -> dict[str, Any]:
    client = client or OratsClient()
    summary = client.get_summary(ticker, trade_date)
    rank = client.get_iv_rank(ticker, trade_date)
    if not summary:
        raise OratsError(f"No ORATS summary returned for {ticker}")

    spot = _f(summary, "stockPrice") or _f(summary, "spotPrice")
    iv30, iv60, iv90 = (_f(summary, "iv30d"), _f(summary, "iv60d"), _f(summary, "iv90d"))
    atm = iv30 or (_f(rank, "iv") / 100.0 if _f(rank, "iv") is not None else None)

    # ORATS delta buckets are call-delta buckets.  dlt25 is OTM call IV; dlt75
    # corresponds approximately to the same absolute delta on the put wing.
    call25 = _f(summary, "dlt25Iv30d")
    put25 = _f(summary, "dlt75Iv30d")
    call_skew = (call25 - atm) if call25 is not None and atm is not None else None
    put_skew = (put25 - atm) if put25 is not None and atm is not None else None

    implied_move = _f(summary, "impliedMove")
    # ORATS has historically returned impliedMove in decimal form in EOD history;
    # normalize defensively if a feed returns percentage points instead.
    if implied_move is not None and implied_move > 1.0:
        implied_move /= 100.0
    expected_dollars = spot * implied_move if spot is not None and implied_move is not None else None

    td = trade_date
    if td is None:
        raw = summary.get("tradeDate") or (rank or {}).get("tradeDate")
        td = datetime.strptime(str(raw)[:10], "%Y-%m-%d").date() if raw else date.today()

    components = {
        "summary_confidence": _f(summary, "confidence"),
        "contango_vendor": _f(summary, "contango"),
        "skewing_vendor": _f(summary, "skewing"),
        "implied_earnings_move": _f(summary, "impliedEarningsMove"),
    }
    available = sum(v is not None for v in (atm, iv30, iv60, iv90, put25, call25, implied_move))

    return {
        "ticker": ticker.upper(), "snapshot_date": td, "source": "ORATS_EOD",
        "spot": spot, "atm_iv": atm,
        "iv_rank_1y": _f(rank, "ivRank1y"), "iv_percentile_1y": _f(rank, "ivPct1y"),
        "iv_30d": iv30, "iv_60d": iv60, "iv_90d": iv90,
        "term_slope_30_60": (iv60 - iv30) if iv60 is not None and iv30 is not None else None,
        "term_slope_60_90": (iv90 - iv60) if iv90 is not None and iv60 is not None else None,
        "term_state": _term_state(iv30, iv60, iv90),
        "put_25d_iv": put25, "call_25d_iv": call25,
        "put_skew": put_skew, "call_skew": call_skew,
        "expected_move_pct": implied_move * 100 if implied_move is not None else None,
        "expected_move_dollars": expected_dollars,
        "confidence": available / 7.0,
        "components": components,
    }



def _yahoo_chain_iv(frame, spot: float, dte: int, target_abs_delta: float | None = None,
                    right: str = "C") -> float | None:
    """Pick ATM IV or IV nearest an absolute Black-Scholes delta from a Yahoo frame."""
    if frame is None or frame.empty or spot <= 0:
        return None
    from .pricing import bs_greeks, years_to_expiry
    best = None
    for _, row in frame.iterrows():
        try:
            strike = float(row.get("strike"))
            iv = float(row.get("impliedVolatility"))
        except (TypeError, ValueError):
            continue
        if not (0.01 < iv < 5.0) or strike <= 0:
            continue
        if target_abs_delta is None:
            distance = abs(strike - spot) / spot
        else:
            delta = bs_greeks(spot, strike, years_to_expiry(dte), iv, right).delta
            distance = abs(abs(delta) - target_abs_delta)
        if best is None or distance < best[0]:
            best = (distance, iv)
    return best[1] if best else None


def build_yahoo_options_market(session: Session, ticker: str) -> dict[str, Any]:
    """Current options surface from Yahoo; designed as the no-extra-live-subscription path."""
    import yfinance as yf
    from .clock import market_date
    from .iv_rank import compute_iv_rank
    from .options import fetch_spot

    tk = yf.Ticker(ticker.upper())
    expiries = []
    today = market_date()
    for raw in list(tk.options or []):
        try:
            exp = datetime.strptime(raw, "%Y-%m-%d").date()
            dte = (exp - today).days
            if dte >= 7:
                expiries.append((dte, raw))
        except ValueError:
            continue
    if not expiries:
        raise RuntimeError(f"No Yahoo option expiries for {ticker}")
    spot = fetch_spot(ticker)
    if not spot:
        raise RuntimeError(f"No spot price for {ticker}")

    snapshots = {}
    for target in (30, 60, 90):
        dte, expiry = min(expiries, key=lambda x: abs(x[0] - target))
        if expiry in snapshots:
            continue
        chain = tk.option_chain(expiry)
        snapshots[expiry] = (dte, chain)

    def iv_for(target):
        dte, expiry = min(expiries, key=lambda x: abs(x[0] - target))
        chain_dte, chain = snapshots[expiry]
        civ = _yahoo_chain_iv(chain.calls, spot, chain_dte)
        piv = _yahoo_chain_iv(chain.puts, spot, chain_dte, right="P")
        vals = [x for x in (civ, piv) if x is not None]
        return sum(vals) / len(vals) if vals else None

    iv30, iv60, iv90 = iv_for(30), iv_for(60), iv_for(90)
    dte30, exp30 = min(expiries, key=lambda x: abs(x[0] - 30))
    _, chain30 = snapshots[exp30]
    call25 = _yahoo_chain_iv(chain30.calls, spot, dte30, .25, "C")
    put25 = _yahoo_chain_iv(chain30.puts, spot, dte30, .25, "P")
    atm = iv30
    rank = compute_iv_rank(session, ticker.upper())
    move_pct = (atm * math.sqrt(max(dte30, 1) / 365.0) * 100.0) if atm else None

    return {
        "ticker": ticker.upper(), "snapshot_date": today, "source": "YAHOO_CHAIN",
        "spot": spot, "atm_iv": atm,
        "iv_rank_1y": rank.get("iv_rank") if rank.get("basis") == "chain" else None,
        "iv_percentile_1y": rank.get("iv_pct") if rank.get("basis") == "chain" else None,
        "iv_30d": iv30, "iv_60d": iv60, "iv_90d": iv90,
        "term_slope_30_60": (iv60 - iv30) if iv60 is not None and iv30 is not None else None,
        "term_slope_60_90": (iv90 - iv60) if iv90 is not None and iv60 is not None else None,
        "term_state": _term_state(iv30, iv60, iv90),
        "put_25d_iv": put25, "call_25d_iv": call25,
        "put_skew": (put25 - atm) if put25 is not None and atm is not None else None,
        "call_skew": (call25 - atm) if call25 is not None and atm is not None else None,
        "expected_move_pct": move_pct,
        "expected_move_dollars": spot * move_pct / 100.0 if move_pct is not None else None,
        "confidence": sum(v is not None for v in (atm, iv30, iv60, iv90, put25, call25)) / 6.0,
        "components": {"expiry_30_proxy": exp30, "dte_30_proxy": dte30,
                       "iv_rank_basis": rank.get("basis")},
    }

def persist_options_market_snapshot(session: Session, data: dict[str, Any]) -> OptionsMarketSnapshot:
    row = session.execute(select(OptionsMarketSnapshot).where(
        OptionsMarketSnapshot.ticker == data["ticker"],
        OptionsMarketSnapshot.snapshot_date == data["snapshot_date"],
    )).scalar_one_or_none()
    if row is None:
        row = OptionsMarketSnapshot(ticker=data["ticker"], snapshot_date=data["snapshot_date"])
        session.add(row)
    for key, value in data.items():
        if key in {"ticker", "snapshot_date"}:
            continue
        if key == "components":
            value = json.dumps(value, sort_keys=True)
        if hasattr(row, key):
            setattr(row, key, value)
    row.snapped_at = datetime.utcnow()
    session.commit()
    return row


def refresh_options_market(session: Session, tickers: list[str],
                           source: str = "yahoo",
                           client: OratsClient | None = None) -> dict[str, dict[str, Any]]:
    """Refresh current options analytics.

    ``source=yahoo`` is the default so ORATS can remain a historical-only paid
    service. Use ``source=orats`` when the ORATS Data API subscription includes
    current/delayed analytics.
    """
    source = source.lower()
    if source not in {"yahoo", "orats"}:
        raise ValueError("source must be 'yahoo' or 'orats'")
    if source == "orats":
        client = client or OratsClient()
    out = {}
    for ticker in tickers:
        data = (build_orats_options_market(ticker, client=client)
                if source == "orats" else build_yahoo_options_market(session, ticker))
        persist_options_market_snapshot(session, data)
        out[ticker] = data
    return out


def latest_options_market(session: Session, ticker: str | None = None):
    stmt = select(OptionsMarketSnapshot)
    if ticker:
        stmt = stmt.where(OptionsMarketSnapshot.ticker == ticker.upper())
        return session.execute(stmt.order_by(OptionsMarketSnapshot.snapshot_date.desc()).limit(1)).scalar_one_or_none()
    # Latest per ticker using a simple ordered scan; basket size is intentionally small.
    rows = session.execute(stmt.order_by(OptionsMarketSnapshot.snapshot_date.desc())).scalars().all()
    latest = {}
    for row in rows:
        latest.setdefault(row.ticker, row)
    return list(latest.values())
