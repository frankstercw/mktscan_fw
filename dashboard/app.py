"""MktScan Decision Terminal v2.

A compact four-area Streamlit interface inspired by professional trading terminals:
TODAY -> RESEARCH -> KEY EVENTS -> PORTFOLIO -> VALIDATION.

The UI intentionally prioritizes conclusions and drill-down over dense raw tables.
"""
from __future__ import annotations

import calendar
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from sqlalchemy import desc, func, select

sys.path.insert(0, str(Path(__file__).parent.parent))

from mktscan.config import load_config
from mktscan.analyst_ratings import get_analyst_momentum
from mktscan.database import (
    AnalystRatingEvent,
    Article,
    Company,
    EarningsEvent,
    MarketRegimeSnapshot,
    MacroEvent,
    OptionsMarketSnapshot,
    PriceSnapshot,
    ScraperRun,
    TradeJournalEntry,
    TradeabilityOutcome,
    get_basket,
    get_session,
    init_db,
    seed_default_basket,
)
from mktscan.options import DISCLAIMER, generate_basket_setups
from mktscan.on_demand import normalize_ticker, run_on_demand_review
from mktscan.options_interpretation import interpret_options_market
from mktscan.terminal import iv_state, semantic_signal, setup_quality
from mktscan.trade_journal import close_trade, create_trade, mark_trade, trade_metrics

st.set_page_config(page_title="MktScan", page_icon="◈", layout="wide", initial_sidebar_state="expanded")

# ─────────────────────────────────────────────────────────────────────────────
# Trading-terminal visual system
# ─────────────────────────────────────────────────────────────────────────────
st.markdown(
    """
<style>
:root {
  --bg: #0d0f14;
  --panel: #131722;
  --panel2: #171b26;
  --border: #2a2e39;
  --text: #d1d4dc;
  --muted: #787b86;
  --blue: #2962ff;
  --green: #26a69a;
  --red: #ef5350;
  --amber: #f2b84b;
}
html, body, [data-testid="stAppViewContainer"] { background: var(--bg); color: var(--text); }
[data-testid="stHeader"] { background: rgba(13,15,20,.88); }
[data-testid="stSidebar"] { background: #10131a; border-right: 1px solid var(--border); }
.block-container { padding-top: 1.1rem; max-width: 1600px; }
h1,h2,h3,h4 { letter-spacing: -.02em; }
hr { border-color: var(--border) !important; }
.tv-card { background: var(--panel); border: 1px solid var(--border); border-radius: 7px; padding: 14px 16px; }
.tv-kicker { color: var(--muted); font-size: .72rem; text-transform: uppercase; letter-spacing: .09em; }
.tv-value { font-size: 1.18rem; font-weight: 650; margin-top: .15rem; }
.tv-small { color: var(--muted); font-size: .82rem; }
.tv-title { font-size: 1.05rem; font-weight: 650; }
.tv-bull { color: var(--green); }
.tv-bear { color: var(--red); }
.tv-warn { color: var(--amber); }
.tv-blue { color: #6e9bff; }
.tv-pill { display:inline-block; padding:2px 7px; margin-right:4px; border-radius:4px; background:#1f2430; border:1px solid #303746; font-size:.76rem; }
.tv-section { margin-top: .35rem; margin-bottom: .7rem; color:#9aa0ad; font-size:.78rem; font-weight:650; letter-spacing:.08em; text-transform:uppercase; }
[data-testid="stMetric"] { background:var(--panel); border:1px solid var(--border); border-radius:7px; padding:10px 12px; }
[data-testid="stMetricLabel"] { color:var(--muted); }
[data-testid="stDataFrame"] { border: 1px solid var(--border); border-radius:7px; overflow:hidden; }
.stButton > button { border-radius:5px; border:1px solid #3a4050; }
.stButton > button[kind="primary"] { background:var(--blue); border-color:var(--blue); }
div[data-baseweb="select"] > div { background:#151923; border-color:#343a46; }
</style>
""",
    unsafe_allow_html=True,
)


@st.cache_resource
def setup_app():
    try:
        load_config()
    except FileNotFoundError:
        pass
    init_db()
    s = get_session()
    try:
        seed_default_basket(s)
    finally:
        s.close()


setup_app()

# ─────────────────────────────────────────────────────────────────────────────
# Data access: cheap database paths first, network paths only on demand
# ─────────────────────────────────────────────────────────────────────────────
@st.cache_data(ttl=60, show_spinner=False)
def basket_tickers() -> list[str]:
    s = get_session()
    try:
        return [c.ticker for c in get_basket(s)]
    finally:
        s.close()


@st.cache_data(ttl=60, show_spinner=False)
def latest_regime():
    s = get_session()
    try:
        return s.execute(select(MarketRegimeSnapshot).order_by(desc(MarketRegimeSnapshot.snapped_at)).limit(1)).scalar_one_or_none()
    finally:
        s.close()


@st.cache_data(ttl=60, show_spinner=False)
def latest_signal_rows() -> dict[str, TradeabilityOutcome]:
    """Latest persisted signal per ticker. Keeps Today DB-only and fast."""
    s = get_session()
    try:
        rows = s.execute(select(TradeabilityOutcome).order_by(TradeabilityOutcome.ticker, desc(TradeabilityOutcome.predicted_at))).scalars().all()
        out = {}
        for r in rows:
            out.setdefault(r.ticker, r)
        return out
    finally:
        s.close()


@st.cache_data(ttl=60, show_spinner=False)
def latest_price_rows() -> dict[str, PriceSnapshot]:
    s = get_session()
    try:
        rows = s.execute(select(PriceSnapshot).order_by(PriceSnapshot.ticker, desc(PriceSnapshot.snapped_at))).scalars().all()
        out = {}
        for r in rows:
            out.setdefault(r.ticker, r)
        return out
    finally:
        s.close()


@st.cache_data(ttl=60, show_spinner=False)
def latest_options_rows() -> dict[str, OptionsMarketSnapshot]:
    s = get_session()
    try:
        rows = s.execute(select(OptionsMarketSnapshot).order_by(OptionsMarketSnapshot.ticker, desc(OptionsMarketSnapshot.snapped_at))).scalars().all()
        out = {}
        for r in rows:
            out.setdefault(r.ticker, r)
        return out
    finally:
        s.close()


@st.cache_data(ttl=120, show_spinner=False)
def analyst_activity(ticker: str, days: int = 60) -> tuple[list[dict], dict]:
    s = get_session()
    try:
        start = datetime.utcnow() - timedelta(days=days)
        rows = s.execute(
            select(AnalystRatingEvent)
            .where(
                AnalystRatingEvent.ticker == ticker.upper(),
                AnalystRatingEvent.published_at >= start,
            )
            .order_by(desc(AnalystRatingEvent.published_at))
        ).scalars().all()
        momentum = get_analyst_momentum(s, ticker, days=30)
        events = [{
            "published_at": r.published_at,
            "firm": r.firm,
            "analyst_name": r.analyst_name,
            "action_company": r.action_company,
            "action_pt": r.action_pt,
            "rating_prior": r.rating_prior,
            "rating_current": r.rating_current,
            "pt_prior": r.pt_prior,
            "pt_current": r.pt_current,
            "importance": r.importance,
            "url": r.url,
        } for r in rows]
        return events, momentum
    finally:
        s.close()


def _analyst_event_text(e: AnalystRatingEvent) -> str:
    firm = e.firm or "Analyst"
    company_action = (e.action_company or "").strip()
    pt_action = (e.action_pt or "").strip()
    parts = [firm]
    if company_action:
        parts.append(company_action)
    if e.rating_prior or e.rating_current:
        if e.rating_prior and e.rating_current and e.rating_prior != e.rating_current:
            parts.append(f"{e.rating_prior} → {e.rating_current}")
        elif e.rating_current:
            parts.append(str(e.rating_current))
    if pt_action:
        if e.pt_prior is not None and e.pt_current is not None:
            parts.append(f"{pt_action} PT ${e.pt_prior:.0f} → ${e.pt_current:.0f}")
        elif e.pt_current is not None:
            parts.append(f"{pt_action} PT ${e.pt_current:.0f}")
        else:
            parts.append(pt_action)
    return " · ".join(parts)


@st.cache_data(ttl=300, show_spinner=False)
def upcoming_earnings() -> dict[str, EarningsEvent]:
    s = get_session()
    try:
        now = datetime.utcnow()
        rows = s.execute(
            select(EarningsEvent)
            .where(EarningsEvent.report_date >= now)
            .order_by(EarningsEvent.report_date)
        ).scalars().all()
        out = {}
        for r in rows:
            out.setdefault(r.ticker, r)
        return out
    finally:
        s.close()


@st.cache_data(ttl=60, show_spinner=False)
def next_macro_event():
    s = get_session()
    try:
        now = datetime.utcnow()
        return s.execute(select(MacroEvent).where(MacroEvent.event_at >= now).order_by(MacroEvent.event_at).limit(1)).scalar_one_or_none()
    finally:
        s.close()


@st.cache_data(ttl=300, show_spinner=False)
def key_events_between(start_at: datetime, end_at: datetime) -> list[dict]:
    """Economic calendar + basket earnings for the requested calendar window."""
    s = get_session()
    try:
        macro_rows = s.execute(
            select(MacroEvent)
            .where(MacroEvent.event_at >= start_at, MacroEvent.event_at < end_at)
            .order_by(MacroEvent.event_at)
        ).scalars().all()
        earnings_rows = s.execute(
            select(EarningsEvent)
            .where(EarningsEvent.report_date >= start_at, EarningsEvent.report_date < end_at)
            .order_by(EarningsEvent.report_date)
        ).scalars().all()
        events = []
        for r in macro_rows:
            events.append({
                "kind": "ECON",
                "at": r.event_at,
                "title": r.name,
                "ticker": None,
                "importance": r.importance or "Normal",
                "source": r.source or "MarketWatch",
                "category": r.category or "Economic",
                "consensus": r.consensus,
                "prior": r.prior,
                "actual": r.actual,
            })
        for r in earnings_rows:
            events.append({
                "kind": "EARN",
                "at": r.report_date,
                "title": f"{r.ticker} Earnings",
                "ticker": r.ticker,
                "importance": "Company",
                "source": "Yahoo Finance",
                "category": "Earnings",
                "consensus": f"EPS {r.eps_estimate:.2f}" if r.eps_estimate is not None else None,
                "prior": None,
                "actual": f"EPS {r.eps_actual:.2f}" if r.eps_actual is not None else None,
            })
        return sorted(events, key=lambda x: x["at"])
    finally:
        s.close()


@st.cache_data(ttl=60, show_spinner=False)
def live_treasury_yields() -> dict[str, dict]:
    """Near-real-time 10Y/30Y Treasury yield proxies from Yahoo Finance CBOE yield indexes."""
    out = {}
    try:
        import yfinance as yf
        for label, symbol in (("10Y", "^TNX"), ("30Y", "^TYX")):
            t = yf.Ticker(symbol)
            fi = t.fast_info
            raw = getattr(fi, "last_price", None)
            prev = getattr(fi, "previous_close", None)
            current = float(raw) / 10.0 if raw is not None else None
            previous = float(prev) / 10.0 if prev is not None else None
            delta_bps = (current - previous) * 100.0 if current is not None and previous is not None else None
            out[label] = {
                "yield": current,
                "delta_bps": delta_bps,
                "symbol": symbol,
                "source": "Yahoo Finance / CBOE Treasury Yield Index",
                "as_of": datetime.utcnow(),
            }
    except Exception:
        pass

    if "10Y" not in out or out["10Y"].get("yield") is None:
        r = latest_regime()
        if r and r.ten_year_yield is not None:
            out["10Y"] = {
                "yield": float(r.ten_year_yield),
                "delta_bps": None,
                "symbol": "persisted",
                "source": "MktScan market-regime snapshot",
                "as_of": r.snapped_at,
            }
    return out



@st.cache_data(ttl=900, show_spinner=False)
def live_index_breadth() -> dict[str, dict]:
    """Compute SPX and QQQ breadth as % of constituents trading above their 50-day SMA.

    Constituents are sourced from public index-member tables and daily prices
    are downloaded from Yahoo Finance. Cached for 15 minutes because breadth
    does not need tick-level refresh.
    """
    import pandas as pd
    import yfinance as yf

    def _symbols(url: str, table_hint: str) -> list[str]:
        tables = pd.read_html(url)
        for frame in tables:
            cols = {str(c).strip().lower(): c for c in frame.columns}
            # S&P 500 table uses Symbol; Nasdaq-100 table typically uses Ticker.
            for candidate in ("symbol", "ticker"):
                if candidate in cols:
                    vals = frame[cols[candidate]].dropna().astype(str).str.strip().tolist()
                    if len(vals) >= 50:
                        return [v.replace(".", "-") for v in vals]
        raise ValueError(f"Could not find {table_hint} constituent table")

    def _breadth(symbols: list[str]) -> dict:
        raw = yf.download(
            symbols,
            period="4mo",
            interval="1d",
            auto_adjust=True,
            progress=False,
            threads=True,
            group_by="column",
        )
        if raw.empty:
            return {"pct_above_50d": None, "above": 0, "usable": 0, "state": "UNAVAILABLE"}

        close = raw["Close"] if isinstance(raw.columns, pd.MultiIndex) and "Close" in raw.columns.get_level_values(0) else raw
        above = 0
        usable = 0
        for symbol in symbols:
            try:
                s = close[symbol].dropna() if hasattr(close, "columns") else close.dropna()
                if len(s) < 50:
                    continue
                last = float(s.iloc[-1])
                ma50 = float(s.tail(50).mean())
                usable += 1
                above += int(last > ma50)
            except Exception:
                continue
        pct = (above / usable * 100.0) if usable else None
        state = "HEALTHY" if pct is not None and pct >= 60 else "WEAK" if pct is not None and pct < 40 else "MIXED" if pct is not None else "UNAVAILABLE"
        return {"pct_above_50d": pct, "above": above, "usable": usable, "state": state}

    out = {}
    try:
        spx = _symbols("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", "S&P 500")
        out["SPX"] = _breadth(spx)
        out["SPX"]["source"] = "S&P 500 constituents (Wikipedia) + Yahoo Finance adjusted daily prices"
    except Exception as exc:
        out["SPX"] = {"pct_above_50d": None, "above": 0, "usable": 0, "state": "UNAVAILABLE", "error": str(exc)}

    try:
        qqq = _symbols("https://en.wikipedia.org/wiki/Nasdaq-100", "Nasdaq-100")
        out["QQQ"] = _breadth(qqq)
        out["QQQ"]["source"] = "Nasdaq-100 constituents (Wikipedia) + Yahoo Finance adjusted daily prices"
    except Exception as exc:
        out["QQQ"] = {"pct_above_50d": None, "above": 0, "usable": 0, "state": "UNAVAILABLE", "error": str(exc)}

    return out


def equity_market_state(regime, breadth: dict[str, dict]) -> dict[str, str]:
    """Semantic equity-market summary used on Today."""
    trend = "NEUTRAL"
    momentum = "MIXED"
    if regime:
        spy_score = getattr(regime, "spy_trend_score", None)
        qqq_score = getattr(regime, "qqq_trend_score", None)
        vals = [float(v) for v in (spy_score, qqq_score) if v is not None]
        avg = sum(vals) / len(vals) if vals else 0.0
        trend = "BULLISH" if avg >= 0.20 else "BEARISH" if avg <= -0.20 else "NEUTRAL"

        spy_m = getattr(regime, "spy_return_20d", None)
        qqq_m = getattr(regime, "qqq_return_20d", None)
        mvals = [float(v) for v in (spy_m, qqq_m) if v is not None]
        mavg = sum(mvals) / len(mvals) if mvals else 0.0
        momentum = "STRONG" if mavg >= 2.0 else "WEAK" if mavg <= -2.0 else "MIXED"

    vals = [x.get("pct_above_50d") for x in breadth.values() if x.get("pct_above_50d") is not None]
    bavg = sum(vals) / len(vals) if vals else None
    breadth_state = "HEALTHY" if bavg is not None and bavg >= 60 else "WEAK" if bavg is not None and bavg < 40 else "MIXED" if bavg is not None else "UNAVAILABLE"

    vix_state = "NORMAL"
    if regime:
        vix = getattr(regime, "vix", None)
        if vix is not None:
            vix_state = "STRESS" if float(vix) >= 30 else "ELEVATED" if float(vix) >= 22 else "NORMAL"

    return {"Trend": trend, "Breadth": breadth_state, "VIX Structure": vix_state, "Momentum": momentum}


@st.cache_data(ttl=60, show_spinner=False)
def data_freshness() -> dict[str, datetime | None]:
    s = get_session()
    try:
        def mx(model, col):
            return s.execute(select(func.max(col))).scalar_one_or_none()
        return {
            "price": mx(PriceSnapshot, PriceSnapshot.snapped_at),
            "signal": mx(TradeabilityOutcome, TradeabilityOutcome.predicted_at),
            "options": mx(OptionsMarketSnapshot, OptionsMarketSnapshot.snapped_at),
            "regime": mx(MarketRegimeSnapshot, MarketRegimeSnapshot.snapped_at),
            "news": mx(Article, Article.scraped_at),
            "run": mx(ScraperRun, ScraperRun.finished_at),
        }
    finally:
        s.close()


@st.cache_data(ttl=300, show_spinner=False)
def technical_opportunity(ticker: str):
    from mktscan.terminal import technical_opportunity as _tech
    return _tech(ticker)


@st.cache_data(ttl=15, show_spinner=False)
def live_quote(ticker: str, feed: str, nonce: int = 0):
    from mktscan.providers.alpaca import AlpacaMarketDataClient
    return AlpacaMarketDataClient(feed=feed).get_quote(ticker)


@st.cache_data(ttl=15, show_spinner=False)
def live_bars(ticker: str, range_label: str, feed: str, nonce: int = 0) -> pd.DataFrame:
    from mktscan.live_charts import chart_window, prepare_chart_bars
    from mktscan.providers.alpaca import AlpacaMarketDataClient
    cfg, start, end = chart_window(range_label)
    raw = AlpacaMarketDataClient(feed=feed).get_bars(ticker, timeframe=cfg.timeframe, start=start, end=end, limit=cfg.max_bars)
    return prepare_chart_bars(raw, range_label)


@st.cache_data(ttl=600, show_spinner=False)
def live_tradeability() -> dict:
    from mktscan.tradeability import compute_basket_tradeability
    s = get_session()
    try:
        return compute_basket_tradeability(s)
    finally:
        s.close()


@st.cache_data(ttl=300, show_spinner=False)
def on_demand_review(ticker: str, nonce: int = 0) -> dict:
    """Ephemeral full MktScan review for a symbol outside the scheduled basket."""
    s = get_session()
    try:
        return run_on_demand_review(s, ticker)
    finally:
        s.close()


def _launch_custom_review():
    """Safe callback: executes before widget creation on the rerun."""
    try:
        ticker = normalize_ticker(st.session_state.get("ticker_lookup", ""))
    except ValueError as exc:
        st.session_state["ticker_lookup_error"] = str(exc)
        return
    st.session_state.pop("ticker_lookup_error", None)
    st.session_state["custom_ticker"] = ticker
    st.session_state["global_ticker"] = ticker
    st.session_state["area"] = "Research"
    st.session_state["research_section"] = "Summary"


@st.cache_data(ttl=120, show_spinner=False)
def change_feed(limit: int = 12) -> list[dict]:
    """Compact 'what changed' feed using persisted signals/options/regime."""
    s = get_session()
    events: list[dict] = []
    try:
        tickers = [c.ticker for c in get_basket(s)]
        for tk in tickers:
            sigs = s.execute(select(TradeabilityOutcome).where(TradeabilityOutcome.ticker == tk).order_by(desc(TradeabilityOutcome.predicted_at)).limit(2)).scalars().all()
            if len(sigs) == 2:
                a, b = sigs[0], sigs[1]
                if a.label_at_prediction != b.label_at_prediction:
                    events.append({"at": a.predicted_at, "ticker": tk, "text": f"Signal {b.label_at_prediction} → {a.label_at_prediction}", "kind": "signal"})
                elif abs(float(a.score_at_prediction) - float(b.score_at_prediction)) >= .15:
                    events.append({"at": a.predicted_at, "ticker": tk, "text": f"Signal moved {b.score_at_prediction:+.2f} → {a.score_at_prediction:+.2f}", "kind": "signal"})
            opts = s.execute(select(OptionsMarketSnapshot).where(OptionsMarketSnapshot.ticker == tk).order_by(desc(OptionsMarketSnapshot.snapped_at)).limit(2)).scalars().all()
            if len(opts) == 2 and opts[0].iv_percentile_1y is not None and opts[1].iv_percentile_1y is not None:
                delta = float(opts[0].iv_percentile_1y) - float(opts[1].iv_percentile_1y)
                if abs(delta) >= 15:
                    events.append({"at": opts[0].snapped_at, "ticker": tk, "text": f"IV percentile {opts[1].iv_percentile_1y:.0f} → {opts[0].iv_percentile_1y:.0f}", "kind": "iv"})
        regs = s.execute(select(MarketRegimeSnapshot).order_by(desc(MarketRegimeSnapshot.snapped_at)).limit(2)).scalars().all()
        if len(regs) == 2 and regs[0].regime_label != regs[1].regime_label:
            events.append({"at": regs[0].snapped_at, "ticker": "MARKET", "text": f"Regime {regs[1].regime_label} → {regs[0].regime_label}", "kind": "market"})

        analyst_cutoff = datetime.utcnow() - timedelta(days=2)
        watched_analyst = set(tickers)
        watched_analyst.update(
            s.execute(
                select(TradeJournalEntry.ticker).where(TradeJournalEntry.status == "OPEN")
            ).scalars().all()
        )
        analyst_rows = s.execute(
            select(AnalystRatingEvent)
            .where(
                AnalystRatingEvent.published_at >= analyst_cutoff,
                AnalystRatingEvent.ticker.in_(sorted(watched_analyst)),
            )
            .order_by(desc(AnalystRatingEvent.published_at))
            .limit(20)
        ).scalars().all()
        for a in analyst_rows:
            action = f"{a.action_company or ''} {a.action_pt or ''}".upper()
            if not any(k in action for k in ("UPGRADE", "DOWNGRADE", "INITIAT", "REINST", "RAISE", "LOWER", "CUT", "INCREASE", "DECREASE")):
                continue
            events.append({
                "at": a.published_at,
                "ticker": a.ticker,
                "text": _analyst_event_text(a),
                "kind": "analyst",
            })
    finally:
        s.close()
    events.sort(key=lambda x: x["at"] or datetime.min, reverse=True)
    return events[:limit]


def age_text(ts: datetime | None) -> str:
    if not ts:
        return "unavailable"
    now = datetime.utcnow()
    if ts.tzinfo is not None:
        now = datetime.now(timezone.utc)
    sec = max(0, (now - ts).total_seconds())
    if sec < 90: return f"{int(sec)}s ago"
    if sec < 3600: return f"{int(sec/60)}m ago"
    if sec < 86400: return f"{sec/3600:.1f}h ago"
    return f"{sec/86400:.1f}d ago"


def signal_color(label: str) -> str:
    u = str(label).upper()
    if "BULL" in u or "RISK_ON" in u: return "tv-bull"
    if "BEAR" in u or "RISK_OFF" in u: return "tv-bear"
    return ""


METRIC_HELP: dict[str, dict[str, str]] = {
    "Market": {"source": "MktScan MarketRegimeSnapshot", "definition": "Composite market-risk regime derived from SPY/QQQ trend, volatility, breadth, rates and macro context.", "interpret": "RISK_ON supports taking directional risk; RISK_OFF argues for more defensive sizing and stricter confirmation."},
    "VIX": {"source": "MktScan regime pipeline / VIX market data", "definition": "CBOE Volatility Index level used as a proxy for expected 30-day S&P 500 volatility.", "interpret": "Higher/rising VIX generally means more market stress and richer option volatility; context matters more than the absolute level alone."},
    "Open P&L": {"source": "MktScan Trade Journal", "definition": "Estimated P&L of currently open journal positions using the latest stored mark.", "interpret": "Positive is unrealized profit; stale marks can make this differ from brokerage P&L."},
    "Capital at Risk": {"source": "MktScan Trade Journal", "definition": "Sum of planned maximum loss across open positions.", "interpret": "Use it as a portfolio risk budget, not as a forecast of likely loss."},
    "10Y Treasury": {"source": "Yahoo Finance / CBOE ^TNX; fallback to MktScan regime snapshot", "definition": "Near-real-time U.S. 10-year Treasury yield proxy.", "interpret": "Rapidly rising long rates can pressure long-duration/growth equities; falling yields can ease valuation pressure. Watch the direction and basis-point change."},
    "30Y Treasury": {"source": "Yahoo Finance / CBOE ^TYX", "definition": "Near-real-time U.S. 30-year Treasury yield proxy.", "interpret": "Useful for tracking the long end of the curve and long-run growth/inflation expectations. Compare it with the 10Y and its daily change."},
    "Equity Trend": {"source": "MktScan MarketRegimeSnapshot using SPY and QQQ trend components", "definition": "Broad equity trend state synthesized from SPY and QQQ trend scores.", "interpret": "BULLISH means the major equity benchmarks are broadly trend-supportive; BEARISH means the tape is a headwind for long momentum setups."},
    "Equity Breadth": {"source": "MktScan SPX + QQQ breadth calculations", "definition": "Combined participation state based on the share of S&P 500 and Nasdaq-100 constituents above their 50-day moving averages.", "interpret": "HEALTHY (roughly ≥60%) means broad participation; MIXED (40–60%) means selective participation; WEAK (<40%) means a narrow/fragile tape."},
    "SPX Breadth": {"source": "S&P 500 constituent list + Yahoo Finance adjusted daily prices", "definition": "Percentage of usable S&P 500 constituents trading above their 50-day simple moving average.", "interpret": "≥60% = healthy broad participation; 40–60% = mixed; <40% = weak. Rising breadth confirms index rallies; falling breadth can warn that gains are narrowing."},
    "QQQ Breadth": {"source": "Nasdaq-100 constituent list + Yahoo Finance adjusted daily prices", "definition": "Percentage of usable Nasdaq-100 constituents trading above their 50-day simple moving average.", "interpret": "Especially relevant to growth/technology momentum. ≥60% is healthy, 40–60% mixed, <40% weak. Compare with QQQ price trend to detect narrow mega-cap leadership."},
    "VIX Structure": {"source": "MktScan volatility regime / VIX market data", "definition": "Current equity-volatility state used in the Equity Market summary.", "interpret": "NORMAL supports ordinary risk-taking; ELEVATED calls for more caution; STRESS indicates unusually high equity volatility. A future VIX-term-structure feed can refine this further."},
    "Equity Momentum": {"source": "MktScan MarketRegimeSnapshot using SPY/QQQ momentum", "definition": "Broad-market momentum state synthesized from recent SPY and QQQ returns.", "interpret": "STRONG supports continuation/momentum setups; MIXED means less confirmation; WEAK suggests deteriorating broad-market momentum."},
    "Setup": {"source": "MktScan setup-quality heuristic", "definition": "Qualitative ranking combining signal strength, regime alignment, IV context, event risk and technical state.", "interpret": "HIGH means several independent dimensions align; it is a prioritization aid, not a validated probability."},
    "Signal": {"source": "MktScan TradeabilityOutcome", "definition": "Semantic translation of the latest persisted tradeability score.", "interpret": "Bullish/bearish indicates direction; stronger labels indicate larger score magnitude."},
    "Score": {"source": "MktScan tradeability model", "definition": "Composite directional score, typically ranging from -1 to +1.", "interpret": "Positive favors bullish direction, negative favors bearish direction; compare magnitude and validation history rather than treating a cutoff as certainty."},
    "IV": {"source": "MktScan OptionsMarketSnapshot / option-chain IV history", "definition": "Semantic state of the ticker's current implied-volatility percentile.", "interpret": "LOW IV can favor debit structures; HIGH IV can favor defined-risk premium-selling structures when the directional thesis supports them."},
    "Regime": {"source": "MktScan MarketRegimeSnapshot", "definition": "Current broad-market risk environment.", "interpret": "Use it to judge whether the broad tape supports or conflicts with a ticker-level setup."},
    "Risk": {"source": "MktScan EarningsEvent and event logic", "definition": "Near-term event warning shown for the setup.", "interpret": "An event close to the trade horizon can dominate normal technical/volatility behavior and deserves explicit planning."},
    "Price": {"source": "MktScan PriceSnapshot; intraday charts use Alpaca", "definition": "Latest persisted underlying price for scanner views.", "interpret": "Use with freshness timestamp; persisted scanner price is not necessarily tick-by-tick."},
    "Change %": {"source": "MktScan PriceSnapshot", "definition": "Latest stored daily percentage price change.", "interpret": "Large moves can confirm momentum but may also indicate extension; compare with volume and expected move."},
    "Underlying return": {"source": "MktScan Trade Journal", "definition": "Percentage change in the underlying between journal entry and exit prices.", "interpret": "Compare it with trade P&L to separate directional thesis quality from option-structure/execution quality."},
    "Trade P&L": {"source": "MktScan Trade Journal", "definition": "Realized dollar profit or loss recorded for a closed journal trade.", "interpret": "Evaluate alongside return on risk and thesis correctness; dollar P&L alone is scale-dependent."},
    "Return on risk": {"source": "MktScan Trade Journal", "definition": "Trade P&L divided by planned/defined capital at risk.", "interpret": "Higher positive ROR indicates more efficient use of risk capital; compare across strategies with similar holding periods."},
    "Net P&L": {"source": "MktScan Trade Journal", "definition": "Sum of realized P&L across closed journal trades.", "interpret": "Measures total realized dollars but does not normalize for capital, time or changing position size."},
    "Trades": {"source": "MktScan Trade Journal", "definition": "Number of closed trades included in the performance sample.", "interpret": "Larger samples make performance metrics more informative; small samples are noisy."},
    "Win Rate": {"source": "MktScan Trade Journal", "definition": "Percentage of closed trades with positive realized P&L.", "interpret": "Higher is not automatically better; interpret with average win/loss and profit factor."},
    "Profit Factor": {"source": "MktScan Trade Journal", "definition": "Gross profits divided by absolute gross losses.", "interpret": ">1 means gross profits exceed gross losses; values materially above 1 are stronger, but sample size matters."},
    "Resolved signals": {"source": "MktScan TradeabilityOutcome", "definition": "Number of historical signals with a realized forward outcome.", "interpret": "This is the calibration sample size; more observations generally increase confidence in validation results."},
    "Directional accuracy": {"source": "MktScan TradeabilityOutcome", "definition": "Share of resolved signals whose score direction matched the sign of the subsequent return.", "interpret": ">50% can be useful depending on payoff asymmetry, but accuracy alone does not establish profitability."},
    "Positions": {"source": "MktScan Trade Journal", "definition": "Count of currently open journal positions.", "interpret": "Use with capital at risk and concentration; position count can understate risk when trades are highly correlated."},
    "Net Bias": {"source": "MktScan Trade Journal", "definition": "Simple directional balance of open bullish versus bearish journal positions.", "interpret": "Shows portfolio directional tilt; it is not delta-weighted until live contract Greeks are integrated."},
    "Analyst Momentum": {"source": "Benzinga Analyst Ratings API via MktScan", "definition": "30-day weighted score of upgrades/downgrades, bullish/bearish initiations and price-target changes.", "interpret": "Positive means recent sell-side actions skew constructive; negative means deteriorating analyst sentiment. Treat as a catalyst/confirmation layer, not a standalone trading signal."},
    "Analyst Events": {"source": "Benzinga Analyst Ratings API", "definition": "Number of analyst rating/price-target events stored for the selected 30-day window.", "interpret": "Higher event count means more sell-side activity; interpret the direction and quality of actions rather than treating activity itself as bullish or bearish."},
    "Upgrades": {"source": "Benzinga Analyst Ratings API", "definition": "Count of analyst upgrades during the trailing 30 days.", "interpret": "A cluster of upgrades can confirm improving sentiment, especially when price/volume momentum agrees."},
    "Downgrades": {"source": "Benzinga Analyst Ratings API", "definition": "Count of analyst downgrades during the trailing 30 days.", "interpret": "Multiple downgrades can flag deteriorating expectations; compare with price reaction and whether downgrades are already discounted."},
    "PT Raises": {"source": "Benzinga Analyst Ratings API", "definition": "Count of analyst price-target increases during the trailing 30 days.", "interpret": "Repeated target raises suggest improving earnings/valuation expectations, but target changes are weaker evidence than rating changes."},
    "PT Cuts": {"source": "Benzinga Analyst Ratings API", "definition": "Count of analyst price-target reductions during the trailing 30 days.", "interpret": "Repeated target cuts can be a caution signal, particularly if momentum and estimates are also weakening."},
    "Setup Quality": {"source": "MktScan terminal.setup_quality", "definition": "Workflow heuristic combining tradeability, regime, technical state, IV and event proximity.", "interpret": "Use HIGH/MODERATE/LOW to prioritize review. It is intentionally not presented as a calibrated probability."},
    "Trend": {"source": "MktScan technical_opportunity from underlying OHLCV", "definition": "Trend state derived from moving averages and ADX/trend-strength context.", "interpret": "Strong bullish/bearish trend alignment supports directional trades; weak trend argues for lower conviction."},
    "Momentum": {"source": "MktScan technical_opportunity from underlying OHLCV", "definition": "Momentum state derived from recent returns, acceleration and RSI context.", "interpret": "Accelerating momentum supports continuation setups; decelerating momentum can warn that a move is losing force."},
    "Relative Strength": {"source": "MktScan technical_opportunity vs SPY/QQQ", "definition": "Ticker performance relative to broad-market benchmarks over the configured lookback.", "interpret": "Positive/strong relative strength means the ticker is outperforming its benchmark, useful confirmation for long momentum setups."},
    "Volume": {"source": "Underlying OHLCV via MktScan technical pipeline", "definition": "Volume confirmation state using relative volume versus recent history.", "interpret": "Higher RVOL can validate breakouts/momentum; low volume makes price moves less convincing."},
    "ATM IV": {"source": "MktScan OptionsMarketSnapshot from option-chain data", "definition": "Implied volatility of options nearest at-the-money.", "interpret": "Higher ATM IV means the market prices a wider future return distribution and generally richer option premiums."},
    "IV Percentile": {"source": "MktScan IV history / OptionsMarketSnapshot", "definition": "Percent of trailing historical IV observations below the current IV.", "interpret": "Low percentile means IV is historically inexpensive; high percentile means it is historically rich."},
    "Term": {"source": "MktScan OptionsMarketSnapshot", "definition": "Shape of implied volatility across 30D, 60D and 90D expirations.", "interpret": "Backwardation signals expensive near-term volatility/event risk; contango is a more normal upward-sloping curve."},
    "Put Skew": {"source": "MktScan OptionsMarketSnapshot", "definition": "25-delta put IV relative to ATM IV.", "interpret": "Positive/rising put skew means downside protection is carrying a volatility premium."},
    "Expected Move": {"source": "MktScan OptionsMarketSnapshot", "definition": "Option-implied magnitude of the underlying move over the selected horizon.", "interpret": "Treat it as the move embedded in option pricing, not a directional forecast. Compare it with your own thesis."},
    "Structure": {"source": "MktScan options strategy engine", "definition": "Option structure selected for the current directional and volatility context.", "interpret": "Use as a starting structure to evaluate; confirm payoff, liquidity, event risk and sizing before trading."},
    "Cost / Credit": {"source": "MktScan options strategy engine / option-chain marks", "definition": "Estimated debit paid or credit received for one standard option spread/position.", "interpret": "For debit trades it is capital spent; for credit trades compare the credit with maximum defined loss."},
    "Max Loss": {"source": "MktScan options payoff model", "definition": "Estimated worst-case loss for the proposed defined-risk option structure.", "interpret": "Use this as the core position-sizing input; never size solely from premium paid/received if max risk differs."},
    "Breakeven": {"source": "MktScan options payoff model", "definition": "Underlying price at expiration where the proposed option position has approximately zero P&L.", "interpret": "Compare breakeven with spot, expected move and your price target to judge whether the thesis has enough room."},
}

def metric_help(label: str, source: str | None = None, definition: str | None = None, interpret: str | None = None) -> str:
    d = METRIC_HELP.get(label, {})
    src = source or d.get("source", "MktScan")
    defin = definition or d.get("definition", f"{label} metric used by the MktScan decision workflow.")
    interp = interpret or d.get("interpret", "Interpret in context with the surrounding signal, regime, event risk and data freshness.")
    return f"Source: {src}\\n\\nDefinition: {defin}\\n\\nHow to interpret: {interp}"

def ui_metric(container, label: str, value, delta=None, **kwargs):
    kwargs.setdefault("help", metric_help(label))
    return container.metric(label, value, delta=delta, **kwargs)

def card(label: str, value: str, sub: str = "", cls: str = "", help_text: str | None = None):
    tip = (help_text or metric_help(label)).replace('"', '&quot;').replace("\\n", " • ")
    st.markdown(
        f'<div class="tv-card" title="{tip}"><div class="tv-kicker">{label} ⓘ</div>'
        f'<div class="tv-value {cls}">{value}</div><div class="tv-small">{sub}</div></div>',
        unsafe_allow_html=True,
    )

def dataframe_with_help(df: pd.DataFrame, help_overrides: dict[str, str] | None = None, **kwargs):
    overrides = help_overrides or {}
    cfg = {}
    for col in df.columns:
        cfg[col] = st.column_config.Column(col, help=overrides.get(col, metric_help(str(col))))
    return st.dataframe(df, column_config=cfg, **kwargs)


def nav_to(area: str, ticker: str | None = None, section: str | None = None):
    """Safe Streamlit navigation callback. Called via on_click before rerun."""
    if ticker:
        st.session_state["global_ticker"] = ticker
    st.session_state["area"] = area
    if section:
        st.session_state["research_section"] = section


def nav_to_journal(ticker: str, strategy: str = ""):
    st.session_state["journal_prefill_ticker"] = ticker
    st.session_state["journal_prefill_strategy"] = strategy
    st.session_state["portfolio_view"] = "Journal"
    nav_to("Portfolio", ticker)


def _event_days(row: EarningsEvent | None) -> int | None:
    if not row or not row.report_date:
        return None
    return max(0, (row.report_date.date() - date.today()).days)


def build_decision(ticker: str, sig, regime, tech, opt, earn) -> dict:
    score = float(sig.score_at_prediction) if sig else None
    coverage = 0.75 if sig else 0.0  # persisted outcomes currently do not store coverage
    ivpct = float(opt.iv_percentile_1y) if opt and opt.iv_percentile_1y is not None else None
    quality = setup_quality(score, coverage, regime.regime_label if regime else None, tech, ivpct, _event_days(earn))
    options_view = interpret_options_market(opt, score, sig.label_at_prediction if sig else None) if opt else None
    risks = list(quality["risks"])
    if opt and opt.term_state == "BACKWARDATION": risks.append("Near-term IV is elevated")
    status = "READY" if quality["label"] == "HIGH" and not any("risk in" in r.lower() for r in risks) else "WATCH" if quality["label"] != "LOW" else "AVOID"
    return {"quality": quality, "options": options_view, "status": status, "score": score, "risks": risks[:4]}


# ─────────────────────────────────────────────────────────────────────────────

@st.cache_data(ttl=900, show_spinner=False)
def market_performance_history(tickers: tuple[str, ...], trading_days: int = 14):
    """Old-dashboard-style rolling daily performance table for the full basket."""
    import yfinance as yf

    if not tickers:
        return None, None
    end = date.today()
    start = end - timedelta(days=35)
    raw = yf.download(
        list(tickers),
        start=str(start),
        end=str(end + timedelta(days=1)),
        progress=False,
        auto_adjust=True,
        group_by="column",
        threads=True,
    )
    if raw is None or raw.empty:
        return None, None
    if isinstance(raw.columns, pd.MultiIndex):
        closes = raw["Close"]
    else:
        closes = raw[["Close"]].rename(columns={"Close": tickers[0]})
    closes = closes.dropna(how="all").tail(trading_days)
    if closes.empty:
        return None, None
    pct = closes.pct_change() * 100.0
    return pct.iloc[1:], closes.iloc[-1]


# Global context + four-area navigation
# ─────────────────────────────────────────────────────────────────────────────
basket_symbols = basket_tickers()
if not basket_symbols:
    st.error("No basket tickers are configured.")
    st.stop()

custom_ticker = st.session_state.get("custom_ticker")
tickers = list(basket_symbols)
if custom_ticker and custom_ticker not in tickers:
    tickers.append(custom_ticker)

if st.session_state.get("global_ticker") not in tickers:
    st.session_state["global_ticker"] = tickers[0]
if st.session_state.get("area") not in {"Today", "Market Performance", "Research", "Key Events", "Portfolio", "Validation"}:
    st.session_state["area"] = "Today"

with st.sidebar:
    st.markdown("### ◈ MktScan")
    st.caption("Decision Terminal")
    st.text_input("Analyze any ticker", key="ticker_lookup", placeholder="e.g. PLTR, JPM, BRK-B")
    st.button("Run full review", type="primary", use_container_width=True, on_click=_launch_custom_review)
    if st.session_state.get("ticker_lookup_error"):
        st.error(st.session_state["ticker_lookup_error"])
    st.caption("Ad-hoc symbols run on demand and are not added to the scheduled basket.")
    st.selectbox("Ticker", tickers, key="global_ticker")
    st.radio("", ["Today", "Market Performance", "Research", "Key Events", "Portfolio", "Validation"], key="area", label_visibility="collapsed")
    st.divider()
    fresh = data_freshness()
    st.caption("DATA FRESHNESS")
    st.caption(f"Price · {age_text(fresh['price'])}")
    st.caption(f"Signals · {age_text(fresh['signal'])}")
    st.caption(f"Options · {age_text(fresh['options'])}")
    st.caption(f"Regime · {age_text(fresh['regime'])}")
    if st.button("↻ Refresh", use_container_width=True):
        st.cache_data.clear(); st.rerun()
    with st.expander("System"):
        st.caption("Expensive/admin actions stay out of the main workflow.")
        if st.button("Run scraper now", use_container_width=True):
            try:
                from mktscan.engine import MktScanEngine
                with st.spinner("Running scraper…"):
                    r = MktScanEngine().run("all")
                st.success(f"Run complete: {getattr(r, 'tickers_scored', 'done')}")
                st.cache_data.clear()
            except Exception as e:
                st.error(str(e))

area = st.session_state["area"]
ticker = st.session_state["global_ticker"]
regime = latest_regime()
signals = latest_signal_rows()
prices = latest_price_rows()
options = latest_options_rows()
earnings = upcoming_earnings()

# ─────────────────────────────────────────────────────────────────────────────
# TODAY — orient, rank, act
# ─────────────────────────────────────────────────────────────────────────────
if area == "Today":
    st.markdown("## Today")
    st.caption("Market orientation, opportunities, open-position warnings, and meaningful changes.")

    open_s = get_session()
    try:
        open_trades = open_s.execute(select(TradeJournalEntry).where(TradeJournalEntry.status == "OPEN").order_by(desc(TradeJournalEntry.opened_at))).scalars().all()
    finally:
        open_s.close()
    marked_pnl = sum((trade_metrics(t, use_current=True).pnl or 0) for t in open_trades)
    capital_risk = sum(float(t.planned_max_loss or 0) for t in open_trades)
    treasuries = live_treasury_yields()

    c1,c2,c3,c4 = st.columns(4)
    with c1: card("Market", regime.regime_label if regime else "UNKNOWN", f"confidence {(regime.confidence or 0):.0%}" if regime else "", signal_color(regime.regime_label if regime else ""))
    with c2: card("VIX", f"{regime.vix:.1f}" if regime and regime.vix is not None else "—", regime.volatility_state if regime else "")
    with c3:
        t10 = treasuries.get("10Y", {})
        y10 = t10.get("yield")
        d10 = t10.get("delta_bps")
        card("10Y Treasury", f"{y10:.3f}%" if y10 is not None else "—", f"{d10:+.1f} bps vs prev close" if d10 is not None else "near-real-time")
    with c4:
        t30 = treasuries.get("30Y", {})
        y30 = t30.get("yield")
        d30 = t30.get("delta_bps")
        card("30Y Treasury", f"{y30:.3f}%" if y30 is not None else "—", f"{d30:+.1f} bps vs prev close" if d30 is not None else "near-real-time")

    with st.expander("Review a ticker outside the basket"):
        st.caption("Use the sidebar **Analyze any ticker** box to run the full MktScan review without changing the scheduled basket.")

    st.markdown('<div class="tv-section">Equity Market</div>', unsafe_allow_html=True)
    with st.spinner("Updating SPX / QQQ breadth…"):
        index_breadth = live_index_breadth()
    eq_state = equity_market_state(regime, index_breadth)

    spx_b = index_breadth.get("SPX", {})
    qqq_b = index_breadth.get("QQQ", {})
    spx_pct = spx_b.get("pct_above_50d")
    qqq_pct = qqq_b.get("pct_above_50d")

    equity_rows = pd.DataFrame([
        {"Metric": "Trend", "Value": eq_state["Trend"]},
        {"Metric": "Breadth", "Value": eq_state["Breadth"]},
        {"Metric": "SPX Breadth", "Value": f"{spx_pct:.1f}% above 50DMA" if spx_pct is not None else "Unavailable"},
        {"Metric": "QQQ Breadth", "Value": f"{qqq_pct:.1f}% above 50DMA" if qqq_pct is not None else "Unavailable"},
        {"Metric": "VIX Structure", "Value": eq_state["VIX Structure"]},
        {"Metric": "Momentum", "Value": eq_state["Momentum"]},
    ])

    # Table headers expose the interpretation framework; each metric also gets
    # an explicit expandable tooltip-style help row so touch users do not rely
    # on hover alone.
    dataframe_with_help(
        equity_rows,
        help_overrides={
            "Metric": "Each row is a market-state metric. Open the interpretation guide directly below for Source, Definition, and How to interpret each metric.",
            "Value": "Current semantic state or breadth percentage. Breadth percentages represent constituents above their 50-day moving average.",
        },
        use_container_width=True,
        hide_index=True,
    )
    with st.expander("ⓘ How to interpret Equity Market metrics"):
        for label, help_key in [
            ("Trend", "Equity Trend"),
            ("Breadth", "Equity Breadth"),
            ("SPX Breadth", "SPX Breadth"),
            ("QQQ Breadth", "QQQ Breadth"),
            ("VIX Structure", "VIX Structure"),
            ("Momentum", "Equity Momentum"),
        ]:
            st.markdown(f"**{label}**")
            st.caption(metric_help(help_key))

    st.markdown('<div class="tv-section">Top opportunities</div>', unsafe_allow_html=True)
    rows=[]
    for tk in tickers:
        sig=signals.get(tk); p=prices.get(tk); opt=options.get(tk); earn=earnings.get(tk)
        score=float(sig.score_at_prediction) if sig else None
        ivpct=float(opt.iv_percentile_1y) if opt and opt.iv_percentile_1y is not None else (float(p.iv_percentile) if p and p.iv_percentile is not None else None)
        # Today stays fast: use persisted signal/price/options only. Technicals load after drilldown.
        qual=setup_quality(score, .75 if sig else 0, regime.regime_label if regime else None, None, ivpct, _event_days(earn))
        rows.append({
            "Ticker":tk, "Setup":qual["label"], "Signal":semantic_signal(score), "Score":round(score,2) if score is not None else None,
            "IV":iv_state(ivpct), "Regime":regime.regime_label if regime else "UNKNOWN",
            "Risk":f"Earnings {_event_days(earn)}d" if _event_days(earn) is not None and _event_days(earn)<=14 else "—",
            "Price":round(float(p.price),2) if p and p.price is not None else None,
            "Change %":round(float(p.change_pct),2) if p and p.change_pct is not None else None,
            "_q":qual["score"],
        })
    df=pd.DataFrame(rows).sort_values(["_q","Score"], ascending=[False,False])
    f1,f2=st.columns([1,3])
    with f1: min_setup=st.selectbox("Minimum setup", ["All","Moderate+","High only"], index=1)
    show=df.copy()
    if min_setup=="Moderate+": show=show[show["Setup"].isin(["MODERATE","HIGH"])]
    elif min_setup=="High only": show=show[show["Setup"]=="HIGH"]
    dataframe_with_help(show.drop(columns=["_q"]), use_container_width=True, hide_index=True, height=min(500, 55+35*max(1,len(show))))
    if not show.empty:
        pick=st.selectbox("Review opportunity", show["Ticker"].tolist(), key="today_pick")
        st.button("Open Research", type="primary", on_click=nav_to, args=("Research",pick,"Summary"))

    st.markdown('<div class="tv-section">What changed</div>', unsafe_allow_html=True)
    changes=change_feed()
    if changes:
        for e in changes[:8]:
            st.markdown(f"**{e['ticker']}** · {e['text']}  \n<span class='tv-small'>{age_text(e['at'])}</span>", unsafe_allow_html=True)
    else:
        st.caption("No material stored signal/IV/regime changes detected yet.")

    if open_trades:
        st.markdown('<div class="tv-section">Open position warnings</div>', unsafe_allow_html=True)
        for t in open_trades:
            ed=_event_days(earnings.get(t.ticker))
            msgs=[]
            if ed is not None and ed<=7: msgs.append(f"earnings in {ed}d")
            current_sig=signals.get(t.ticker)
            if current_sig and t.direction=="BULLISH" and current_sig.score_at_prediction<-.2: msgs.append("signal flipped bearish")
            if current_sig and t.direction=="BEARISH" and current_sig.score_at_prediction>.2: msgs.append("signal flipped bullish")
            if msgs: st.warning(f"{t.ticker} · {t.strategy}: " + "; ".join(msgs))

# ─────────────────────────────────────────────────────────────────────────────
# MARKET PERFORMANCE — rolling 2-week basket history
# ─────────────────────────────────────────────────────────────────────────────
elif area == "Market Performance":
    st.markdown("## Market Performance")
    st.caption("Rolling daily performance across the entire configured basket. Yahoo Finance adjusted daily prices; cached for 15 minutes.")

    with st.spinner("Loading basket price history…"):
        pct_df, last_prices = market_performance_history(tuple(basket_symbols), trading_days=14)

    if pct_df is None or pct_df.empty:
        st.info("Price history is temporarily unavailable.")
    else:
        cols_ordered = [t for t in basket_symbols if t in pct_df.columns]
        pct_df = pct_df[cols_ordered]
        dates_fmt = [d.strftime("%b %d") for d in pct_df.index]

        fig_heat = go.Figure(go.Heatmap(
            z=pct_df.values.tolist(),
            x=cols_ordered,
            y=dates_fmt,
            colorscale=[
                [0.0, "#ef4444"],
                [0.35, "#fca5a5"],
                [0.50, "#131722"],
                [0.65, "#86efac"],
                [1.0, "#22d3a0"],
            ],
            zmid=0,
            zmin=-5,
            zmax=5,
            text=[[f"{v:+.2f}%" if pd.notna(v) else "—" for v in row] for row in pct_df.values.tolist()],
            texttemplate="%{text}",
            textfont=dict(size=11),
            hovertemplate="<b>%{x}</b> · %{y}<br>Daily change: %{text}<extra></extra>",
            colorbar=dict(title="% Chg", ticksuffix="%", thickness=12, len=0.8),
        ))
        fig_heat.update_layout(
            height=max(360, len(dates_fmt) * 28),
            margin=dict(l=10, r=60, t=10, b=10),
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            font=dict(color="#94a3b8"),
            xaxis=dict(side="top", tickangle=0),
            yaxis=dict(autorange="reversed"),
        )
        st.plotly_chart(fig_heat, use_container_width=True)

        rows = []
        for tk in cols_ordered:
            series = pct_df[tk].dropna()
            if series.empty:
                continue
            total = ((1 + series / 100.0).prod() - 1) * 100.0
            rows.append({
                "Ticker": tk,
                "Last Price": float(last_prices[tk]) if last_prices is not None and tk in last_prices.index and pd.notna(last_prices[tk]) else None,
                "2W Return %": total,
                "Up Days": int((series > 0).sum()),
                "Down Days": int((series < 0).sum()),
                "Best Day %": float(series.max()),
                "Worst Day %": float(series.min()),
                "Avg Daily %": float(series.mean()),
            })
        summary_df = pd.DataFrame(rows).sort_values("2W Return %", ascending=False)

        st.markdown('<div class="tv-section">2-week ranking</div>', unsafe_allow_html=True)
        dataframe_with_help(
            summary_df,
            help_overrides={
                "Ticker": "Source: MktScan configured basket. Definition: Security symbol. How to interpret: Compare each name against peers in the same basket.",
                "Last Price": "Source: Yahoo Finance adjusted daily close. Definition: Most recent adjusted close in this history. How to interpret: End-of-day reference price, not an intraday quote.",
                "2W Return %": "Source: Yahoo Finance adjusted daily closes. Definition: Compounded return over the displayed rolling window. How to interpret: Higher positive values identify recent leaders; large negatives identify laggards.",
                "Up Days": "Source: Yahoo Finance. Definition: Positive-return sessions in the window. How to interpret: Many up days suggest persistent participation rather than one isolated gap.",
                "Down Days": "Source: Yahoo Finance. Definition: Negative-return sessions in the window. How to interpret: Persistent down days can reveal weakness hidden by one large rebound.",
                "Best Day %": "Source: Yahoo Finance. Definition: Largest daily gain in the window. How to interpret: A very large best day can indicate catalyst-driven or unstable performance.",
                "Worst Day %": "Source: Yahoo Finance. Definition: Largest daily loss in the window. How to interpret: Useful quick read of recent downside shock risk.",
                "Avg Daily %": "Source: Yahoo Finance. Definition: Arithmetic average daily return. How to interpret: Helps distinguish sustained drift from a return dominated by one outlier day.",
            },
            use_container_width=True,
            hide_index=True,
        )

        with st.expander("ⓘ How to interpret Market Performance"):
            st.markdown(
                "**Heatmap:** green = positive day, red = negative day. Look for repeated clusters rather than one isolated cell.\n\n"
                "**2W Return + Up Days:** together they separate persistent momentum from a single event-driven gap.\n\n"
                "**Best/Worst Day:** highlights names whose recent return path is unusually volatile."
            )

# ─────────────────────────────────────────────────────────────────────────────
# KEY EVENTS — combined legacy economic + earnings calendars
# ─────────────────────────────────────────────────────────────────────────────
elif area == "Key Events":
    st.markdown("## Key Events")
    st.caption(
        "The old MktScan Economic Calendar and Earnings Calendar are combined here. "
        "Economic events come from the persisted macro calendar; earnings come from "
        "the configured MktScan basket's Yahoo earnings calendar."
    )

    # Refresh controls mirror the old dashboard's two data feeds.
    rc1, rc2, rc3 = st.columns([1, 1, 3])
    with rc1:
        if st.button("Refresh Economic", use_container_width=True):
            try:
                from mktscan.macro import refresh_economic_calendar
                s = get_session()
                try:
                    with st.spinner("Refreshing economic calendar…"):
                        result = refresh_economic_calendar(s)
                finally:
                    s.close()
                key_events_between.clear()
                total = result.get("marketwatch_events", 0) + result.get("benzinga_events", 0)
                if total:
                    st.success(f"{total} economic events refreshed via {result.get('source') or 'calendar provider'}.")
                else:
                    st.warning("No economic events were returned by MarketWatch or the configured fallback.")
            except Exception as exc:
                st.error(f"Economic refresh failed: {exc}")

    with rc2:
        if st.button("Refresh Earnings", use_container_width=True):
            try:
                from mktscan.earnings_calendar import refresh_earnings_calendar
                s = get_session()
                try:
                    with st.spinner("Refreshing basket earnings dates…"):
                        result = refresh_earnings_calendar(s, basket_symbols)
                finally:
                    s.close()
                key_events_between.clear()
                if result.get("upcoming", 0):
                    st.success(
                        f"{result['upcoming']} upcoming earnings events refreshed "
                        f"across {result['tickers']} basket tickers."
                    )
                else:
                    st.warning(
                        "Yahoo returned no upcoming earnings dates for the current basket. "
                        "The stored calendar below will still show any existing events."
                    )
            except Exception as exc:
                st.error(f"Earnings refresh failed: {exc}")

    with rc3:
        st.caption(
            "Economic: MarketWatch primary, Benzinga Economics fallback when entitled. "
            "Earnings: Yahoo Finance for the configured MktScan basket."
        )

    view = st.radio(
        "Calendar view",
        ["Combined Calendar", "Economic Calendar", "Earnings Calendar"],
        horizontal=True,
        label_visibility="collapsed",
    )

    today = date.today()
    months = []
    for offset in range(-1, 7):
        y = today.year + (today.month - 1 + offset) // 12
        m = (today.month - 1 + offset) % 12 + 1
        months.append((y, m))
    labels = [datetime(y, m, 1).strftime("%B %Y") for y, m in months]
    default_idx = next(
        (i for i, (y, m) in enumerate(months) if y == today.year and m == today.month),
        0,
    )
    chosen = st.selectbox("Calendar month", labels, index=default_idx)
    year, month = months[labels.index(chosen)]

    start_at = datetime(year, month, 1)
    end_at = datetime(year + 1, 1, 1) if month == 12 else datetime(year, month + 1, 1)

    # If either legacy feed is empty for the current/future month, attempt one
    # self-healing refresh before rendering a blank calendar.
    s = get_session()
    try:
        econ_count = s.execute(
            select(func.count(MacroEvent.id)).where(
                MacroEvent.event_at >= start_at,
                MacroEvent.event_at < end_at,
            )
        ).scalar_one()
        earn_count = s.execute(
            select(func.count(EarningsEvent.id)).where(
                EarningsEvent.report_date >= start_at,
                EarningsEvent.report_date < end_at,
                EarningsEvent.is_upcoming == True,  # noqa: E712
            )
        ).scalar_one()

        if not econ_count and end_at >= datetime.utcnow():
            try:
                from mktscan.macro import refresh_economic_calendar
                refresh_economic_calendar(s)
            except Exception:
                pass

        if not earn_count and end_at >= datetime.utcnow():
            try:
                from mktscan.earnings_calendar import refresh_earnings_calendar
                refresh_earnings_calendar(s, basket_symbols)
            except Exception:
                pass
    finally:
        s.close()

    key_events_between.clear()
    all_events = key_events_between(start_at, end_at)

    econ_events = [e for e in all_events if e["kind"] == "ECON"]
    earnings_events = [
        e for e in all_events
        if e["kind"] == "EARN" and e["at"] >= datetime.utcnow() - timedelta(days=1)
    ]

    # Current calendar display follows selected legacy view.
    if view == "Economic Calendar":
        events = econ_events
    elif view == "Earnings Calendar":
        events = earnings_events
    else:
        events = econ_events + earnings_events
        events = sorted(events, key=lambda x: x["at"])

    econ_major_only = False
    if view in {"Combined Calendar", "Economic Calendar"}:
        econ_major_only = st.toggle(
            "Major economic events only",
            value=False,
            help=(
                "Off reproduces the fuller legacy economic calendar. "
                "Turn on to keep only High/Medium importance macro releases."
            ),
        )
        if econ_major_only:
            events = [
                e for e in events
                if e["kind"] == "EARN"
                or str(e.get("importance") or "").upper() in {"HIGH", "MEDIUM"}
            ]

    # Summary cards give immediate confirmation that both old feeds are present.
    m1, m2, m3, m4 = st.columns(4)
    with m1:
        card(
            "Economic Events",
            str(len(econ_events)),
            chosen,
            help_text=metric_help(
                "Economic Events",
                "MktScan MacroEvent table; MarketWatch primary",
                "Stored macroeconomic releases/events occurring in the selected month.",
                "Use High/Medium releases such as CPI, PCE, payrolls, GDP, ISM and FOMC as explicit event-risk windows.",
            ),
        )
    with m2:
        high_n = sum(str(e.get("importance") or "").upper() == "HIGH" for e in econ_events)
        card(
            "High Impact",
            str(high_n),
            "economic events",
            help_text=metric_help(
                "High Impact",
                "MktScan macro-event importance classifier",
                "Count of selected-month economic events classified High importance.",
                "High-impact events are most likely to produce broad index, rates and implied-volatility repricing.",
            ),
        )
    with m3:
        card(
            "Upcoming Earnings",
            str(len(earnings_events)),
            "basket events",
            help_text=metric_help(
                "Upcoming Earnings",
                "Yahoo Finance earnings calendar via MktScan EarningsEvent",
                "Upcoming company earnings dates for tickers in the configured MktScan basket.",
                "Treat earnings inside the intended option holding period as binary/event risk; IV can rise materially into the report.",
            ),
        )
    with m4:
        next_event = min(events, key=lambda e: e["at"], default=None)
        card(
            "Next Key Event",
            next_event["title"][:28] if next_event else "None",
            next_event["at"].strftime("%b %d · %H:%M UTC") if next_event else chosen,
            help_text=metric_help(
                "Next Key Event",
                "Combined MktScan economic + earnings calendars",
                "Chronologically nearest event in the selected calendar view.",
                "Use it to avoid entering a position without knowing the nearest scheduled binary or macro catalyst.",
            ),
        )

    # ── Calendar grid ────────────────────────────────────────────────────────
    st.markdown('<div class="tv-section">Calendar</div>', unsafe_allow_html=True)
    events_by_day: dict[int, list[dict]] = {}
    for e in events:
        events_by_day.setdefault(e["at"].day, []).append(e)

    weekday_labels = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    hdr = st.columns(7)
    for i, wd in enumerate(weekday_labels):
        hdr[i].markdown(
            f"<div class='tv-kicker' style='text-align:center'>{wd}</div>",
            unsafe_allow_html=True,
        )

    cal = calendar.Calendar(firstweekday=0)
    for week in cal.monthdayscalendar(year, month):
        cols = st.columns(7)
        for i, day_num in enumerate(week):
            with cols[i]:
                if day_num == 0:
                    st.markdown("<div style='min-height:128px'></div>", unsafe_allow_html=True)
                    continue

                is_today = year == today.year and month == today.month and day_num == today.day
                border = "border-color:#2962ff;" if is_today else ""
                day_events = events_by_day.get(day_num, [])
                pills = []

                for e in day_events[:5]:
                    if e["kind"] == "EARN":
                        cls = "tv-blue"
                        short = e["ticker"] or e["title"]
                        prefix = "EARN"
                    else:
                        imp = str(e.get("importance") or "").upper()
                        cls = "tv-bear" if imp == "HIGH" else "tv-warn"
                        short = e["title"]
                        prefix = "ECON"
                    pills.append(
                        f"<div class='tv-pill {cls}' "
                        f"style='display:block;margin:4px 0;overflow:hidden;"
                        f"text-overflow:ellipsis;white-space:nowrap' "
                        f"title='{e['title']}'>{prefix} · {short[:18]}</div>"
                    )

                if len(day_events) > 5:
                    pills.append(f"<div class='tv-small'>+{len(day_events)-5} more</div>")

                st.markdown(
                    f"<div class='tv-card' style='min-height:128px;{border}'>"
                    f"<div class='tv-kicker'>{day_num}</div>{''.join(pills)}</div>",
                    unsafe_allow_html=True,
                )

    # ── Legacy Economic Calendar detail view ─────────────────────────────────
    if view in {"Combined Calendar", "Economic Calendar"}:
        st.markdown('<div class="tv-section">Economic Calendar</div>', unsafe_allow_html=True)
        if not econ_events:
            st.warning(
                "No economic events are stored for this month. Use **Refresh Economic** above. "
                "If it remains empty, inspect scheduler logs for MarketWatch/Benzinga calendar diagnostics."
            )
        else:
            economic_rows = []
            for e in econ_events:
                economic_rows.append({
                    "Date": e["at"].strftime("%Y-%m-%d"),
                    "Time UTC": e["at"].strftime("%H:%M"),
                    "Event": e["title"],
                    "Category": e.get("category") or "Economic",
                    "Importance": e.get("importance") or "Normal",
                    "Consensus": e.get("consensus") or "—",
                    "Prior": e.get("prior") or "—",
                    "Actual": e.get("actual") or "—",
                    "Source": e.get("source") or "—",
                })
            econ_df = pd.DataFrame(economic_rows)
            if econ_major_only:
                econ_df = econ_df[
                    econ_df["Importance"].astype(str).str.upper().isin(["HIGH", "MEDIUM"])
                ]
            dataframe_with_help(
                econ_df,
                help_overrides={
                    "Date": "Source: MktScan MacroEvent. Definition: Scheduled calendar date. How to interpret: Track proximity to planned entries and expirations.",
                    "Time UTC": "Source: Calendar provider normalized by MktScan. Definition: Scheduled release time in UTC. How to interpret: Convert to local market time when planning entries.",
                    "Event": "Source: MarketWatch primary; fallback provider when used. Definition: Named macro release or policy event. How to interpret: CPI/PCE/jobs/FOMC/ISM/GDP can materially alter rates, index direction and IV.",
                    "Category": "Source: MktScan event classifier/provider. Definition: Macro-event grouping. How to interpret: Helps identify whether the event primarily relates to inflation, labor, growth, housing or policy.",
                    "Importance": "Source: MktScan/provider classification. Definition: Expected market relevance. How to interpret: High-impact releases warrant the greatest event-risk caution.",
                    "Consensus": "Source: Calendar provider. Definition: Pre-release market consensus where available. How to interpret: Price reactions are often driven by actual-versus-consensus and revisions.",
                    "Prior": "Source: Calendar provider. Definition: Previous reading. How to interpret: Compare with consensus/actual to identify acceleration or deceleration.",
                    "Actual": "Source: Calendar provider. Definition: Released value after publication. How to interpret: Interpret relative to consensus, prior and positioning rather than in isolation.",
                    "Source": "Source: MktScan provenance field. Definition: Upstream provider that populated the event. How to interpret: Useful for diagnosing freshness and provider fallback behavior.",
                },
                use_container_width=True,
                hide_index=True,
            )

    # ── Legacy Earnings Calendar detail view ─────────────────────────────────
    if view in {"Combined Calendar", "Earnings Calendar"}:
        st.markdown('<div class="tv-section">Upcoming Earnings</div>', unsafe_allow_html=True)

        s = get_session()
        try:
            stored_earnings = s.execute(
                select(EarningsEvent)
                .where(
                    EarningsEvent.report_date >= start_at,
                    EarningsEvent.report_date < end_at,
                    EarningsEvent.is_upcoming == True,  # noqa: E712
                    EarningsEvent.ticker.in_(basket_symbols),
                )
                .order_by(EarningsEvent.report_date, EarningsEvent.ticker)
            ).scalars().all()
        finally:
            s.close()

        if not stored_earnings:
            st.warning(
                "No upcoming basket earnings are stored for this month. "
                "Use **Refresh Earnings** above to query Yahoo Finance."
            )
        else:
            earning_rows = []
            for r in stored_earnings:
                earning_rows.append({
                    "Date": r.report_date.strftime("%Y-%m-%d") if r.report_date else "—",
                    "Ticker": r.ticker,
                    "EPS Estimate": r.eps_estimate,
                    "Status": "Upcoming" if r.is_upcoming else "Reported",
                    "Last Updated": (
                        r.updated_at.strftime("%Y-%m-%d %H:%M")
                        if r.updated_at else
                        r.scraped_at.strftime("%Y-%m-%d %H:%M")
                        if r.scraped_at else "—"
                    ),
                    "Source": "Yahoo Finance",
                })
            earnings_df = pd.DataFrame(earning_rows)
            dataframe_with_help(
                earnings_df,
                help_overrides={
                    "Date": "Source: Yahoo Finance earnings calendar. Definition: Scheduled earnings report date. How to interpret: Earnings inside your trade horizon introduce binary gap and IV-crush risk.",
                    "Ticker": "Source: MktScan configured basket. Definition: Company reporting earnings. How to interpret: Cross-reference with open trades and current setup quality.",
                    "EPS Estimate": "Source: Yahoo Finance analyst consensus where available. Definition: Consensus EPS estimate before the report. How to interpret: Actual-vs-estimate and guidance usually matter more than the estimate alone.",
                    "Status": "Source: MktScan EarningsEvent. Definition: Whether the report is still upcoming. How to interpret: Upcoming events should be explicitly incorporated into structure/expiration selection.",
                    "Last Updated": "Source: MktScan persistence metadata. Definition: Most recent stored refresh time. How to interpret: Stale dates should be refreshed because earnings dates can move.",
                    "Source": "Source: MktScan provenance. Definition: Upstream earnings-calendar provider. How to interpret: Current v2.5 earnings calendar uses Yahoo Finance.",
                },
                use_container_width=True,
                hide_index=True,
            )

# ─────────────────────────────────────────────────────────────────────────────
# RESEARCH — one ticker, conclusion first, diagnostics on demand
# ─────────────────────────────────────────────────────────────────────────────
elif area == "Research":
    is_ad_hoc = ticker not in basket_symbols
    review = None

    if is_ad_hoc:
        st.markdown(f"## {ticker} Research")
        st.caption("On-demand MktScan review · not part of the scheduled basket")
        try:
            nonce = st.session_state.get("adhoc_nonce", 0)
            with st.spinner(f"Running full MktScan review for {ticker}…"):
                review = on_demand_review(ticker, nonce)
        except Exception as exc:
            st.error(f"Could not analyze {ticker}: {exc}")
            st.info("Try another symbol or confirm that Yahoo Finance supports this ticker.")
            st.stop()

        pdict = review["price_data"]
        p = SimpleNamespace(**pdict, snapped_at=datetime.utcnow())
        td = review["tradeability"]
        sig = SimpleNamespace(
            score_at_prediction=float(td.get("score", 0.0)),
            label_at_prediction=td.get("label", "NEUTRAL"),
        )
        opt = SimpleNamespace(**review["options_market"]) if review.get("options_market") else None

        earn = None
        now = datetime.utcnow()
        upcoming = []
        for ev in review.get("earnings", []):
            rd = ev.get("report_date")
            if rd and rd >= now:
                upcoming.append(ev)
        if upcoming:
            ev = sorted(upcoming, key=lambda x: x.get("report_date"))[0]
            earn = SimpleNamespace(**ev)

        b1, b2 = st.columns([1, 5])
        with b1:
            if st.button("↻ Re-run review", use_container_width=True):
                st.session_state["adhoc_nonce"] = st.session_state.get("adhoc_nonce", 0) + 1
                st.rerun()
        with b2:
            st.caption(
                f"Coverage {td.get('coverage', 0):.0%} · "
                f"{review['sentiment'].get('article_count', 0)} unique news stories · "
                "cross-sectional basket ranks are intentionally omitted for ad-hoc symbols"
            )
    else:
        st.markdown(f"## {ticker} Research")
        p=prices.get(ticker); sig=signals.get(ticker); opt=options.get(ticker); earn=earnings.get(ticker)

    price_txt=f"${p.price:,.2f}" if p and p.price is not None else "—"
    chg=float(p.change_pct) if p and p.change_pct is not None else None
    st.caption(f"{price_txt}" + (f" · {chg:+.2f}%" if chg is not None else "") + f" · price {age_text(p.snapped_at) if p else 'unavailable'}")

    if "research_section" not in st.session_state:
        st.session_state["research_section"]="Summary"
    section=st.radio("Research view", ["Summary","Chart","Options","Analyst Activity","Trade Builder","ChatGPT Research","Advanced"], key="research_section", horizontal=True, label_visibility="collapsed")

    if section=="Summary":
        with st.spinner("Calculating technical opportunity…"):
            tech=review["technical"] if review else technical_opportunity(ticker)
        decision=build_decision(ticker,sig,regime,tech,opt,earn)
        q=decision["quality"]
        st.markdown('<div class="tv-section">Decision summary</div>', unsafe_allow_html=True)
        l,r=st.columns([2,1])
        with l:
            direction=semantic_signal(decision["score"])
            options_bias=decision["options"].structure_bias if decision["options"] else "Options context unavailable"
            strengths=" · ".join(q["strengths"][:3]) or "No major strengths confirmed"
            risks=" · ".join(decision["risks"][:3]) or "No major stored risk flags"
            st.markdown(f"""
<div class="tv-card">
<div class="tv-kicker">Decision summary</div>
<div class="tv-value {signal_color(direction)}">{decision['status']} · {direction}</div>
<p>{ticker} has a <b>{q['label'].lower()}</b> setup ({q['score']}/100). {strengths}.</p>
<p><b>Preferred expression:</b> {options_bias}</p>
<p class="tv-small"><b>Primary risks:</b> {risks}</p>
</div>
""", unsafe_allow_html=True)
        with r:
            card("Setup Quality", f"{q['label']} · {q['score']}/100", "workflow heuristic, not a validated forecast")
            card("Signal", semantic_signal(decision["score"]), f"raw {decision['score']:+.2f}" if decision["score"] is not None else "", signal_color(semantic_signal(decision["score"])))
            card("IV", iv_state(float(opt.iv_percentile_1y) if opt and opt.iv_percentile_1y is not None else None), f"{opt.iv_percentile_1y:.0f}th percentile" if opt and opt.iv_percentile_1y is not None else "history unavailable")

        st.markdown('<div class="tv-section">Setup scorecard</div>', unsafe_allow_html=True)
        c1,c2,c3,c4=st.columns(4)
        with c1: card("Trend",tech.trend_state,f"ADX {tech.adx14:.0f}" if tech.adx14 is not None else "ADX —",signal_color(tech.trend_state))
        with c2: card("Momentum",tech.momentum_state,f"RSI {tech.rsi14:.0f}" if tech.rsi14 is not None else "RSI —",signal_color(tech.momentum_state))
        with c3: card("Relative Strength",tech.relative_strength_state,f"vs QQQ {tech.rs_qqq_20d:+.1f}%" if tech.rs_qqq_20d is not None else "vs QQQ —")
        with c4: card("Volume",tech.volume_state,f"RVOL {tech.rvol20:.2f}×" if tech.rvol20 is not None else "RVOL —")
        st.caption("Strengths: " + (" · ".join(q["strengths"]) or "None confirmed"))
        if decision["risks"]: st.warning("Risks: " + " · ".join(decision["risks"]))

        analyst_events, analyst_mom = analyst_activity(ticker, 60)
        st.markdown('<div class="tv-section">Analyst Activity</div>', unsafe_allow_html=True)
        a1,a2,a3,a4,a5=st.columns(5)
        with a1: card("Analyst Momentum", analyst_mom["state"], f"score {analyst_mom['score']:+.1f}", signal_color("BULL" if analyst_mom["score"]>0 else "BEAR" if analyst_mom["score"]<0 else ""))
        with a2: card("Upgrades", str(analyst_mom["upgrades"]), "30D")
        with a3: card("Downgrades", str(analyst_mom["downgrades"]), "30D")
        with a4: card("PT Raises", str(analyst_mom["pt_raises"]), "30D")
        with a5: card("PT Cuts", str(analyst_mom["pt_cuts"]), "30D")
        if analyst_events:
            last = analyst_events[0]
            st.caption(
                f"Latest: {last.get('firm') or 'Analyst'} · "
                f"{last.get('action_company') or last.get('action_pt') or 'rating update'} · "
                f"{age_text(last.get('published_at'))}"
            )
        else:
            st.caption("No Benzinga analyst-rating events stored for this ticker yet.")

    elif section=="Chart":
        feed=os.getenv("ALPACA_DATA_FEED","iex").lower()
        c1,c2,c3=st.columns([1,1,5])
        with c1: rng=st.selectbox("Range",["1D","5D","1M","3M","6M","1Y"],index=0)
        with c2:
            if st.button("Refresh chart"):
                st.session_state["chart_nonce"]=st.session_state.get("chart_nonce",0)+1
        nonce=st.session_state.get("chart_nonce",0)
        try:
            q=live_quote(ticker,feed,nonce); bars=live_bars(ticker,rng,feed,nonce)
            if bars is None or bars.empty:
                st.info("No live bars returned for this range.")
            else:
                fig=go.Figure()
                fig.add_trace(go.Candlestick(x=bars["market_time"],open=bars["open"],high=bars["high"],low=bars["low"],close=bars["close"],name=ticker,increasing_line_color="#26a69a",decreasing_line_color="#ef5350"))
                fig.add_trace(go.Scatter(x=bars["market_time"],y=bars["ema_9"],name="EMA 9",line=dict(width=1.2,color="#4c8bf5")))
                fig.add_trace(go.Scatter(x=bars["market_time"],y=bars["ema_20"],name="EMA 20",line=dict(width=1.2,color="#f2b84b")))
                if bars["vwap"].notna().any(): fig.add_trace(go.Scatter(x=bars["market_time"],y=bars["vwap"],name="VWAP",line=dict(width=1,dash="dot",color="#b37feb")))
                if opt and opt.expected_move_dollars and p and p.price:
                    upper=float(p.price)+float(opt.expected_move_dollars); lower=float(p.price)-float(opt.expected_move_dollars)
                    fig.add_hline(y=upper,line_dash="dot",line_color="#787b86",annotation_text="Implied upper")
                    fig.add_hline(y=lower,line_dash="dot",line_color="#787b86",annotation_text="Implied lower")
                fig.update_layout(template="plotly_dark",paper_bgcolor="#0d0f14",plot_bgcolor="#0d0f14",height=590,margin=dict(l=10,r=10,t=15,b=10),xaxis_rangeslider_visible=False,legend=dict(orientation="h"))
                st.plotly_chart(fig,use_container_width=True)
                st.caption(f"Alpaca {feed.upper()} · live chart data cached briefly for performance")
        except Exception as e:
            st.error(f"Live chart unavailable: {e}")

    elif section=="Options":
        if not opt:
            if review and review.get("options_error"):
                st.info(f"Options Market unavailable for {ticker}: {review['options_error']}")
            else:
                st.info("No Options Market snapshot is stored for this ticker yet.")
        else:
            if review and opt.iv_percentile_1y is None:
                st.caption("Ad-hoc ticker: live option surface is available, but IV Rank/Percentile needs stored historical IV observations.")
            score=float(sig.score_at_prediction) if sig else None
            interp=interpret_options_market(opt,score,sig.label_at_prediction if sig else None)
            c1,c2,c3,c4,c5=st.columns(5)
            with c1: card("ATM IV",f"{opt.atm_iv*100:.1f}%" if opt.atm_iv is not None and opt.atm_iv<2 else f"{opt.atm_iv:.1f}%" if opt.atm_iv is not None else "—",interp.iv_state)
            with c2: card("IV Percentile",f"{opt.iv_percentile_1y:.0f}" if opt.iv_percentile_1y is not None else "—","1Y")
            with c3: card("Term",opt.term_state or "—",f"30D/60D/90D")
            with c4: card("Put Skew",f"{opt.put_skew*100:+.1f}" if opt.put_skew is not None else "—","vol pts vs ATM")
            with c5: card("Expected Move",f"±{opt.expected_move_pct:.1f}%" if opt.expected_move_pct is not None else "—",f"±${opt.expected_move_dollars:.2f}" if opt.expected_move_dollars is not None else "")
            st.markdown(f"**Interpretation:** {interp.thesis}")
            st.info(f"Preferred expression: **{interp.structure_bias}**")
            with st.expander("Full volatility surface diagnostics"):
                st.write(interp.term_view); st.write(interp.skew_view); st.write(interp.move_view)
                dataframe_with_help(pd.DataFrame([{"30D IV":opt.iv_30d,"60D IV":opt.iv_60d,"90D IV":opt.iv_90d,"30→60":opt.term_slope_30_60,"60→90":opt.term_slope_60_90,"Put skew":opt.put_skew,"Call skew":opt.call_skew,"Source":opt.source,"Confidence":opt.confidence}]),use_container_width=True,hide_index=True)
            for c in interp.cautions: st.warning(c)

    elif section=="Analyst Activity":
        st.markdown("### Analyst Activity")
        st.caption("Benzinga is the primary ratings source; Yahoo upgrades/downgrades are used as an explicitly labeled fallback if Benzinga is unavailable or not entitled.")

        r1, r2 = st.columns([1, 4])
        with r1:
            if st.button("Refresh analyst data", use_container_width=True):
                try:
                    from mktscan.analyst_ratings import refresh_analyst_ratings
                    s = get_session()
                    try:
                        result = refresh_analyst_ratings(s, [ticker], lookback_days=45)
                    finally:
                        s.close()
                    analyst_activity.clear()
                    if result.get("events"):
                        st.success(
                            f"{result['events']} events refreshed via {result.get('provider', 'unknown')} "
                            f"({result.get('inserted', 0)} new)."
                        )
                    else:
                        st.warning(
                            "No analyst events returned from Benzinga or Yahoo. "
                            "Check the ticker, MKTSCAN_BENZINGA_KEY, and Benzinga Analyst Ratings entitlement."
                        )
                    if result.get("errors"):
                        with st.expander("Provider diagnostics"):
                            for err in result["errors"]:
                                st.code(err)
                    st.rerun()
                except Exception as exc:
                    st.error(f"Analyst refresh failed: {exc}")
        with r2:
            st.caption("Scheduler refresh: every 15 minutes during the U.S. regular session; universe = basket + open journal positions.")

        events, momentum = analyst_activity(ticker, 90)
        source_counts = {}
        if events:
            s = get_session()
            try:
                rows = s.execute(
                    select(AnalystRatingEvent.source, func.count(AnalystRatingEvent.id))
                    .where(
                        AnalystRatingEvent.ticker == ticker,
                        AnalystRatingEvent.published_at >= datetime.utcnow() - timedelta(days=90),
                    )
                    .group_by(AnalystRatingEvent.source)
                ).all()
                source_counts = {str(src): int(n) for src, n in rows}
            finally:
                s.close()
            st.caption("Stored sources: " + " · ".join(f"{k}: {v}" for k, v in source_counts.items()))

        c1,c2,c3,c4,c5,c6=st.columns(6)
        with c1: card("Analyst Momentum", momentum["state"], f"score {momentum['score']:+.1f}", signal_color("BULL" if momentum["score"]>0 else "BEAR" if momentum["score"]<0 else ""))
        with c2: card("Analyst Events", str(momentum["events"]), "30D")
        with c3: card("Upgrades", str(momentum["upgrades"]), "30D")
        with c4: card("Downgrades", str(momentum["downgrades"]), "30D")
        with c5: card("PT Raises", str(momentum["pt_raises"]), "30D")
        with c6: card("PT Cuts", str(momentum["pt_cuts"]), "30D")

        st.markdown(
            "<div class='tv-small'>Scoring: upgrade +2 · downgrade −2 · bullish/bearish initiation ±1.5 · PT raise +1 · PT cut −1.</div>",
            unsafe_allow_html=True,
        )

        if not events:
            st.info("No analyst events are stored for this ticker yet. Use **Refresh analyst data** above. Benzinga requires the Analyst Ratings entitlement; Yahoo is attempted as a fallback.")
        else:
            rows=[]
            for e in events:
                rows.append({
                    "Date": e["published_at"].strftime("%Y-%m-%d %H:%M") if e.get("published_at") else "—",
                    "Firm": e.get("firm") or "—",
                    "Analyst": e.get("analyst_name") or "—",
                    "Action": e.get("action_company") or "—",
                    "Rating": (
                        f"{e.get('rating_prior')} → {e.get('rating_current')}"
                        if e.get("rating_prior") and e.get("rating_current") and e.get("rating_prior") != e.get("rating_current")
                        else e.get("rating_current") or "—"
                    ),
                    "PT Action": e.get("action_pt") or "—",
                    "PT Prior": f"${e['pt_prior']:.2f}" if e.get("pt_prior") is not None else "—",
                    "PT Current": f"${e['pt_current']:.2f}" if e.get("pt_current") is not None else "—",
                    "Importance": e.get("importance") if e.get("importance") is not None else "—",
                })
            dataframe_with_help(
                pd.DataFrame(rows),
                help_overrides={
                    "Date": "Source: Benzinga Analyst Ratings API\\n\\nDefinition: Timestamp of the analyst action.\\n\\nHow to interpret: Rating actions can act as short-term catalysts; the price/volume reaction matters.",
                    "Firm": "Source: Benzinga Analyst Ratings API\\n\\nDefinition: Sell-side research firm issuing the action.\\n\\nHow to interpret: Firm identity provides context; future validation can measure which firms/analysts add the most signal.",
                    "Analyst": "Source: Benzinga Analyst Ratings API\\n\\nDefinition: Named analyst associated with the action when supplied.\\n\\nHow to interpret: Analyst identity is context only in v1; accuracy scoring is not yet included.",
                    "Action": "Source: Benzinga Analyst Ratings API\\n\\nDefinition: Firm action such as Upgrade, Downgrade, Initiates or Maintains.\\n\\nHow to interpret: Upgrades/downgrades carry more weight in Analyst Momentum than reiterations.",
                    "Rating": "Source: Benzinga Analyst Ratings API\\n\\nDefinition: Prior and current analyst rating language.\\n\\nHow to interpret: Benzinga does not normalize rating language; focus on the direction of change.",
                    "PT Action": "Source: Benzinga Analyst Ratings API\\n\\nDefinition: Action on the price target, such as Raises or Lowers.\\n\\nHow to interpret: PT changes are useful secondary evidence but weaker than rating changes.",
                    "PT Prior": "Source: Benzinga Analyst Ratings API\\n\\nDefinition: Previous analyst price target.\\n\\nHow to interpret: Compare with the current target and spot price, but do not treat a target as a forecast guarantee.",
                    "PT Current": "Source: Benzinga Analyst Ratings API\\n\\nDefinition: New/current analyst price target.\\n\\nHow to interpret: A cluster of rising targets can confirm improving expectations; divergence from price momentum can be informative.",
                    "Importance": "Source: Benzinga Analyst Ratings API\\n\\nDefinition: Benzinga importance score, 0–5.\\n\\nHow to interpret: Higher values can help prioritize events in a busy feed.",
                },
                use_container_width=True,
                hide_index=True,
            )

    elif section=="Trade Builder":
        st.caption("Expensive live chain access only runs when this view is opened.")
        try:
            with st.spinner("Loading current tradeability and option chain…"):
                if review:
                    result = review["tradeability"]
                    setup = review.get("trade_setup")
                else:
                    result=live_tradeability().get(ticker)
                    setup=generate_basket_setups({ticker:result},max_workers=1).get(ticker) if result else None
            if not setup or not setup.get("tradeable"):
                st.warning((setup or {}).get("reason","No tradeable structure generated."))
            else:
                c1,c2,c3,c4=st.columns(4)
                with c1: card("Structure",setup.get("strategy","—"),setup.get("confidence_tier",""))
                with c2: card("Cost / Credit",f"${float(setup.get('net_debit') or setup.get('net_credit') or 0)*100:,.0f}","per 1-lot")
                with c3: card("Max Loss",f"${abs(float(setup.get('max_loss') or 0))*100:,.0f}","per 1-lot")
                with c4: card("Breakeven",f"${float(setup.get('breakeven') or 0):.2f}" if setup.get("breakeven") else "—",f"DTE {setup.get('dte','—')}")
                legs=pd.DataFrame(setup.get("legs",[]))
                if not legs.empty:
                    keep=[x for x in ["action","right","strike","expiry","bid","ask","fill","delta","theta","vega","open_interest","volume"] if x in legs.columns]
                    dataframe_with_help(legs[keep],use_container_width=True,hide_index=True)
                st.caption(f"Net Δ {setup.get('net_delta','—')} · Theta/day ${setup.get('net_theta_per_day_per_contract','—')} · Vega ${setup.get('net_vega_per_contract','—')}")
                for w in setup.get("warnings",[]): st.warning(w)
                st.button("Log this trade", type="primary", on_click=nav_to_journal, args=(ticker, setup.get("strategy", "")))
                st.caption(DISCLAIMER)
        except Exception as e:
            st.error(f"Trade builder failed: {e}")

    elif section=="ChatGPT Research":
        st.markdown("### ChatGPT research handoff")
        st.caption("MktScan remains the quantitative model. Use ChatGPT as a qualitative research and adversarial-review layer.")
        tech=review["technical"] if review else technical_opportunity(ticker)
        score=float(sig.score_at_prediction) if sig else None
        preset=st.selectbox("Research task",["Challenge the thesis","Explain the move","Identify catalysts","Evaluate risks","Compare with peers","Earnings review"])
        context={
            "ticker":ticker,"market_regime":regime.regime_label if regime else None,"tradeability":score,"signal":semantic_signal(score),
            "trend":tech.trend_state,"momentum":tech.momentum_state,"relative_strength":tech.relative_strength_state,"rvol":tech.rvol20,
            "iv_percentile":opt.iv_percentile_1y if opt else None,"term_structure":opt.term_state if opt else None,"expected_move_pct":opt.expected_move_pct if opt else None,
            "earnings":earn.report_date.isoformat() if earn and earn.report_date else None,
        }
        tasks={
            "Challenge the thesis":"Act as an adversarial equity analyst. Identify the strongest reasons this setup could fail, missing catalysts, and what evidence would invalidate the thesis.",
            "Explain the move":"Explain the likely company, sector, macro, and positioning drivers behind the recent price action. Distinguish confirmed facts from hypotheses.",
            "Identify catalysts":"Identify near-term catalysts and event risks that could matter over the next 1-8 weeks.",
            "Evaluate risks":"Assess business, valuation, macro, event, technical, and options-market risks relevant to this setup.",
            "Compare with peers":"Compare this ticker with its most relevant public peers on momentum, catalysts, valuation narrative, and competitive position.",
            "Earnings review":"Summarize the latest earnings/guidance themes and identify the questions that matter most for the next report.",
        }
        prompt=f"""Research {ticker}.\n\nMktScan quantitative context:\n{json.dumps(context,indent=2,default=str)}\n\nTask:\n{tasks[preset]}\n\nDo not treat the MktScan signal as ground truth. Challenge it. Cite current sources and call out stale or uncertain information."""
        st.code(prompt,language=None)
        st.info("Copy this prompt into ChatGPT. A direct API integration is intentionally not required for the dashboard to work.")

    elif section=="Advanced":
        tech=review["technical"] if review else technical_opportunity(ticker)
        st.markdown("### Advanced diagnostics")
        dataframe_with_help(pd.DataFrame([tech.__dict__]),use_container_width=True,hide_index=True)
        if p:
            st.markdown("#### Price/fundamental snapshot" if review else "#### Persisted price/fundamental snapshot")
            vals={k:v for k,v in p.__dict__.items() if not k.startswith("_")}
            st.json(vals,expanded=False)
        if review:
            st.markdown("#### On-demand signal categories")
            cats = review["tradeability"].get("categories", {})
            cat_rows = []
            for name, data in cats.items():
                cat_rows.append({
                    "Category": name,
                    "Score": data.get("score"),
                    "Confidence": data.get("confidence"),
                    "Detail": data.get("detail"),
                })
            dataframe_with_help(pd.DataFrame(cat_rows), use_container_width=True, hide_index=True)
            st.markdown("#### On-demand news sentiment")
            st.json(review.get("sentiment", {}), expanded=False)
        if opt:
            st.markdown("#### Raw options snapshot")
            vals={k:v for k,v in opt.__dict__.items() if not k.startswith("_")}
            st.json(vals,expanded=False)

# ─────────────────────────────────────────────────────────────────────────────
# PORTFOLIO — open risk first, journal second
# ─────────────────────────────────────────────────────────────────────────────
elif area == "Portfolio":
    st.markdown("## Portfolio")
    st.caption("Open positions, concentration risk, and trade journal in one place.")
    if "portfolio_view" not in st.session_state: st.session_state["portfolio_view"]="Open Positions"
    view=st.radio("Portfolio view",["Open Positions","Risk","Journal"],key="portfolio_view",horizontal=True,label_visibility="collapsed")
    s=get_session()
    try:
        trades=s.execute(select(TradeJournalEntry).order_by(desc(TradeJournalEntry.opened_at))).scalars().all()
        open_trades=[t for t in trades if t.status=="OPEN"]
        closed=[t for t in trades if t.status=="CLOSED"]
        if view=="Open Positions":
            marked=sum((trade_metrics(t,True).pnl or 0) for t in open_trades); risk=sum(float(t.planned_max_loss or 0) for t in open_trades)
            c1,c2,c3,c4=st.columns(4)
            ui_metric(c1,"Open P&L",f"${marked:,.0f}"); ui_metric(c2,"Capital at Risk",f"${risk:,.0f}"); ui_metric(c3,"Positions",len(open_trades)); ui_metric(c4,"Net Bias",("Bullish" if sum(1 if t.direction=="BULLISH" else -1 for t in open_trades)>0 else "Bearish" if open_trades else "Flat"))
            rows=[]
            for t in open_trades:
                m=trade_metrics(t,True); rows.append({"ID":t.id,"Ticker":t.ticker,"Strategy":t.strategy,"Direction":t.direction,"Opened":t.opened_at.date(),"Mark":t.current_value,"P&L":m.pnl,"ROR %":m.return_on_risk_pct,"Risk":t.planned_max_loss})
            dataframe_with_help(pd.DataFrame(rows),use_container_width=True,hide_index=True)
            if open_trades:
                tid=st.selectbox("Manage position",[t.id for t in open_trades],format_func=lambda i: next(f"#{t.id} · {t.ticker} · {t.strategy}" for t in open_trades if t.id==i))
                t=next(x for x in open_trades if x.id==tid)
                a,b=st.columns(2)
                with a:
                    with st.form("mark_trade"):
                        mark=st.number_input("Current option/position value",min_value=0.0,value=float(t.current_value or t.entry_value or 0),step=.01)
                        if st.form_submit_button("Update mark"):
                            mark_trade(s,t,mark); st.cache_data.clear(); st.rerun()
                with b:
                    with st.form("close_trade"):
                        exitv=st.number_input("Exit value",min_value=0.0,value=float(t.current_value or t.entry_value or 0),step=.01)
                        underlying=st.number_input("Underlying exit",min_value=0.0,value=float(prices.get(t.ticker).price if prices.get(t.ticker) and prices.get(t.ticker).price else 0),step=.01)
                        reason=st.selectbox("Exit reason",["Profit target","Stop loss","Signal reversal","Technical invalidation","Time exit","Pre-earnings exit","Manual discretion","Expiration","Other"])
                        if st.form_submit_button("Close trade"):
                            close_trade(s,t,closed_at=datetime.utcnow(),underlying_exit=underlying,exit_value=exitv,exit_reason=reason); st.cache_data.clear(); st.rerun()
        elif view=="Risk":
            risk=sum(float(t.planned_max_loss or 0) for t in open_trades)
            rows=[]
            for t in open_trades:
                r=float(t.planned_max_loss or 0); rows.append({"Ticker":t.ticker,"Strategy":t.strategy,"Direction":t.direction,"Risk $":r,"% Risk":(r/risk*100 if risk else 0)})
            rdf=pd.DataFrame(rows)
            if rdf.empty: st.info("Log open trades to populate portfolio risk.")
            else:
                dataframe_with_help(rdf.sort_values("Risk $",ascending=False),use_container_width=True,hide_index=True)
                by=rdf.groupby("Ticker",as_index=False)["Risk $"].sum().sort_values("Risk $",ascending=False)
                for _,row in by.iterrows():
                    pct=row["Risk $"]/risk*100 if risk else 0
                    if pct>=35: st.warning(f"{row['Ticker']} represents {pct:.0f}% of recorded portfolio risk.")
                st.caption("Portfolio Greeks/correlation are intentionally omitted until live option positions can be matched to reliable contract-level Greeks.")
        else:  # Journal
            sub=st.radio("Journal",["Log Trade","History","Trade Review"],horizontal=True,label_visibility="collapsed")
            if sub=="Log Trade":
                pre=st.session_state.get("journal_prefill_ticker",ticker); strat=st.session_state.get("journal_prefill_strategy","")
                with st.form("new_trade"):
                    c1,c2,c3=st.columns(3)
                    tk=c1.selectbox("Ticker",tickers,index=tickers.index(pre) if pre in tickers else 0)
                    direction=c2.selectbox("Direction",["BULLISH","BEARISH"])
                    strategy=c3.text_input("Strategy",value=strat or "Long Call")
                    c4,c5,c6=st.columns(3)
                    instrument=c4.selectbox("Instrument",["OPTION","STOCK"]); entry_type=c5.selectbox("Entry type",["DEBIT","CREDIT"]); qty=c6.number_input("Quantity",min_value=.01,value=1.0)
                    c7,c8,c9=st.columns(3)
                    underlying_entry=c7.number_input("Underlying entry",min_value=0.0,value=float(prices.get(tk).price if prices.get(tk) and prices.get(tk).price else 0),step=.01)
                    entry_value=c8.number_input("Entry premium/value",min_value=0.0,value=1.0,step=.01); max_loss=c9.number_input("Planned max loss $",min_value=0.0,value=100.0,step=25.0)
                    thesis=st.text_area("Trade thesis"); tags=st.text_input("Tags",placeholder="momentum, breakout, earnings")
                    stop=st.text_input("Stop / invalidation"); target=st.text_input("Profit target")
                    submitted=st.form_submit_button("Log trade",type="primary")
                if submitted:
                    create_trade(s,ticker=tk,instrument_type=instrument,direction=direction,strategy=strategy,status="OPEN",opened_at=datetime.utcnow(),underlying_entry=underlying_entry,quantity=qty,multiplier=100 if instrument=="OPTION" else 1,entry_type=entry_type,entry_value=entry_value,planned_max_loss=max_loss,thesis=thesis,tags=tags,stop_condition=stop,profit_target=target)
                    st.success("Trade logged with immutable MktScan entry context."); st.cache_data.clear(); st.session_state.pop("journal_prefill_strategy",None)
            elif sub=="History":
                rows=[]
                for t in trades:
                    m=trade_metrics(t,t.status=="OPEN"); rows.append({"ID":t.id,"Date":t.opened_at.date(),"Ticker":t.ticker,"Strategy":t.strategy,"Status":t.status,"P&L":m.pnl if t.status=="OPEN" else t.realized_pnl,"ROR %":m.return_on_risk_pct if t.status=="OPEN" else t.return_on_risk_pct,"Regime":t.regime_label,"IV Pct":t.iv_percentile,"Signal":t.tradeability_label,"Analyst":t.analyst_momentum_state})
                dataframe_with_help(pd.DataFrame(rows),use_container_width=True,hide_index=True)
            else:
                if not closed: st.info("Close trades to unlock attribution reviews.")
                else:
                    tid=st.selectbox("Closed trade",[t.id for t in closed],format_func=lambda i: next(f"#{t.id} · {t.ticker} · {t.strategy}" for t in closed if t.id==i))
                    t=next(x for x in closed if x.id==tid)
                    underlying_ret=((t.underlying_exit/t.underlying_entry-1)*100) if t.underlying_entry and t.underlying_exit else None
                    direction_ok=(underlying_ret is not None and ((t.direction=="BULLISH" and underlying_ret>0) or (t.direction=="BEARISH" and underlying_ret<0)))
                    pnl=float(t.realized_pnl or 0)
                    if underlying_ret is None: diagnosis="NEEDS UNDERLYING EXIT"
                    elif direction_ok and pnl>0: diagnosis="MODEL + STRUCTURE WORKED"
                    elif direction_ok and pnl<=0: diagnosis="STRUCTURE / EXECUTION ERROR"
                    elif not direction_ok and pnl<=0: diagnosis="DIRECTIONAL THESIS ERROR"
                    else: diagnosis="P&L POSITIVE DESPITE THESIS"
                    st.markdown(f"### {diagnosis}")
                    c1,c2,c3=st.columns(3); ui_metric(c1,"Underlying return",f"{underlying_ret:+.1f}%" if underlying_ret is not None else "—"); ui_metric(c2,"Trade P&L",f"${pnl:,.0f}"); ui_metric(c3,"Return on risk",f"{t.return_on_risk_pct:+.1f}%" if t.return_on_risk_pct is not None else "—")
                    st.caption(
                        f"Entry signal {t.tradeability_label or '—'} · regime {t.regime_label or '—'} · "
                        f"IV pct {t.iv_percentile if t.iv_percentile is not None else '—'} · "
                        f"analyst momentum {t.analyst_momentum_state or '—'} "
                        f"({t.analyst_momentum_score:+.1f})" if t.analyst_momentum_score is not None else
                        f"Entry signal {t.tradeability_label or '—'} · regime {t.regime_label or '—'} · "
                        f"IV pct {t.iv_percentile if t.iv_percentile is not None else '—'} · analyst momentum —"
                    )
    finally:
        s.close()

# ─────────────────────────────────────────────────────────────────────────────
# VALIDATION — does the system work?
# ─────────────────────────────────────────────────────────────────────────────
elif area == "Validation":
    st.markdown("## Validation")
    st.caption("Live trading outcomes, signal calibration, backtests, and attribution.")
    view=st.radio("Validation view",["Live Performance","Signal Calibration","Backtest","Attribution"],horizontal=True,label_visibility="collapsed")
    s=get_session()
    try:
        if view=="Live Performance":
            closed=s.execute(select(TradeJournalEntry).where(TradeJournalEntry.status=="CLOSED").order_by(TradeJournalEntry.closed_at)).scalars().all()
            pnls=[float(t.realized_pnl or 0) for t in closed]; wins=[x for x in pnls if x>0]; losses=[x for x in pnls if x<0]
            net=sum(pnls); winrate=(len(wins)/len(pnls)*100) if pnls else None; pf=(sum(wins)/abs(sum(losses))) if losses else None; expectancy=(net/len(pnls)) if pnls else None
            c1,c2,c3,c4=st.columns(4); ui_metric(c1,"Net P&L",f"${net:,.0f}"); ui_metric(c2,"Trades",len(pnls)); ui_metric(c3,"Win Rate",f"{winrate:.1f}%" if winrate is not None else "—"); ui_metric(c4,"Profit Factor",f"{pf:.2f}" if pf is not None else "—")
            if pnls:
                curve=pd.DataFrame({"Trade":range(1,len(pnls)+1),"Cumulative P&L":pd.Series(pnls).cumsum()})
                st.line_chart(curve.set_index("Trade"),height=300)
                st.caption(f"Expectancy per closed trade: ${expectancy:,.0f}")
        elif view=="Signal Calibration":
            rows=s.execute(select(TradeabilityOutcome).where(TradeabilityOutcome.actual_return_pct.isnot(None))).scalars().all()
            if not rows: st.info("No resolved forward outcomes yet.")
            else:
                n=len(rows); acc=sum(1 for r in rows if r.direction_correct)/n*100
                c1,c2=st.columns(2); ui_metric(c1,"Resolved signals",n); ui_metric(c2,"Directional accuracy",f"{acc:.1f}%")
                df=pd.DataFrame([{"Ticker":r.ticker,"Score":r.score_at_prediction,"Return %":r.actual_return_pct,"Correct":r.direction_correct,"Regime":r.regime_label_at_prediction} for r in rows])
                df["Score bucket"]=pd.cut(df["Score"],[-1,-.5,-.2,.2,.5,1],labels=["Strong Bear","Bear","Neutral","Bull","Strong Bull"])
                cal=df.groupby("Score bucket",observed=True).agg(N=("Ticker","size"),Avg_Return=("Return %","mean"),Accuracy=("Correct","mean")).reset_index(); cal["Accuracy"]*=100
                dataframe_with_help(cal,use_container_width=True,hide_index=True)
        elif view=="Backtest":
            try:
                from mktscan.backtest_incremental import BacktestObservation, BacktestSummary
                sums=s.execute(select(BacktestSummary).order_by(BacktestSummary.label,BacktestSummary.holding_days)).scalars().all()
                if sums:
                    dataframe_with_help(pd.DataFrame([{"Label":r.label,"Days":r.holding_days,"N":r.n_observations,"Avg Return %":r.avg_return_pct,"Excess %":r.excess_return_pct,"Win Rate %":r.win_rate_pct,"Option P&L %":r.option_avg_pnl_pct,"Option Win %":r.option_win_rate} for r in sums]),use_container_width=True,hide_index=True)
                else: st.info("Backtest summary is empty. Run the incremental backtest first.")
            except Exception as e: st.error(f"Backtest data unavailable: {e}")
        else:
            closed=s.execute(select(TradeJournalEntry).where(TradeJournalEntry.status=="CLOSED")).scalars().all()
            if not closed: st.info("No closed journal trades yet.")
            else:
                df=pd.DataFrame([{"Ticker":t.ticker,"Strategy":t.strategy,"P&L":t.realized_pnl or 0,"ROR":t.return_on_risk_pct,"Regime":t.regime_label or "Unknown","IV Pct":t.iv_percentile,"Signal":t.tradeability_label or "Unknown","Analyst Momentum":t.analyst_momentum_state or "Unknown"} for t in closed])
                left,right=st.columns(2)
                with left:
                    st.markdown("#### By strategy"); dataframe_with_help(df.groupby("Strategy").agg(N=("Ticker","size"),PnL=("P&L","sum"),Avg_ROR=("ROR","mean")).reset_index().sort_values("PnL",ascending=False),use_container_width=True,hide_index=True)
                with right:
                    st.markdown("#### By market regime"); dataframe_with_help(df.groupby("Regime").agg(N=("Ticker","size"),PnL=("P&L","sum"),Avg_ROR=("ROR","mean")).reset_index().sort_values("PnL",ascending=False),use_container_width=True,hide_index=True)
                if df["IV Pct"].notna().any():
                    df["IV Bucket"]=pd.cut(df["IV Pct"],[-1,20,40,60,80,101],labels=["<20","20-40","40-60","60-80","80+"])
                    st.markdown("#### By IV percentile")
                    dataframe_with_help(df.groupby("IV Bucket",observed=True).agg(N=("Ticker","size"),PnL=("P&L","sum"),Avg_ROR=("ROR","mean")).reset_index(),use_container_width=True,hide_index=True)
                if (df["Analyst Momentum"] != "Unknown").any():
                    st.markdown("#### By analyst momentum at entry")
                    dataframe_with_help(
                        df.groupby("Analyst Momentum").agg(
                            N=("Ticker","size"),
                            PnL=("P&L","sum"),
                            Avg_ROR=("ROR","mean"),
                        ).reset_index().sort_values("PnL",ascending=False),
                        use_container_width=True,
                        hide_index=True,
                    )
    finally:
        s.close()
