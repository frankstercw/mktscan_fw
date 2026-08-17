"""
mktscan/regime.py
Market-regime context for MktScan.

This module deliberately does NOT feed the tradeability score. It records a
separate daily context layer so regime-conditioned performance can be measured
before regime is allowed to change recommendations or position sizing.
"""
from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, asdict
from datetime import datetime
from io import StringIO
from typing import Any

import pandas as pd

from .clock import market_date, market_now
from .database import MarketRegimeSnapshot, MacroEvent

log = logging.getLogger(__name__)


def _clip(value: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(value)))


def _finite(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _series_from_download(raw: pd.DataFrame, ticker: str, field: str = "Close") -> pd.Series:
    """Normalise yfinance single- and multi-ticker download shapes."""
    if raw is None or raw.empty:
        return pd.Series(dtype=float)
    try:
        if isinstance(raw.columns, pd.MultiIndex):
            # yfinance has emitted both (field,ticker) and (ticker,field) layouts.
            if (field, ticker) in raw.columns:
                s = raw[(field, ticker)]
            elif (ticker, field) in raw.columns:
                s = raw[(ticker, field)]
            else:
                return pd.Series(dtype=float)
        elif field in raw.columns:
            s = raw[field]
        else:
            return pd.Series(dtype=float)
        return pd.to_numeric(s, errors="coerce").dropna()
    except Exception:
        return pd.Series(dtype=float)


def _ema(s: pd.Series, span: int) -> float | None:
    if len(s) < span:
        return None
    return _finite(s.ewm(span=span, adjust=False).mean().iloc[-1])


def _return(s: pd.Series, days: int) -> float | None:
    if len(s) <= days:
        return None
    old, new = _finite(s.iloc[-days - 1]), _finite(s.iloc[-1])
    if old in (None, 0) or new is None:
        return None
    return (new / old - 1.0) * 100.0


def _index_trend(s: pd.Series) -> dict[str, Any]:
    """Absolute trend score in [-1,1] using level, alignment and momentum."""
    if len(s) < 60:
        return {"score": None, "confidence": 0.0}

    price = _finite(s.iloc[-1])
    ema20, ema50, ema200 = _ema(s, 20), _ema(s, 50), _ema(s, 200)
    ret20, ret60 = _return(s, 20), _return(s, 60)

    components: list[tuple[float, float]] = []
    def add(cond: bool | None, weight: float):
        if cond is not None:
            components.append((1.0 if cond else -1.0, weight))

    add(price is not None and ema20 is not None and price > ema20, 0.25)
    add(price is not None and ema50 is not None and price > ema50, 0.20)
    add(ema20 is not None and ema50 is not None and ema20 > ema50, 0.20)
    if ema200 is not None:
        add(ema50 is not None and ema50 > ema200, 0.15)

    if ret20 is not None:
        components.append((_clip(ret20 / 10.0), 0.10))
    if ret60 is not None:
        components.append((_clip(ret60 / 20.0), 0.10))

    denom = sum(w for _, w in components)
    score = sum(v * w for v, w in components) / denom if denom else None
    confidence = min(1.0, denom / 1.0)
    return {
        "price": price,
        "ema20": ema20,
        "ema50": ema50,
        "ema200": ema200,
        "return_20d": ret20,
        "return_60d": ret60,
        "above_20ema": bool(price > ema20) if price is not None and ema20 is not None else None,
        "above_50ema": bool(price > ema50) if price is not None and ema50 is not None else None,
        "ema20_above_50": bool(ema20 > ema50) if ema20 is not None and ema50 is not None else None,
        "ema50_above_200": bool(ema50 > ema200) if ema50 is not None and ema200 is not None else None,
        "score": _clip(score) if score is not None else None,
        "confidence": confidence,
    }


def _percentile_last(s: pd.Series, window: int) -> float | None:
    s = s.dropna().tail(window)
    if len(s) < min(20, window):
        return None
    last = s.iloc[-1]
    return float((s <= last).mean() * 100.0)


def _volatility_regime(vix: pd.Series) -> dict[str, Any]:
    if len(vix) < 21:
        return {"score": None, "confidence": 0.0, "state": "UNKNOWN"}
    level = _finite(vix.iloc[-1])
    ret5 = _return(vix, 5)
    pct20 = _percentile_last(vix, 20)
    pct1y = _percentile_last(vix, 252)
    if level is None:
        return {"score": None, "confidence": 0.0, "state": "UNKNOWN"}

    if level < 15:
        level_score, band = 0.55, "CALM"
    elif level < 20:
        level_score, band = 0.25, "NORMAL"
    elif level < 30:
        level_score, band = -0.35, "ELEVATED"
    else:
        level_score, band = -0.85, "STRESSED"

    change_score = -_clip((ret5 or 0.0) / 25.0)
    percentile_score = -_clip(((pct1y if pct1y is not None else pct20 or 50.0) - 50.0) / 50.0)
    score = 0.55 * level_score + 0.30 * change_score + 0.15 * percentile_score
    direction = "RISING" if (ret5 or 0) > 3 else "FALLING" if (ret5 or 0) < -3 else "FLAT"
    conf = 0.65 + (0.20 if pct1y is not None else 0.0) + (0.15 if ret5 is not None else 0.0)
    return {
        "vix": level,
        "change_5d_pct": ret5,
        "percentile_20d": pct20,
        "percentile_1y": pct1y,
        "state": f"{band}_{direction}",
        "score": _clip(score),
        "confidence": min(1.0, conf),
    }


def _breadth(raw: pd.DataFrame, tickers: list[str]) -> dict[str, Any]:
    metrics = {"above_20d": [], "above_50d": [], "above_200d": [], "positive_5d": [], "positive_20d": []}
    usable = 0
    for tk in tickers:
        s = _series_from_download(raw, tk)
        if len(s) < 21:
            continue
        usable += 1
        price = _finite(s.iloc[-1])
        if price is None:
            continue
        ma20 = _finite(s.tail(20).mean()) if len(s) >= 20 else None
        ma50 = _finite(s.tail(50).mean()) if len(s) >= 50 else None
        ma200 = _finite(s.tail(200).mean()) if len(s) >= 200 else None
        if ma20 is not None: metrics["above_20d"].append(price > ma20)
        if ma50 is not None: metrics["above_50d"].append(price > ma50)
        if ma200 is not None: metrics["above_200d"].append(price > ma200)
        r5, r20 = _return(s, 5), _return(s, 20)
        if r5 is not None: metrics["positive_5d"].append(r5 > 0)
        if r20 is not None: metrics["positive_20d"].append(r20 > 0)

    pct: dict[str, float | None] = {}
    for k, vals in metrics.items():
        pct[k] = (sum(bool(v) for v in vals) / len(vals) * 100.0) if vals else None
    scored = [2.0 * p / 100.0 - 1.0 for p in pct.values() if p is not None]
    score = sum(scored) / len(scored) if scored else None
    coverage = usable / len(tickers) if tickers else 0.0
    return {**pct, "score": _clip(score) if score is not None else None,
            "universe_size": usable, "confidence": min(1.0, coverage)}


def _rates_from_frame(df: pd.DataFrame) -> dict[str, Any]:
    """Compute a small rates context from FRED DGS2 / DGS10 observations."""
    if df is None or df.empty:
        return {"score": None, "confidence": 0.0}
    d = df.copy()
    for c in ("DGS2", "DGS10"):
        if c in d:
            d[c] = pd.to_numeric(d[c], errors="coerce")
    d = d.dropna(subset=[c for c in ("DGS2", "DGS10") if c in d.columns])
    if d.empty or "DGS10" not in d:
        return {"score": None, "confidence": 0.0}
    y10 = _finite(d["DGS10"].iloc[-1])
    y2 = _finite(d["DGS2"].iloc[-1]) if "DGS2" in d else None
    c10_5 = (y10 - _finite(d["DGS10"].iloc[-6])) * 100 if y10 is not None and len(d) >= 6 else None
    c10_20 = (y10 - _finite(d["DGS10"].iloc[-21])) * 100 if y10 is not None and len(d) >= 21 else None
    curve = (y10 - y2) if y10 is not None and y2 is not None else None
    # Rising long yields are treated only as a modest equity headwind here. This
    # is a context heuristic, not a fitted causal relationship.
    move = -_clip((c10_20 or c10_5 or 0.0) / 50.0)
    curve_score = _clip((curve or 0.0) / 1.0) if curve is not None else 0.0
    score = 0.75 * move + 0.25 * curve_score
    return {"two_year": y2, "ten_year": y10, "curve_10y_2y": curve,
            "ten_year_5d_change_bps": c10_5, "ten_year_20d_change_bps": c10_20,
            "score": _clip(score), "confidence": 1.0 if y2 is not None else 0.7}


def fetch_fred_rates() -> pd.DataFrame:
    """Fetch DGS2/DGS10 from FRED's keyless CSV endpoint. Failure is non-fatal."""
    import requests
    url = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS2,DGS10"
    try:
        r = requests.get(url, timeout=12, headers={"User-Agent": "MktScan/1.0"})
        r.raise_for_status()
        return pd.read_csv(StringIO(r.text))
    except Exception as e:
        log.warning("[Regime] FRED rates unavailable: %s", e)
        return pd.DataFrame()


def _macro_context(session, now: datetime | None = None) -> dict[str, Any]:
    # MarketWatch calendar datetimes are stored as naive US/Eastern wall time.
    # Compare them with a naive market clock, not UTC, or event risk shifts 4–5h.
    now = now or market_now().replace(tzinfo=None)
    future = list(session.query(MacroEvent).filter(MacroEvent.event_at >= now).order_by(MacroEvent.event_at.asc()).limit(50))
    ranked = [e for e in future if (e.importance or "").lower() == "high"] or future
    ev = ranked[0] if ranked else None
    if ev is None or ev.event_at is None:
        return {"risk_score": 0.0, "confidence": 0.0}
    hours = max(0.0, (ev.event_at - now).total_seconds() / 3600.0)
    if hours <= 6: risk = 1.0
    elif hours <= 24: risk = 0.8
    elif hours <= 48: risk = 0.5
    elif hours <= 72: risk = 0.25
    else: risk = 0.0
    return {"event": ev.name, "event_at": ev.event_at, "importance": ev.importance,
            "hours": hours, "risk_score": risk, "confidence": 1.0}


def _label(score: float, macro_risk: float, vol_state: str) -> str:
    if score >= 0.45: base = "STRONG_RISK_ON"
    elif score >= 0.15: base = "RISK_ON"
    elif score <= -0.45: base = "STRONG_RISK_OFF"
    elif score <= -0.15: base = "RISK_OFF"
    else: base = "NEUTRAL"
    caution = macro_risk >= 0.5 or vol_state.startswith("STRESSED")
    return f"{base}_CAUTION" if caution else base


def compute_regime_from_data(
    market_data: pd.DataFrame,
    basket_tickers: list[str],
    rates_data: pd.DataFrame | None = None,
    macro: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Pure calculation entry point used by tests and the live fetcher."""
    spy = _index_trend(_series_from_download(market_data, "SPY"))
    qqq = _index_trend(_series_from_download(market_data, "QQQ"))
    vix = _volatility_regime(_series_from_download(market_data, "^VIX"))
    breadth = _breadth(market_data, basket_tickers)
    rates = _rates_from_frame(rates_data if rates_data is not None else pd.DataFrame())
    macro = macro or {"risk_score": 0.0, "confidence": 0.0}

    trend_scores = [x["score"] for x in (spy, qqq) if x.get("score") is not None]
    trend_score = sum(trend_scores) / len(trend_scores) if trend_scores else None
    trend_conf = sum(x.get("confidence", 0.0) for x in (spy, qqq)) / 2.0

    pieces = [
        (trend_score, trend_conf, 0.45),
        (vix.get("score"), vix.get("confidence", 0.0), 0.20),
        (breadth.get("score"), breadth.get("confidence", 0.0), 0.25),
        (rates.get("score"), rates.get("confidence", 0.0), 0.10),
    ]
    available = [(s, c, w) for s, c, w in pieces if s is not None and c > 0]
    denom = sum(w * c for _, c, w in available)
    score = sum(float(s) * w * c for s, c, w in available) / denom if denom else 0.0
    intended_weight = sum(w for _, _, w in pieces)
    coverage = sum(w for s, c, w in pieces if s is not None and c > 0) / intended_weight
    confidence = min(1.0, denom / intended_weight)

    return {
        "score": _clip(score),
        "label": _label(_clip(score), float(macro.get("risk_score", 0.0)), vix.get("state", "UNKNOWN")),
        "confidence": confidence,
        "coverage": coverage,
        "trend_score": trend_score,
        "spy": spy, "qqq": qqq, "volatility": vix, "breadth": breadth,
        "rates": rates, "macro": macro,
    }


def fetch_market_data(tickers: list[str], period: str = "1y") -> pd.DataFrame:
    import yfinance as yf
    universe = list(dict.fromkeys(["SPY", "QQQ", "^VIX", *tickers]))
    return yf.download(universe, period=period, interval="1d", auto_adjust=True,
                       progress=False, group_by="column", threads=True)


def refresh_market_regime(session, tickers: list[str]) -> dict[str, Any]:
    """Fetch, compute and upsert today's market-regime snapshot."""
    data = fetch_market_data(tickers)
    rates = fetch_fred_rates()
    macro = _macro_context(session)
    result = compute_regime_from_data(data, tickers, rates, macro)
    today = market_date()
    row = session.query(MarketRegimeSnapshot).filter(MarketRegimeSnapshot.snapshot_date == today).one_or_none()
    if row is None:
        row = MarketRegimeSnapshot(snapshot_date=today)
        session.add(row)

    spy, qqq, vol, breadth, rts = result["spy"], result["qqq"], result["volatility"], result["breadth"], result["rates"]
    values = {
        "snapped_at": datetime.utcnow(), "regime_score": result["score"], "regime_label": result["label"],
        "confidence": result["confidence"], "coverage": result["coverage"], "trend_score": result["trend_score"],
        "spy_price": spy.get("price"), "spy_return_20d": spy.get("return_20d"), "spy_return_60d": spy.get("return_60d"), "spy_trend_score": spy.get("score"),
        "qqq_price": qqq.get("price"), "qqq_return_20d": qqq.get("return_20d"), "qqq_return_60d": qqq.get("return_60d"), "qqq_trend_score": qqq.get("score"),
        "vix": vol.get("vix"), "vix_change_5d_pct": vol.get("change_5d_pct"), "vix_percentile_20d": vol.get("percentile_20d"), "vix_percentile_1y": vol.get("percentile_1y"), "volatility_state": vol.get("state"), "volatility_score": vol.get("score"),
        "breadth_above_20d": breadth.get("above_20d"), "breadth_above_50d": breadth.get("above_50d"), "breadth_above_200d": breadth.get("above_200d"), "breadth_positive_5d": breadth.get("positive_5d"), "breadth_positive_20d": breadth.get("positive_20d"), "breadth_score": breadth.get("score"), "breadth_universe_size": breadth.get("universe_size"),
        "two_year_yield": rts.get("two_year"), "ten_year_yield": rts.get("ten_year"), "curve_10y_2y": rts.get("curve_10y_2y"), "ten_year_5d_change_bps": rts.get("ten_year_5d_change_bps"), "ten_year_20d_change_bps": rts.get("ten_year_20d_change_bps"), "rates_score": rts.get("score"),
        "next_macro_event": macro.get("event"), "next_macro_at": macro.get("event_at"), "next_macro_importance": macro.get("importance"), "hours_to_macro": macro.get("hours"), "macro_risk_score": macro.get("risk_score"),
        "components": json.dumps(result, default=str),
    }
    for k, v in values.items(): setattr(row, k, v)
    session.commit()
    return result


def latest_market_regime(session) -> MarketRegimeSnapshot | None:
    return session.query(MarketRegimeSnapshot).order_by(MarketRegimeSnapshot.snapped_at.desc()).first()
