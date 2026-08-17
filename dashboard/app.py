"""
dashboard/app.py
MktScan Streamlit dashboard.
Run: streamlit run dashboard/app.py
"""
from __future__ import annotations
import calendar
import json
import sys
import os
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

# Make mktscan importable from dashboard dir
sys.path.insert(0, str(Path(__file__).parent.parent))

from mktscan.config import load_config
from mktscan.database import (
    init_db, get_session, get_basket, get_latest_scores,
    get_score_history, get_recent_articles,
    SentimentScore, PriceSnapshot, EarningsEvent, ScraperRun,
    Article, Company, MarketRegimeSnapshot, seed_default_basket, upsert_company
)
from sqlalchemy import select, desc, func
from mktscan.tradeability import (
    compute_basket_tradeability, DEFAULT_WEIGHTS,
    tradeability_label, tradeability_color,
)
from mktscan.options import generate_basket_setups, DISCLAIMER
from mktscan.feedback import get_basket_accuracy_stats


# ── Page config ────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="MktScan",
    page_icon="📡",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Custom CSS ─────────────────────────────────────────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=IBM+Plex+Sans:wght@300;400;600&display=swap');

html, body, [class*="css"] {
    font-family: 'IBM Plex Sans', sans-serif;
}
.metric-card {
    background: rgba(255,255,255,0.03);
    border: 1px solid rgba(255,255,255,0.08);
    border-radius: 8px;
    padding: 16px 20px;
    margin-bottom: 8px;
}
.ticker-label {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 18px;
    font-weight: 600;
    color: #22d3a0;
}
.score-badge {
    font-family: 'IBM Plex Mono', monospace;
    font-weight: 600;
}
.stDataFrame { font-family: 'IBM Plex Mono', monospace; font-size: 12px; }
div[data-testid="stMetricValue"] { font-family: 'IBM Plex Mono', monospace; }
</style>
""", unsafe_allow_html=True)


# ── Init DB ────────────────────────────────────────────────────────────────────
@st.cache_resource
def setup():
    try:
        load_config()
    except FileNotFoundError:
        pass  # OK — dashboard can still show existing data
    init_db()
    session = get_session()
    seed_default_basket(session)
    session.close()

setup()


# ── Cached expensive paths ─────────────────────────────────────────────────────
# These two calls dominate page load: compute_basket_tradeability issues one
# yfinance download per ticker, and generate_basket_setups fetches an option
# chain per ticker. Neither was cached, and the weight sliders live on the same
# page — so *every slider drag* triggered ~40 network round trips and a full
# rescore. Streamlit reruns the whole script on any widget change, so caching
# here is what makes the page usable.

@st.cache_data(ttl=600, show_spinner=False)
def cached_tradeability(weights: dict | None = None) -> dict:
    """Basket tradeability. Cache key includes the weights, so moving a slider
    recomputes the score but reuses the underlying price/IV data pulled below."""
    session = get_session()
    try:
        return compute_basket_tradeability(session, weights=weights)
    finally:
        session.close()


@st.cache_data(ttl=600, show_spinner=False)
def cached_setups(_results_key: str, results: dict) -> dict:
    """
    Priced option setups. Keyed on a digest of the tradeability scores rather
    than the full result dict, since only the score and strategy inputs affect
    the setup.
    """
    from mktscan.options import generate_basket_setups
    return generate_basket_setups(results)


def results_digest(results: dict) -> str:
    """Stable cache key from the scores that actually drive setup selection."""
    import hashlib
    payload = "|".join(
        f"{t}:{r.get('score'):.4f}:{r.get('iv_rank')}:{r.get('days_to_earnings')}"
        for t, r in sorted(results.items())
    )
    return hashlib.sha1(payload.encode()).hexdigest()


@st.cache_data(ttl=300, show_spinner=False)
def cached_market_regime():
    """Latest persisted regime snapshot. No network calls on dashboard reruns."""
    session = get_session()
    try:
        return session.execute(
            select(MarketRegimeSnapshot)
            .order_by(desc(MarketRegimeSnapshot.snapped_at))
            .limit(1)
        ).scalar_one_or_none()
    finally:
        session.close()


@st.cache_data(ttl=300, show_spinner=False)
def cached_sidebar_stats() -> dict:
    """Sidebar counters. These ran a COUNT(*) over articles on every rerun of
    every page, including page switches that do not use them."""
    session = get_session()
    try:
        latest_run = session.execute(
            select(ScraperRun).order_by(desc(ScraperRun.started_at)).limit(1)
        ).scalar_one_or_none()
        return {
            "companies": len(get_basket(session)),
            "articles": session.execute(select(func.count(Article.id))).scalar() or 0,
            "last_run_at": latest_run.started_at if latest_run else None,
            "last_run_status": latest_run.status if latest_run else None,
        }
    finally:
        session.close()


# ── Sidebar ────────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("## 📡 MktScan")
    st.caption("Market Intelligence Terminal")
    st.divider()

    page = st.radio(
        "Navigation",
        ["Dashboard", "Tradeability", "News Feed", "Earnings", "Economic Calendar", "Basket", "Backtest", "Data Definitions", "Run Scraper"],
        label_visibility="collapsed",
    )

    st.divider()

    # Quick stats (cached — see cached_sidebar_stats)
    stats = cached_sidebar_stats()

    st.metric("Companies", stats["companies"])
    st.metric("Total Articles", f"{stats['articles']:,}")
    if stats["last_run_at"]:
        elapsed = (datetime.utcnow() - stats["last_run_at"]).total_seconds() / 3600
        st.metric("Last Run", f"{elapsed:.1f}h ago")
        st.caption(f"Status: {stats['last_run_status']}")
    else:
        st.metric("Last Run", "Never")

    if st.button("↻ Refresh data", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

    st.divider()
    st.caption("v2.0.0 · MktScan")


# ── Helper functions ───────────────────────────────────────────────────────────
class _AdhocPriceError(Exception):
    """
    Raised when an ad-hoc ticker has no retrievable price.

    A plain ``st.stop()`` cannot be used inside the ad-hoc analysis block: it
    raises Streamlit's StopException, which the surrounding broad
    ``except Exception`` catches, replacing the specific "check the ticker
    symbol" message with a generic failure. A dedicated exception caught ahead
    of the broad handler preserves the message.
    """
    def __init__(self, ticker: str):
        self.ticker = ticker
        super().__init__(f"No price for {ticker}")


def format_component_value(value) -> str:
    """
    Render a signal component for display, whatever its type.

    Component dicts are deliberately heterogeneous — they carry the signed
    sub-scores that drive the composite, but also context the user needs to
    interpret them: `iv_basis` is a string ("chain" / "proxy" / "none"),
    `mean_reversion_flag` is a bool, `earnings_days_away` is a count, and
    `iv_percentile` is None until IV history exists.

    Formatting them all with `:+.3f` crashes on the first string
    (`ValueError: Unknown format code 'f' for object of type 'str'`) and on the
    first None. Signed decimals are reserved for the actual float sub-scores,
    where the sign carries meaning.
    """
    if value is None:
        return "—"
    if isinstance(value, bool):            # check before int — bool subclasses int
        return "yes" if value else "no"
    if isinstance(value, int):
        # Plain integers — these are counts (days to earnings, history days,
        # streak length), not signed scores, so a leading "+" would misread as
        # bullishness. Negatives still print their minus sign naturally.
        return str(value)
    if isinstance(value, float):
        # Only the float sub-scores get an explicit sign, where it means direction.
        return f"{value:+.3f}"
    return str(value)


def sentiment_color(score: float) -> str:
    if score > 0.3:  return "#22d3a0"
    if score < -0.1: return "#f87171"
    return "#fbbf24"

def sentiment_emoji(label: str) -> str:
    return {"BULLISH": "🟢", "BEARISH": "🔴", "NEUTRAL": "🟡"}.get(label, "⚪")

def score_bar_html(score: float, width: int = 120) -> str:
    pct  = abs(score) * 100
    col  = sentiment_color(score)
    sign = "+" if score >= 0 else ""
    return (
        f'<span style="font-family:\'IBM Plex Mono\',monospace;color:{col};font-weight:600">'
        f'{sign}{score:.3f}</span>'
        f'<div style="background:rgba(255,255,255,0.05);border-radius:2px;height:4px;'
        f'width:{width}px;margin-top:3px;overflow:hidden">'
        f'<div style="width:{pct:.0f}%;height:100%;background:{col};border-radius:2px"></div>'
        f'</div>'
    )


# ═══════════════════════════════════════════════════════════════════════════════
# DASHBOARD PAGE
# ═══════════════════════════════════════════════════════════════════════════════
if page == "Dashboard":
    st.title("Market Dashboard")

    session = get_session()
    latest_scores = get_latest_scores(session)
    companies     = get_basket(session)

    # ── Last run banner + ad-hoc run button ───────────────────────────────────
    session_kpi = get_session()
    earnings_count = session_kpi.execute(
        select(func.count(EarningsEvent.id)).where(
            EarningsEvent.report_date >= datetime.utcnow(),
            EarningsEvent.report_date <= datetime.utcnow() + timedelta(days=7),
        )
    ).scalar() or 0
    total_articles = session_kpi.execute(select(func.count(Article.id))).scalar() or 0
    recent_run = session_kpi.execute(
        select(ScraperRun).order_by(desc(ScraperRun.started_at)).limit(1)
    ).scalar_one_or_none()

    # Last 5 runs for the history tooltip
    recent_runs = session_kpi.execute(
        select(ScraperRun).order_by(desc(ScraperRun.started_at)).limit(5)
    ).scalars().all()
    session_kpi.close()

    # Build banner content
    if recent_run:
        run_dt      = recent_run.started_at
        elapsed_sec = (datetime.utcnow() - run_dt).total_seconds()
        elapsed_h   = elapsed_sec / 3600
        if elapsed_sec < 60:
            elapsed_str = "just now"
        elif elapsed_sec < 3600:
            elapsed_str = f"{int(elapsed_sec // 60)} min ago"
        elif elapsed_h < 24:
            elapsed_str = f"{elapsed_h:.1f}h ago"
        else:
            elapsed_str = f"{elapsed_h / 24:.1f} days ago"

        run_dt_str  = run_dt.strftime("%A %b %d, %Y at %H:%M UTC")
        status_icon = {"ok": "✅", "error": "❌", "running": "🔄", "partial": "⚠️"}.get(
            recent_run.status, "❓"
        )
        status_color = {
            "ok": "#22d3a0", "error": "#f87171",
            "running": "#60a5fa", "partial": "#fbbf24"
        }.get(recent_run.status, "#94a3b8")

        banner_right = (
            f'<div style="text-align:right">'
            f'<div style="font-family:IBM Plex Mono,monospace;font-size:11px;color:#64748b">Last run</div>'
            f'<div style="font-family:IBM Plex Mono,monospace;font-size:16px;font-weight:600;'
            f'color:{status_color}">{status_icon} {elapsed_str}</div>'
            f'<div style="font-size:11px;color:#475569;margin-top:2px">{run_dt_str}</div>'
            f'<div style="font-size:10px;color:#334155;margin-top:2px">'
            f'Run #{recent_run.id} · {recent_run.articles_new or 0} new articles · '
            f'{recent_run.tickers_scored or 0} scored</div>'
            f'</div>'
        )
    else:
        status_color = "#f87171"
        banner_right = (
            '<div style="text-align:right">'
            '<div style="font-family:IBM Plex Mono,monospace;font-size:16px;'
            'font-weight:600;color:#f87171">Never run</div>'
            '<div style="font-size:11px;color:#475569">Click Run Now to fetch data</div>'
            '</div>'
        )

    # Banner + run button side by side
    banner_col, run_col = st.columns([3, 1])

    with banner_col:
        st.markdown(
            f'<div style="background:rgba(255,255,255,0.02);border:1px solid {status_color}33;'
            f'border-radius:8px;padding:12px 18px;display:flex;'
            f'justify-content:space-between;align-items:center">'
            f'<div>'
            f'<div style="font-family:IBM Plex Mono,monospace;font-size:10px;'
            f'color:#64748b;text-transform:uppercase;letter-spacing:0.08em;margin-bottom:4px">'
            f'Scraper status</div>'
            f'<div style="display:flex;gap:16px;flex-wrap:wrap">'
            + "".join([
                f'<div style="font-size:11px;color:#475569">'
                f'<span style="font-family:IBM Plex Mono,monospace;font-size:11px;'
                f'color:#64748b">Run #{r.id}</span>  '
                f'{"✅" if r.status=="ok" else "⚠️" if r.status=="partial" else "❌"}  '
                f'{r.started_at.strftime("%b %d %H:%M")}'
                f'</div>'
                for r in recent_runs
            ])
            + f'</div></div>'
            + banner_right
            + '</div>',
            unsafe_allow_html=True,
        )

    with run_col:
        st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)
        run_mode = st.selectbox(
            "Mode",
            ["all", "news", "earnings", "prices"],
            label_visibility="collapsed",
            key="dash_run_mode",
        )
        run_clicked = st.button(
            "▶ Run Now",
            type="primary",
            use_container_width=True,
            key="dash_run_btn",
        )

    # ── Ad-hoc run execution ──────────────────────────────────────────────────
    if run_clicked:
        from mktscan.engine import ScrapeEngine
        run_log   = st.empty()
        log_lines: list[str] = []

        def _dash_log(level: str, msg: str):
            icons = {"ok": "✅", "warn": "⚠️", "err": "❌", "info": "ℹ️"}
            ts = datetime.utcnow().strftime("%H:%M:%S")
            log_lines.append(f"`{ts}` {icons.get(level,'·')}  {msg}")
            run_log.markdown("\n\n".join(log_lines[-20:]))

        try:
            cfg = load_config()
            engine = ScrapeEngine(cfg=cfg, progress_cb=_dash_log)
            with st.spinner(f"Running scraper ({run_mode} mode)…"):
                result = engine.run(mode=run_mode)
            st.success(
                f"✅ Done — {result['articles_new']} new articles, "
                f"{result['tickers_scored']} scored in {result['elapsed_seconds']:.0f}s"
            )
            if result.get("errors"):
                with st.expander("Errors", expanded=False):
                    for e in result["errors"]:
                        st.error(e)
            st.rerun()   # refresh dashboard with fresh data
        except Exception as e:
            st.error(f"Run failed: {e}")

    # ── Market regime context ───────────────────────────────────────────────
    regime_row = cached_market_regime()
    st.markdown("### Market Regime")
    st.caption(
        "Context only — this snapshot is recorded for validation and does not "
        "modify tradeability scores, strategy selection, or sizing."
    )
    if regime_row is None:
        st.info("No regime snapshot yet. Run the scraper in `all`/`prices` mode or `mktscan regime --refresh`.")
    else:
        label = (regime_row.regime_label or "UNKNOWN").replace("_", " ")
        score = regime_row.regime_score
        conf = regime_row.confidence
        rg1, rg2, rg3, rg4, rg5, rg6 = st.columns(6)
        rg1.metric("Regime", label)
        rg2.metric("Score", f"{score:+.2f}" if score is not None else "—")
        rg3.metric("Confidence", f"{conf:.0%}" if conf is not None else "—")
        rg4.metric("VIX", f"{regime_row.vix:.1f}" if regime_row.vix is not None else "—",
                   regime_row.volatility_state.replace("_", " ") if regime_row.volatility_state else None)
        rg5.metric("Breadth > 50d", f"{regime_row.breadth_above_50d:.0f}%" if regime_row.breadth_above_50d is not None else "—",
                   f"{regime_row.breadth_universe_size or 0} basket names")
        macro_delta = None
        if regime_row.hours_to_macro is not None:
            macro_delta = f"in {regime_row.hours_to_macro:.0f}h"
        rg6.metric("Next high-impact macro", regime_row.next_macro_event or "None", macro_delta)

        with st.expander("Regime components", expanded=False):
            rows = [
                {"Component": "SPY trend", "Score": regime_row.spy_trend_score, "Detail": f"20d {regime_row.spy_return_20d:+.1f}%" if regime_row.spy_return_20d is not None else "—"},
                {"Component": "QQQ trend", "Score": regime_row.qqq_trend_score, "Detail": f"20d {regime_row.qqq_return_20d:+.1f}%" if regime_row.qqq_return_20d is not None else "—"},
                {"Component": "Volatility", "Score": regime_row.volatility_score, "Detail": regime_row.volatility_state or "—"},
                {"Component": "Basket breadth", "Score": regime_row.breadth_score, "Detail": f">20d {regime_row.breadth_above_20d:.0f}% · >200d {regime_row.breadth_above_200d:.0f}%" if regime_row.breadth_above_20d is not None and regime_row.breadth_above_200d is not None else "—"},
                {"Component": "Rates", "Score": regime_row.rates_score, "Detail": f"2Y {regime_row.two_year_yield:.2f}% · 10Y {regime_row.ten_year_yield:.2f}%" if regime_row.two_year_yield is not None and regime_row.ten_year_yield is not None else "—"},
                {"Component": "Macro risk", "Score": regime_row.macro_risk_score, "Detail": regime_row.next_macro_event or "—"},
            ]
            df_regime = pd.DataFrame(rows)
            df_regime["Score"] = df_regime["Score"].map(lambda x: f"{x:+.2f}" if pd.notna(x) else "—")
            st.dataframe(df_regime, use_container_width=True, hide_index=True)
            st.caption(
                "Regime score weights: trend 45%, basket breadth 25%, volatility 20%, rates 10%. "
                "Macro is a non-directional caution flag and is not included in the score."
            )

    st.divider()

    # ── Ad-hoc ticker analysis ───────────────────────────────────────────────
    with st.expander("🔍  Analyse a specific ticker", expanded=False):
        st.caption(
            "Enter any stock ticker for an instant snapshot — price, momentum, "
            "sentiment, and a trade setup — without adding it to your basket."
        )

        ac1, ac2, ac3 = st.columns([1, 1, 2])
        adhoc_ticker = ac1.text_input(
            "Ticker", placeholder="e.g. TSLA", max_chars=10,
            key="adhoc_ticker_input",
        ).upper().strip()
        adhoc_mode = ac2.selectbox(
            "Analysis depth",
            ["Quick (price + momentum)", "Full (all signals + trade setup)"],
            key="adhoc_mode",
        )
        adhoc_go = ac3.button(
            "▶ Analyse", type="primary", key="adhoc_go_btn",
            use_container_width=False,
        )

        if adhoc_go and adhoc_ticker:
            import yfinance as yf
            from mktscan.tradeability import (
                fetch_daily_returns, calc_price_momentum_signal,
                calc_technical_signal, calc_sentiment_signal,
                compute_tradeability, tradeability_label, tradeability_color,
            )
            from mktscan.options import generate_trade_setup

            with st.spinner(f"Fetching data for {adhoc_ticker}…"):
                try:
                    # ── Fetch price data ──────────────────────────────────────
                    t    = yf.Ticker(adhoc_ticker)
                    info = t.info or {}

                    price     = info.get("regularMarketPrice") or info.get("currentPrice") or 0
                    chg_pct   = info.get("regularMarketChangePercent") or 0
                    hi_52     = info.get("fiftyTwoWeekHigh")
                    lo_52     = info.get("fiftyTwoWeekLow")
                    pe        = info.get("trailingPE")
                    mkt_cap   = info.get("marketCap")
                    rating    = info.get("recommendationKey", "")
                    name      = info.get("shortName") or info.get("longName") or adhoc_ticker
                    sector    = info.get("sector", "—")
                    volume    = info.get("regularMarketVolume")
                    avg_vol   = info.get("averageVolume")

                    if not price:
                        # NOTE: st.stop() raises StopException, which the broad
                        # `except Exception` wrapping this block used to swallow —
                        # so the intended "check the ticker symbol" message was
                        # replaced by a generic analysis-failed error. Raising a
                        # sentinel and re-raising it past the handler keeps the
                        # useful message.
                        raise _AdhocPriceError(adhoc_ticker)

                    # 120 bars so Wilder's RSI converges; it needs ~100+.
                    daily_returns = fetch_daily_returns(adhoc_ticker, bars=120)
                    mom_result    = calc_price_momentum_signal(daily_returns)

                    price_data_adhoc = {
                        "price":         price,
                        "change_pct":    chg_pct,
                        "week_52_high":  hi_52,
                        "week_52_low":   lo_52,
                        "pe_ratio":      pe,
                        "analyst_rating":rating,
                        "market_cap":    mkt_cap,
                    }
                    tech_result = calc_technical_signal(price_data_adhoc)

                    # ── Header ────────────────────────────────────────────────
                    chg_col = "#22d3a0" if chg_pct >= 0 else "#f87171"
                    st.markdown(
                        f'<div style="background:rgba(255,255,255,0.03);border:1px solid '
                        f'rgba(255,255,255,0.08);border-radius:8px;padding:14px 18px;margin-bottom:12px">'
                        f'<div style="display:flex;justify-content:space-between;align-items:flex-start">'
                        f'<div>'
                        f'<div style="font-family:IBM Plex Mono,monospace;font-size:20px;'
                        f'font-weight:700;color:#22d3a0">{adhoc_ticker}</div>'
                        f'<div style="font-size:13px;color:#94a3b8;margin-top:2px">{name}</div>'
                        f'<div style="font-size:11px;color:#475569;margin-top:2px">'
                        f'{sector} · {"${:,.2f}B mkt cap".format(mkt_cap/1e9) if mkt_cap else "—"}'
                        f'</div>'
                        f'</div>'
                        f'<div style="text-align:right">'
                        f'<div style="font-family:IBM Plex Mono,monospace;font-size:28px;'
                        f'font-weight:700;color:#e2e8f0">${price:,.2f}</div>'
                        f'<div style="font-family:IBM Plex Mono,monospace;font-size:14px;'
                        f'color:{chg_col}">{chg_pct:+.2f}% today</div>'
                        f'<div style="font-size:10px;color:#475569;margin-top:2px">'
                        f'Vol: {"{:,.0f}".format(volume) if volume else "—"} · '
                        f'Avg: {"{:,.0f}".format(avg_vol) if avg_vol else "—"}'
                        f'</div>'
                        f'</div>'
                        f'</div>'
                        f'</div>',
                        unsafe_allow_html=True,
                    )

                    # ── Key metrics strip ─────────────────────────────────────
                    m1, m2, m3, m4, m5, m6 = st.columns(6)
                    m1.metric("Price",       f"${price:,.2f}")
                    m2.metric("Today",       f"{chg_pct:+.2f}%",
                              delta=f"{chg_pct:+.2f}%", delta_color="normal")
                    m3.metric("52w High",    f"${hi_52:,.2f}" if hi_52 else "—")
                    m4.metric("52w Low",     f"${lo_52:,.2f}" if lo_52 else "—")
                    m5.metric("P/E",         f"{pe:.1f}" if pe else "—")
                    m6.metric("Analyst",     rating.title() if rating else "—")

                    st.divider()

                    # ── Momentum metrics ──────────────────────────────────────
                    rsi      = mom_result.get("rsi")
                    ann_vol  = mom_result.get("annual_vol")
                    streak   = mom_result.get("streak", 0)
                    ret14    = mom_result.get("total_return_14d", 0)
                    mom_score = mom_result.get("score", 0.0)

                    rsi_col    = "#f87171" if (rsi or 50) > 70 else "#22d3a0" if (rsi or 50) < 30 else "#fbbf24"
                    streak_col = "#22d3a0" if streak > 0 else "#f87171" if streak < 0 else "#64748b"
                    ret_col    = "#22d3a0" if ret14 >= 0 else "#f87171"
                    mom_col    = tradeability_color(mom_score)

                    p1, p2, p3, p4, p5 = st.columns(5)
                    p1.metric("RSI (14d)",      f"{rsi:.0f}" if rsi else "—",
                              help="<30 oversold, >70 overbought")
                    p2.metric("Ann. Volatility", f"{ann_vol:.0f}%" if ann_vol else "—")
                    p3.metric("Day Streak",      f"{streak:+d} days" if streak else "0 days",
                              delta=f"{'up' if streak > 0 else 'down' if streak < 0 else 'flat'}",
                              delta_color="normal" if streak > 0 else "inverse" if streak < 0 else "off")
                    p4.metric("14d Return",      f"{ret14:+.1f}%",
                              delta=f"{ret14:+.1f}%", delta_color="normal")
                    p5.metric("Momentum Score",  f"{mom_score:+.3f}",
                              help="Price Momentum sub-score from Tradeability engine")

                    # ── 52w range bar ─────────────────────────────────────────
                    if hi_52 and lo_52 and hi_52 > lo_52:
                        pct_in_range = (price - lo_52) / (hi_52 - lo_52) * 100
                        st.markdown("**52-week range position**")
                        st.markdown(
                            f'<div style="position:relative;height:20px;background:rgba(255,255,255,0.05);'
                            f'border-radius:10px;overflow:hidden;margin-bottom:4px">'
                            f'<div style="position:absolute;left:0;top:0;height:100%;'
                            f'width:{pct_in_range:.1f}%;background:linear-gradient('
                            f'90deg,#f87171,#fbbf24,#22d3a0);border-radius:10px"></div>'
                            f'<div style="position:absolute;left:{pct_in_range:.1f}%;top:0;'
                            f'width:3px;height:100%;background:#e2e8f0;border-radius:2px"></div>'
                            f'</div>'
                            f'<div style="display:flex;justify-content:space-between;'
                            f'font-family:IBM Plex Mono,monospace;font-size:10px;color:#64748b">'
                            f'<span>52w Low ${lo_52:,.2f}</span>'
                            f'<span style="color:#e2e8f0">{pct_in_range:.0f}% of range</span>'
                            f'<span>52w High ${hi_52:,.2f}</span>'
                            f'</div>',
                            unsafe_allow_html=True,
                        )

                    # ── Full analysis + trade setup ───────────────────────────
                    if "Full" in adhoc_mode:
                        st.divider()
                        st.markdown("**Full Tradeability + Trade Setup**")

                        # Build a minimal tradeability result
                        adhoc_trade = compute_tradeability(
                            ticker=adhoc_ticker,
                            sentiment_score=None,
                            article_count=0,
                            articles=[],
                            sentiment_history=[],
                            price_data=price_data_adhoc,
                            earnings_events=[],
                            daily_returns=daily_returns,
                            weights=None,
                        )

                        t_score = adhoc_trade["score"]
                        t_color = adhoc_trade["color"]
                        t_label = adhoc_trade["label"]

                        st.markdown(
                            f'<div style="background:{t_color}18;border:1px solid {t_color}44;'
                            f'border-radius:8px;padding:12px 18px;margin-bottom:12px;'
                            f'display:flex;align-items:center;justify-content:space-between">'
                            f'<div>'
                            f'<div style="font-family:IBM Plex Mono,monospace;font-size:11px;'
                            f'color:#64748b">TRADEABILITY (technical + momentum only — no sentiment)</div>'
                            f'<div style="font-family:IBM Plex Mono,monospace;font-size:28px;'
                            f'font-weight:700;color:{t_color}">{t_score:+.3f}</div>'
                            f'</div>'
                            f'<div style="font-family:IBM Plex Mono,monospace;font-size:16px;'
                            f'font-weight:600;color:{t_color};background:{t_color}22;'
                            f'padding:8px 18px;border-radius:6px;border:1px solid {t_color}44">'
                            f'{t_label}</div>'
                            f'</div>',
                            unsafe_allow_html=True,
                        )

                        # ── Trade setup, priced from the live chain ───────────
                        setup = generate_trade_setup(adhoc_ticker, adhoc_trade)

                        if not setup.get("tradeable"):
                            st.info(
                                setup.get("rationale")
                                or setup.get("error")
                                or "No actionable setup for this ticker."
                            )
                        else:
                            s1, s2, s3, s4 = st.columns(4)
                            s1.metric("Strategy", setup["strategy"])
                            s1.caption(f"{setup['expiry']} · {setup['dte']}d")
                            s2.metric(
                                "Credit" if setup["is_credit"] else "Debit",
                                f"${abs(setup['net_debit']) * 100:,.0f}",
                                help="Per contract, at a conservative fill.",
                            )
                            s3.metric(
                                "Max loss",
                                f"${setup['max_loss_per_contract']:,.0f}"
                                if setup.get("max_loss_per_contract") else "—",
                            )
                            s4.metric(
                                "Breakeven",
                                f"${setup['breakeven']:.2f}" if setup.get("breakeven") else "—",
                                delta=(f"{setup['breakeven_move_pct']:+.1f}%"
                                       if setup.get("breakeven_move_pct") is not None else None),
                            )

                            st.markdown("**Legs**")
                            st.dataframe(
                                pd.DataFrame([
                                    {
                                        "Action": l["action"],
                                        "Contract": f"{l['expiry']} ${l['strike']:g}{l['right']}",
                                        "Bid": f"${l['bid']:.2f}", "Ask": f"${l['ask']:.2f}",
                                        "Delta": f"{l['delta']:+.3f}" if l.get("delta") is not None else "—",
                                        "IV": f"{l['iv']*100:.1f}%" if l.get("iv") else "—",
                                        "OI": f"{l['open_interest']:,}",
                                    }
                                    for l in setup["legs"]
                                ]),
                                use_container_width=True, hide_index=True,
                            )
                            st.caption(
                                f"Option R/R {setup['rr_ratio']:.2f}  ·  "
                                f"PoP {setup['probability_of_profit']:.0f}%"
                                if setup.get("probability_of_profit")
                                else f"Option R/R {setup['rr_ratio']:.2f}"
                            )
                            st.caption(setup["rationale"])
                            for warning in setup.get("warnings", []):
                                st.warning(warning, icon="⚠️")
                            st.warning(setup["disclaimer"])

                except _AdhocPriceError as e:
                    st.error(
                        f"Could not retrieve a price for **{e.ticker}**. "
                        f"Check the ticker symbol."
                    )
                except Exception as e:
                    st.error(f"Analysis failed for {adhoc_ticker}: {e}")

        elif adhoc_go and not adhoc_ticker:
            st.warning("Please enter a ticker symbol.")

    st.divider()

    # ── KPI row ───────────────────────────────────────────────────────────────
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Companies Tracked", len(companies))
    k2.metric("Total Articles", f"{total_articles:,}")
    k3.metric("Upcoming Earnings", earnings_count, help="Next 7 days")
    if recent_run:
        k4.metric("Last Run", elapsed_str, help=f"Status: {recent_run.status}")
    else:
        k4.metric("Last Run", "Never")

    st.divider()

    # ── Rolling 2-week daily % change table ───────────────────────────────────
    st.subheader("Rolling Price History — Daily % Change (14 trading days)")
    st.caption("Live from Yahoo Finance. Cached for 15 minutes.")

    @st.cache_data(ttl=900)
    def fetch_price_history(tickers: tuple, days: int = 20):
        """
        Fetch daily close prices for all basket tickers via yfinance.
        Returns a DataFrame of daily % changes, columns = tickers, rows = dates.
        Uses extra days buffer to guarantee 14 trading days after weekends/holidays.
        """
        import yfinance as yf
        from datetime import date, timedelta

        end   = date.today()
        start = end - timedelta(days=days)

        try:
            raw = yf.download(
                list(tickers),
                start=str(start),
                end=str(end + timedelta(days=1)),
                progress=False,
                auto_adjust=True,
            )
            if raw.empty:
                return None, None

            # Extract Close prices — handle both single and multi-ticker formats
            if isinstance(raw.columns, pd.MultiIndex):
                closes = raw["Close"]
            else:
                closes = raw[["Close"]].rename(columns={"Close": tickers[0]})

            closes = closes.dropna(how="all")

            # Keep only the last 14 trading days
            closes = closes.tail(14)

            # Daily % change
            pct_chg = closes.pct_change() * 100
            pct_chg = pct_chg.iloc[1:]   # drop first row (NaN)

            # Last close prices for the price row
            last_prices = closes.iloc[-1]

            return pct_chg, last_prices

        except Exception as e:
            return None, None

    basket_tickers = tuple(c.ticker for c in companies)

    if basket_tickers:
        with st.spinner("Fetching price history…"):
            pct_df, last_prices = fetch_price_history(basket_tickers)

        if pct_df is None or pct_df.empty:
            st.info("Price history unavailable — Yahoo Finance may be temporarily unreachable.")
        else:
            # ── Heatmap chart ─────────────────────────────────────────────────
            # Reorder columns to match basket order
            cols_ordered = [t for t in basket_tickers if t in pct_df.columns]
            pct_df       = pct_df[cols_ordered]

            # Build heatmap: rows=dates, columns=tickers
            dates_fmt  = [d.strftime("%b %d") for d in pct_df.index]
            z_values   = pct_df.values.tolist()

            # Custom diverging colorscale: red → white → green
            colorscale = [
                [0.0,  "#ef4444"],
                [0.35, "#fca5a5"],
                [0.50, "#1e293b"],
                [0.65, "#86efac"],
                [1.0,  "#22d3a0"],
            ]

            fig_heat = go.Figure(go.Heatmap(
                z=z_values,
                x=cols_ordered,
                y=dates_fmt,
                colorscale=colorscale,
                zmid=0,
                zmin=-5,
                zmax=5,
                text=[[f"{v:+.2f}%" if v == v else "—" for v in row] for row in z_values],
                texttemplate="%{text}",
                textfont=dict(size=11, family="IBM Plex Mono"),
                hovertemplate="<b>%{x}</b> on %{y}<br>Change: %{text}<extra></extra>",
                colorbar=dict(
                    title=dict(text="% Chg", font=dict(family="IBM Plex Mono", size=10, color="#94a3b8")),
                    ticksuffix="%",
                    thickness=12,
                    len=0.8,
                    tickfont=dict(family="IBM Plex Mono", size=10, color="#94a3b8"),
                ),
            ))
            fig_heat.update_layout(
                height=max(340, len(dates_fmt) * 26),
                margin=dict(l=10, r=60, t=10, b=10),
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)",
                font=dict(family="IBM Plex Mono", color="#94a3b8"),
                xaxis=dict(side="top", tickangle=0, gridcolor="rgba(0,0,0,0)"),
                yaxis=dict(autorange="reversed", gridcolor="rgba(255,255,255,0.04)"),
            )
            st.plotly_chart(fig_heat, use_container_width=True)

            # ── Numeric table below heatmap ───────────────────────────────────
            with st.expander("View as table", expanded=False):
                display_df = pct_df.copy()
                display_df.index = [d.strftime("%Y-%m-%d (%a)") for d in display_df.index]

                # Format each cell
                def fmt_cell(v):
                    if v != v:  # NaN
                        return "—"
                    sign = "+" if v >= 0 else ""
                    return f"{sign}{v:.2f}%"

                styled = display_df.map(fmt_cell)
                st.dataframe(styled, use_container_width=True)

            # ── Per-ticker summary strip ───────────────────────────────────────
            st.markdown("**14-day summary**")
            sum_cols = st.columns(len(cols_ordered))
            for col, ticker in zip(sum_cols, cols_ordered):
                if ticker not in pct_df.columns:
                    continue
                series    = pct_df[ticker].dropna()
                if series.empty:
                    continue
                total_chg = ((1 + series / 100).prod() - 1) * 100
                best_day  = series.max()
                worst_day = series.min()
                up_days   = (series > 0).sum()
                color     = "#22d3a0" if total_chg >= 0 else "#f87171"
                sign      = "+" if total_chg >= 0 else ""
                price_str = f"${last_prices[ticker]:.2f}" if last_prices is not None and ticker in last_prices else "—"

                col.markdown(
                    f'<div style="background:rgba(255,255,255,0.02);border:1px solid rgba(255,255,255,0.07);'
                    f'border-radius:6px;padding:8px 10px;text-align:center">'
                    f'<div style="font-family:IBM Plex Mono,monospace;font-size:12px;'
                    f'font-weight:600;color:#22d3a0">{ticker}</div>'
                    f'<div style="font-family:IBM Plex Mono,monospace;font-size:10px;'
                    f'color:#64748b;margin-bottom:4px">{price_str}</div>'
                    f'<div style="font-family:IBM Plex Mono,monospace;font-size:16px;'
                    f'font-weight:700;color:{color}">{sign}{total_chg:.1f}%</div>'
                    f'<div style="font-size:9px;color:#475569;margin-top:3px">'
                    f'{up_days}/{len(series)} up days</div>'
                    f'<div style="font-size:9px;color:#334155">'
                    f'Best {best_day:+.1f}% / Worst {worst_day:+.1f}%</div>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

    session.close()


# ═══════════════════════════════════════════════════════════════════════════════
# TRADEABILITY PAGE
# ═══════════════════════════════════════════════════════════════════════════════
elif page == "Tradeability":
    st.title("Tradeability Score")
    st.caption(
        "A composite options-trading metric combining five signal categories. "
        "Adjust the category weights below to match your trading style."
    )

    # ── Weight controls ───────────────────────────────────────────────────────
    # All nine categories are exposed. The old panel only had five sliders, but
    # the weights dict it produced was passed straight to _normalise_weights,
    # which assigns 0.0 to any category missing from the dict — so simply opening
    # this expander silently zeroed options_iv, volume, short_interest and
    # analyst, i.e. 35% of the model including the IV regime signal.
    CATEGORY_META = {
        "sentiment":      {"label": "📰 Sentiment",      "color": "#60a5fa", "help": "News sentiment: recency-decayed score, momentum vs prior runs, source diversity. Deduplicated at the headline level so syndicated wire copy counts once."},
        "technical":      {"label": "📈 Technical",      "color": "#22d3a0", "help": "52-week range position, day change, analyst consensus, breakout proximity. Ranked cross-sectionally within the basket."},
        "price_momentum": {"label": "📉 Momentum",       "color": "#34d399", "help": "Wilder RSI over 120 bars, trend slope, annualised volatility, day streak, 5d vs 14d acceleration."},
        "fundamental":    {"label": "📊 Fundamental",    "color": "#a78bfa", "help": "P/E percentile within the basket, average EPS surprise %, beat streak."},
        "event_driven":   {"label": "⚡ Event-Driven",   "color": "#f87171", "help": "Earnings proximity (negative — IV crush risk for long options), last quarter's surprise, 52w high breakout."},
        "volume":         {"label": "🔊 Volume",         "color": "#fbbf24", "help": "Today's volume vs the 30-day average, signed by price direction."},
        "short_interest": {"label": "🩳 Short Interest",  "color": "#fb923c", "help": "Days to cover and short % of float, read as squeeze or distribution depending on price direction."},
        "options_iv":     {"label": "🌊 Options IV",     "color": "#38bdf8", "help": "IV rank regime from stored option-chain history. Drives strategy selection: high rank favours selling premium, low rank favours buying it."},
        "analyst":        {"label": "🎯 Analyst",        "color": "#c084fc", "help": "Consensus mean score and price-target upside, ranked within the basket."},
    }

    with st.expander("⚖️  Adjust category weights", expanded=False):
        st.caption(
            "How much each signal category contributes. Weights are re-normalised "
            "to sum to 100%. Categories with no data are excluded entirely rather "
            "than counted as neutral."
        )

        raw_weights = {}
        for row_keys in (list(CATEGORY_META)[:5], list(CATEGORY_META)[5:]):
            cols = st.columns(len(row_keys))
            for col, key in zip(cols, row_keys):
                meta = CATEGORY_META[key]
                # `value=` and `key=` must not both be supplied — Streamlit then
                # has two sources of truth for the widget and warns/desyncs.
                # session_state seeded once is the correct pattern.
                st.session_state.setdefault(f"tw_{key}", int(DEFAULT_WEIGHTS[key] * 100))
                raw_weights[key] = col.slider(
                    meta["label"], min_value=0, max_value=100, step=1,
                    help=meta["help"], key=f"tw_{key}",
                )

        total_raw = sum(raw_weights.values())
        if total_raw == 0:
            st.warning("All weights are zero — falling back to defaults.")
            weights = dict(DEFAULT_WEIGHTS)
        else:
            weights = {k: v / total_raw for k, v in raw_weights.items()}

        pct_rows = (list(CATEGORY_META)[:5], list(CATEGORY_META)[5:])
        for row_keys in pct_rows:
            cols = st.columns(len(row_keys))
            for col, key in zip(cols, row_keys):
                col.markdown(
                    f'<div style="text-align:center;font-family:IBM Plex Mono,monospace;'
                    f'font-size:11px;color:{CATEGORY_META[key]["color"]}">'
                    f'{weights[key]*100:.1f}%</div>',
                    unsafe_allow_html=True,
                )

        if st.button("Reset to defaults", key="reset_weights"):
            for key in CATEGORY_META:
                st.session_state[f"tw_{key}"] = int(DEFAULT_WEIGHTS[key] * 100)
            st.rerun()

    st.divider()

    # ── Compute scores ────────────────────────────────────────────────────────
    results = cached_tradeability(weights)

    if not results:
        st.info("No data yet. Run the scraper first: `python3 -m mktscan run --mode all`")
        st.stop()

    # Warn when IV rank is unavailable — without it the strategy selector falls
    # back to its least-informed branch, and the user should know that.
    no_iv = [t for t, r in results.items() if r.get("iv_basis") != "chain"]
    if len(no_iv) == len(results):
        st.warning(
            "**IV rank unavailable for the whole basket.** Strategy selection is "
            "running on its fallback branch (debit spreads at reduced size). "
            "Seed the history with `python3 -m mktscan iv --backfill`, then "
            "`python3 -m mktscan iv --update` daily.",
            icon="🌊",
        )
    elif no_iv:
        st.caption(f"⚠️ IV rank unavailable for: {', '.join(sorted(no_iv))}")

    # Sort by score descending
    sorted_results = sorted(results.items(), key=lambda x: -x[1]["score"])

    # ── KPI strip ─────────────────────────────────────────────────────────────
    scores_list = [v["score"] for v in results.values()]
    avg_trade   = sum(scores_list) / len(scores_list)
    strong_buy  = sum(1 for s in scores_list if s >  0.50)
    bearish     = sum(1 for s in scores_list if s < -0.20)
    best        = sorted_results[0]
    worst       = sorted_results[-1]

    k1, k2, k3, k4, k5 = st.columns(5)
    k1.metric("Avg Tradeability", f"{avg_trade:+.3f}")
    k2.metric("Strong Buy",       strong_buy)
    k3.metric("Bearish / Avoid",  bearish)
    k4.metric("Top Pick",         best[0],  delta=f"{best[1]['score']:+.3f}",  delta_color="normal")
    k5.metric("Weakest",          worst[0], delta=f"{worst[1]['score']:+.3f}", delta_color="inverse")

    st.divider()

    # ── Main layout: scoreboard + detail panel ────────────────────────────────
    col_board, col_detail = st.columns([2, 3])

    with col_board:
        st.subheader("Scoreboard")

        # Horizontal bar chart
        tickers_sorted = [t for t, _ in sorted_results]
        scores_sorted  = [v["score"] for _, v in sorted_results]
        colors_sorted  = [tradeability_color(s) for s in scores_sorted]
        labels_sorted  = [tradeability_label(s) for s in scores_sorted]

        fig = go.Figure()
        fig.add_trace(go.Bar(
            y=tickers_sorted,
            x=scores_sorted,
            orientation="h",
            marker_color=colors_sorted,
            text=[f"{s:+.3f}  {l}" for s, l in zip(scores_sorted, labels_sorted)],
            textposition="outside",
            hovertemplate="<b>%{y}</b><br>Score: %{x:.4f}<extra></extra>",
        ))
        fig.add_vline(x=0,     line_color="rgba(255,255,255,0.2)", line_width=1)
        fig.add_vline(x=0.5,   line_color="#22d3a0", line_width=0.5, line_dash="dot")
        fig.add_vline(x=0.2,   line_color="#86efac", line_width=0.5, line_dash="dot")
        fig.add_vline(x=-0.2,  line_color="#fca5a5", line_width=0.5, line_dash="dot")
        fig.add_vline(x=-0.5,  line_color="#f87171", line_width=0.5, line_dash="dot")
        fig.update_layout(
            height=max(320, len(tickers_sorted) * 44),
            margin=dict(l=10, r=120, t=10, b=10),
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            font=dict(family="IBM Plex Mono", color="#94a3b8"),
            xaxis=dict(range=[-1.15, 1.15], gridcolor="rgba(255,255,255,0.05)", zeroline=False),
            yaxis=dict(gridcolor="rgba(0,0,0,0)"),
            showlegend=False,
        )
        st.plotly_chart(fig, use_container_width=True)

        # Score legend
        st.markdown(
            '<div style="display:flex;flex-wrap:wrap;gap:8px;margin-top:4px">'
            + "".join([
                f'<div style="display:flex;align-items:center;gap:5px">'
                f'<div style="width:10px;height:10px;border-radius:2px;background:{c}"></div>'
                f'<span style="font-size:10px;color:#64748b;font-family:IBM Plex Mono,monospace">{l}</span>'
                f'</div>'
                for c, l in [
                    ("#22d3a0","STRONG BUY > +0.5"),
                    ("#86efac","BULLISH > +0.2"),
                    ("#fbbf24","NEUTRAL"),
                    ("#fca5a5","BEARISH < -0.2"),
                    ("#f87171","STRONG SELL < -0.5"),
                ]
            ])
            + '</div>',
            unsafe_allow_html=True,
        )

    with col_detail:
        st.subheader("Category Breakdown")

        selected_ticker = st.selectbox(
            "Inspect ticker",
            tickers_sorted,
            key="trade_inspect",
        )
        res = results.get(selected_ticker, {})
        cats = res.get("categories", {})

        if not cats:
            st.info("No data for this ticker.")
        else:
            # Overall score badge
            score = res["score"]
            color = res["color"]
            label = res["label"]
            st.markdown(
                f'<div style="background:rgba(255,255,255,0.03);border:1px solid {color}44;'
                f'border-radius:8px;padding:14px 18px;margin-bottom:16px;'
                f'display:flex;align-items:center;justify-content:space-between">'
                f'<div>'
                f'<div style="font-family:IBM Plex Mono,monospace;font-size:13px;color:#64748b">TRADEABILITY SCORE</div>'
                f'<div style="font-family:IBM Plex Mono,monospace;font-size:32px;font-weight:700;color:{color}">'
                f'{score:+.3f}</div>'
                f'</div>'
                f'<div style="font-family:IBM Plex Mono,monospace;font-size:16px;font-weight:600;'
                f'color:{color};background:{color}18;padding:8px 16px;border-radius:6px;'
                f'border:1px solid {color}44">{label}</div>'
                f'</div>',
                unsafe_allow_html=True,
            )

            # Category breakdown bars
            for cat_key, meta in CATEGORY_META.items():
                cat_data   = cats.get(cat_key, {})
                cat_score  = cat_data.get("score", 0.0)
                cat_conf   = cat_data.get("confidence", 0.0)
                cat_detail = cat_data.get("detail", "—")
                cat_weight = weights.get(cat_key, 0.0)
                cat_color  = meta["color"]

                # Extra pills for price momentum
                extra_pills = ""
                if cat_key == "price_momentum" and cat_data.get("rsi") is not None:
                    rsi = cat_data["rsi"]
                    vol = cat_data.get("annual_vol", 0)
                    streak = cat_data.get("streak", 0)
                    ret14 = cat_data.get("total_return_14d", 0)
                    rsi_col = "#f87171" if rsi > 70 else "#22d3a0" if rsi < 30 else "#fbbf24"
                    streak_col = "#22d3a0" if streak > 0 else "#f87171" if streak < 0 else "#64748b"
                    ret_col = "#22d3a0" if ret14 >= 0 else "#f87171"
                    extra_pills = (
                        f'<div style="display:flex;gap:6px;margin-top:5px;flex-wrap:wrap">'
                        f'<span style="font-family:IBM Plex Mono,monospace;font-size:10px;'
                        f'background:rgba(255,255,255,0.05);padding:2px 7px;border-radius:3px;'
                        f'color:{rsi_col}">RSI {rsi:.0f}</span>'
                        f'<span style="font-family:IBM Plex Mono,monospace;font-size:10px;'
                        f'background:rgba(255,255,255,0.05);padding:2px 7px;border-radius:3px;'
                        f'color:#94a3b8">Vol {vol:.0f}%</span>'
                        f'<span style="font-family:IBM Plex Mono,monospace;font-size:10px;'
                        f'background:rgba(255,255,255,0.05);padding:2px 7px;border-radius:3px;'
                        f'color:{streak_col}">Streak {streak_col and streak:+d}d</span>'
                        f'<span style="font-family:IBM Plex Mono,monospace;font-size:10px;'
                        f'background:rgba(255,255,255,0.05);padding:2px 7px;border-radius:3px;'
                        f'color:{ret_col}">14d {ret14:+.1f}%</span>'
                        f'</div>'
                    )

                bar_pct    = abs(cat_score) * 100
                bar_color  = "#22d3a0" if cat_score >= 0 else "#f87171"

                st.markdown(
                    f'<div style="background:rgba(255,255,255,0.02);border:1px solid rgba(255,255,255,0.06);'
                    f'border-radius:6px;padding:10px 14px;margin-bottom:8px">'

                    f'<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">'
                    f'<span style="font-size:13px;color:#e2e8f0;font-weight:500">{meta["label"]}</span>'
                    f'<div style="display:flex;align-items:center;gap:10px">'
                    f'<span style="font-family:IBM Plex Mono,monospace;font-size:10px;color:#64748b">'
                    f'weight {cat_weight*100:.0f}%</span>'
                    f'<span style="font-family:IBM Plex Mono,monospace;font-size:14px;font-weight:600;'
                    f'color:{bar_color}">{cat_score:+.3f}</span>'
                    f'</div></div>'

                    f'<div style="background:rgba(255,255,255,0.05);border-radius:3px;height:5px;'
                    f'margin-bottom:6px;overflow:hidden">'
                    f'<div style="width:{bar_pct:.0f}%;height:100%;background:{bar_color};'
                    f'border-radius:3px;margin-left:{"0" if cat_score >= 0 else "auto"}"></div>'
                    f'</div>'

                    f'<div style="display:flex;justify-content:space-between;align-items:center">'
                    f'{extra_pills}'
                    f'<span style="font-size:10px;color:#475569;font-family:IBM Plex Mono,monospace">'
                    f'{cat_detail[:80]}{"…" if len(cat_detail) > 80 else ""}</span>'
                    f'<span style="font-size:10px;color:#334155;font-family:IBM Plex Mono,monospace">'
                    f'conf {cat_conf:.0%}</span>'
                    f'</div>'

                    f'</div>',
                    unsafe_allow_html=True,
                )

            # Component deep-dive
            with st.expander("Component detail", expanded=False):
                for cat_key, meta in CATEGORY_META.items():
                    comps = cats.get(cat_key, {}).get("components", {})
                    if not comps:
                        continue
                    st.markdown(f"**{meta['label']}**")
                    comp_rows = [
                        {"Component": k.replace("_", " ").title(),
                         "Score": format_component_value(v)}
                        for k, v in comps.items()
                    ]
                    st.dataframe(pd.DataFrame(comp_rows), use_container_width=True, hide_index=True)

    st.divider()

    # ── Trade Setups ──────────────────────────────────────────────────────────
    st.subheader("Trade Setups — priced from the live option chain")
    st.caption(
        "Strikes are selected by delta from the actual chain and filtered for "
        "liquidity (open interest, bid/ask spread). Every figure below — premium, "
        "max loss, breakeven, risk/reward — is an option-level number, computed on "
        "the structure itself rather than on the underlying."
    )
    st.warning(DISCLAIMER)

    with st.spinner("Pricing option chains…"):
        setups = cached_setups(results_digest(results), results)

    if not setups:
        st.info("No setups generated — run the scraper first to populate data.")
    else:
        tradeable = {t: s for t, s in setups.items() if s.get("tradeable")}
        skipped   = {t: s for t, s in setups.items() if not s.get("tradeable")}

        sc1, sc2, sc3 = st.columns(3)
        sc1.metric("Tradeable setups", len(tradeable))
        sc2.metric("No trade / avoid", len(skipped))
        total_risk = sum(s.get("max_loss_per_contract") or 0 for s in tradeable.values())
        sc3.metric("Total risk, 1 contract each", f"${total_risk:,.0f}")

        # ── Summary table ─────────────────────────────────────────────────────
        if tradeable:
            summary_rows = []
            for ticker, s in sorted(tradeable.items(),
                                    key=lambda kv: -abs(kv[1].get("tradeability", 0))):
                summary_rows.append({
                    "Ticker":     ticker,
                    "Score":      f"{s['tradeability']:+.3f}",
                    "Strategy":   s["strategy"],
                    "Expiry":     f"{s['expiry']} ({s['dte']}d)",
                    "Net":        f"${s['net_debit']:+.2f}",
                    "Max Loss":   f"${s['max_loss_per_contract']:,.0f}" if s.get("max_loss_per_contract") else "—",
                    "Max Gain":   f"${s['max_profit_per_contract']:,.0f}" if s.get("max_profit_per_contract") else "unbounded",
                    "Breakeven":  f"${s['breakeven']:.2f}" if s.get("breakeven") else "—",
                    "BE Move":    f"{s['breakeven_move_pct']:+.1f}%" if s.get("breakeven_move_pct") is not None else "—",
                    "PoP":        f"{s['probability_of_profit']:.0f}%" if s.get("probability_of_profit") else "—",
                    "R/R":        f"{s['rr_ratio']:.2f}",
                    "Size":       s["sizing"],
                    "Conf":       s["confidence_tier"],
                })
            st.dataframe(pd.DataFrame(summary_rows), use_container_width=True, hide_index=True)
            st.caption(
                "R/R is reward at the price target versus loss at the stop, both "
                "computed by repricing the option structure with time decay — not "
                "the ratio of the underlying's move, which is what the previous "
                "version reported and which understated both sides substantially."
            )

        # ── Detail cards ──────────────────────────────────────────────────────
        for ticker, s in sorted(setups.items(),
                                key=lambda kv: -abs(kv[1].get("tradeability", 0))):
            score     = s.get("tradeability", 0.0)
            strategy  = s.get("strategy", "—")
            direction = s.get("direction", "NEUTRAL")
            dir_icon  = ("🟢" if direction == "BULLISH" else
                         "🔴" if direction == "BEARISH" else "⚪")

            if not s.get("tradeable"):
                reason_text = {
                    "earnings_too_close": "Earnings blackout",
                    "no_edge":            "No directional edge",
                    "illiquid":           "Options too illiquid",
                    "no_chain":           "No chain in expiry window",
                    "no_price":           "Price unavailable",
                    "error":              "Error",
                }.get(s.get("reason", ""), s.get("reason", "—"))

                with st.expander(f"{ticker}  ·  ⚪ {reason_text}  ·  Score {score:+.3f}",
                                 expanded=False):
                    st.info(s.get("rationale") or s.get("error") or reason_text)
                    if s.get("iv_note"):
                        st.caption(s["iv_note"])
                continue

            header = (
                f"{ticker}  ·  {dir_icon} {strategy}  ·  ${s['spot']:.2f}  ·  "
                f"Risk ${s['max_loss_per_contract']:,.0f}/contract  ·  "
                f"R/R {s['rr_ratio']:.2f}  ·  {s['confidence_tier']}"
            )
            with st.expander(header, expanded=abs(score) >= 0.35):

                # ── Economics ─────────────────────────────────────────────────
                st.markdown("**Trade economics** — per contract (100 shares)")
                e1, e2, e3, e4, e5 = st.columns(5)
                e1.metric(
                    "Credit received" if s["is_credit"] else "Debit paid",
                    f"${abs(s['net_debit']) * 100:,.0f}",
                    help="At a conservative fill: pay the ask, receive the bid.",
                )
                e1.caption(f"mid ${abs(s['net_debit_mid']) * 100:,.0f}")

                e2.metric("Max loss", f"${s['max_loss_per_contract']:,.0f}",
                          help="Worst case at expiry. This is the capital at risk.")
                e3.metric(
                    "Max profit",
                    f"${s['max_profit_per_contract']:,.0f}" if s.get("max_profit_per_contract") else "Unbounded",
                )
                if s.get("max_return_pct"):
                    e3.caption(f"{s['max_return_pct']:.0f}% of risk")

                e4.metric("Breakeven", f"${s['breakeven']:.2f}" if s.get("breakeven") else "—",
                          delta=f"{s['breakeven_move_pct']:+.1f}%" if s.get("breakeven_move_pct") is not None else None,
                          help="Underlying price at which the position breaks even at expiry.")
                e5.metric("Prob. of profit",
                          f"{s['probability_of_profit']:.0f}%" if s.get("probability_of_profit") else "—",
                          help="Risk-neutral probability of finishing past breakeven. "
                               "Not the same as probability of finishing in the money.")

                # ── Legs ──────────────────────────────────────────────────────
                st.markdown("**Legs**")
                leg_rows = []
                for leg in s["legs"]:
                    leg_rows.append({
                        "Action":  leg["action"],
                        "Contract": f"{leg['expiry']} ${leg['strike']:g}{leg['right']}",
                        "Bid":     f"${leg['bid']:.2f}",
                        "Ask":     f"${leg['ask']:.2f}",
                        "Mid":     f"${leg['mid']:.2f}",
                        "Spread":  f"{leg['spread_pct']*100:.1f}%" if leg.get("spread_pct") else "—",
                        "Delta":   f"{leg['delta']:+.3f}" if leg.get("delta") is not None else "—",
                        "IV":      f"{leg['iv']*100:.1f}%" if leg.get("iv") else "—",
                        "OI":      f"{leg['open_interest']:,}",
                        "Vol":     f"{leg['volume']:,}",
                    })
                st.dataframe(pd.DataFrame(leg_rows), use_container_width=True, hide_index=True)

                # ── Greeks and levels ─────────────────────────────────────────
                g1, g2, g3, g4 = st.columns(4)
                g1.metric("Net delta", f"{s['net_delta']:+.2f}",
                          help="Share-equivalent exposure per contract: multiply by 100.")
                g2.metric("Theta / day", f"${s['net_theta_per_day_per_contract']:+,.2f}",
                          help="Expected daily P&L from time decay alone, holding price and IV constant.")
                g3.metric("Vega", f"${s['net_vega_per_contract']:+,.2f}",
                          help="P&L per 1-point change in implied volatility.")
                g4.metric("Slippage est.", f"${s['slippage_per_contract']:,.0f}",
                          help="Gap between a mid fill and the conservative fill quoted above.")

                l1, l2, l3, l4 = st.columns(4)
                l1.metric("Spot", f"${s['spot']:.2f}")
                l1.caption(f"ATR ${s['atr']:.2f} ({s['atr_pct']:.1f}%)")
                l2.metric("Price target", f"${s['price_target']:.2f}",
                          delta=f"{(s['price_target']-s['spot'])/s['spot']*100:+.1f}%")
                if s.get("resistance"):
                    l2.caption(f"Resistance ${s['resistance']:.2f}")
                l3.metric("Price stop", f"${s['price_stop']:.2f}",
                          delta=f"{(s['price_stop']-s['spot'])/s['spot']*100:+.1f}%",
                          delta_color="inverse")
                if s.get("support"):
                    l3.caption(f"Support ${s['support']:.2f}")
                l4.metric("Option R/R", f"{s['rr_ratio']:.2f}")
                l4.caption(
                    f"+${s['pnl_at_target_per_contract']:,.0f} / "
                    f"${s['pnl_at_stop_per_contract']:,.0f} at day {s['hold_days']}"
                )

                # ── Context ───────────────────────────────────────────────────
                ctx = []
                if s.get("rsi") is not None:
                    ctx.append(f"RSI {s['rsi']:.0f}")
                if s.get("annual_vol") is not None:
                    ctx.append(f"realised vol {s['annual_vol']:.0f}%")
                if s.get("iv_rank") is not None:
                    ctx.append(f"IV rank {s['iv_rank']:.0f} ({s.get('iv_basis','?')})")
                if s.get("avg_iv"):
                    ctx.append(f"chain IV {s['avg_iv']*100:.0f}%")
                if s.get("days_to_earn") is not None:
                    ctx.append(f"earnings in {s['days_to_earn']}d")
                if s.get("coverage") is not None:
                    ctx.append(f"model coverage {s['coverage']*100:.0f}%")
                if ctx:
                    st.caption("  ·  ".join(ctx))

                st.markdown("**Rationale**")
                st.markdown(
                    f'<div style="font-size:13px;color:#94a3b8;line-height:1.7;padding:12px 16px;'
                    f'background:rgba(255,255,255,0.02);border-radius:6px;'
                    f'border:1px solid rgba(255,255,255,0.06)">{s["rationale"]}</div>',
                    unsafe_allow_html=True,
                )
                if s.get("iv_note"):
                    st.markdown(
                        f'<div style="font-size:12px;color:#fbbf24;padding:7px 12px;'
                        f'margin-top:6px;background:rgba(251,191,36,0.08);border-radius:4px;'
                        f'border-left:2px solid #fbbf24">{s["iv_note"]}</div>',
                        unsafe_allow_html=True,
                    )
                for warning in s.get("warnings", []):
                    st.warning(warning, icon="⚠️")

    st.divider()

    # ── Full basket comparison table ──────────────────────────────────────────
    st.subheader("Full Basket Comparison")
    table_rows = []
    for ticker, res in sorted_results:
        cats = res.get("categories", {})
        table_rows.append({
            "Ticker":          ticker,
            "Tradeability":    f"{res['score']:+.3f}",
            "Signal":          res["label"],
            "Sentiment":      f"{cats.get('sentiment',{}).get('score',0):+.3f}",
            "Technical":      f"{cats.get('technical',{}).get('score',0):+.3f}",
            "Price Momentum": f"{cats.get('price_momentum',{}).get('score',0):+.3f}",
            "Fundamental":    f"{cats.get('fundamental',{}).get('score',0):+.3f}",
            "Event-Driven":   f"{cats.get('event_driven',{}).get('score',0):+.3f}",
        })
    st.dataframe(pd.DataFrame(table_rows), use_container_width=True, hide_index=True)

    st.divider()

    # ── Signal Accuracy & Feedback Panel ─────────────────────────────────────
    st.subheader("Signal Accuracy & Feedback Calibration")
    st.caption(
        "Tracks how often each tradeability score correctly predicted the next-day "
        "price direction. Builds over time — more runs = more accurate calibration. "
        "The feedback adjustment is applied automatically to future scores."
    )

    session_fb = get_session()
    basket_tickers_fb = [c.ticker for c in get_basket(session_fb)]
    acc_stats_all = get_basket_accuracy_stats(session_fb, basket_tickers_fb)
    session_fb.close()

    # ── Pooled result first ───────────────────────────────────────────────────
    # Per-ticker samples are small and will stay small for months; the pooled
    # figure is the only one likely to reach a usable size in year one.
    try:
        from mktscan.feedback import get_aggregate_stats
        _fb_session = get_session()
        try:
            agg = get_aggregate_stats(_fb_session, basket_tickers_fb)
        finally:
            _fb_session.close()
    except Exception:
        agg = {"n_observations": 0}

    if agg.get("n_observations"):
        a1, a2, a3, a4 = st.columns(4)
        a1.metric("Pooled observations", f"{agg['n_observations']:,}",
                  help=f"Across {agg.get('n_tickers', 0)} tickers, one per ticker per day.")
        a2.metric("Direction accuracy", f"{agg['pct_correct']:.1f}%",
                  delta=f"±{agg['std_error_pct']:.1f} SE")
        a3.metric(
            "Directional edge",
            f"{agg['directional_edge_pct']:+.2f}%" if agg.get("directional_edge_pct") is not None else "—",
            help="Average return on bullish calls minus average return on bearish "
                 "calls. A model can look 'accurate' in a rising market simply by "
                 "being long-biased; this is what separates that from real signal.",
        )
        a4.metric("Significant?", "Yes" if agg["statistically_significant"] else "Not yet",
                  help="Whether accuracy is more than 2 standard errors from 50%.")
        if not agg["statistically_significant"]:
            st.caption(
                f"⚠️ {agg['pct_correct']:.1f}% on {agg['n_observations']:,} observations "
                f"is within 2 standard errors of a coin flip. Not yet evidence of an edge."
            )

    st.divider()

    # ── Per-ticker table ──────────────────────────────────────────────────────
    acc_rows = []
    for tkr in basket_tickers_fb:
        s = acc_stats_all.get(tkr, {})
        n        = s.get("n_observations", 0)
        acc      = s.get("pct_correct")
        se       = s.get("std_error_pct")
        bull_ret = s.get("avg_return_on_bull")
        bear_ret = s.get("avg_return_on_bear")
        edge     = s.get("directional_edge_pct")
        acc_rows.append({
            "Ticker":             tkr,
            "Obs":                n,
            "Horizon":            f"{s.get('horizon_days', 5)}d",
            "Direction Accuracy": (f"{acc:.1f}% ±{se:.1f}" if acc is not None and se is not None
                                   else f"{acc:.1f}%" if acc is not None else "—"),
            "Avg Return (Bull)":  f"{bull_ret:+.2f}%" if bull_ret is not None else "—",
            "Avg Return (Bear)":  f"{bear_ret:+.2f}%" if bear_ret is not None else "—",
            "Edge":               f"{edge:+.2f}%" if edge is not None else "—",
            "Significant":        "✅" if s.get("statistically_significant") else "—",
        })

    if any(r["Obs"] > 0 for r in acc_rows):
        st.dataframe(pd.DataFrame(acc_rows), use_container_width=True, hide_index=True)
        st.caption(
            "One observation per ticker per trading day, resolved over a 5-day "
            "horizon. Accuracy is shown with its binomial standard error — on "
            "30 observations that is roughly ±9 percentage points, so treat "
            "anything inside 41–59% as indistinguishable from chance."
        )
        st.info(
            "**Score adjustment is disabled.** These statistics are recorded and "
            "displayed but do not modify the tradeability scores you see. The "
            "adjustment was ±15% at full confidence — smaller than the uncertainty "
            "in the statistic driving it — and it silently changed a displayed "
            "number. Re-enable via `FEEDBACK_ADJUSTMENT_ENABLED` in feedback.py "
            "once the pooled edge above is stable and significant.",
            icon="ℹ️",
        )
    else:
        st.info(
            "No feedback data yet. One prediction is recorded per ticker per "
            "trading day and resolved 5 trading days later, so the first "
            "statistics appear about a week after the scheduler starts."
        )

    # Per-ticker calibration chart (score vs actual return scatter)
    tickers_with_data = [t for t in basket_tickers_fb
                         if acc_stats_all.get(t, {}).get("n_observations", 0) >= 3]

    if tickers_with_data:
        sel_tkr = st.selectbox(
            "View calibration chart for",
            tickers_with_data,
            key="fb_ticker_select"
        )
        hist = acc_stats_all[sel_tkr].get("history", [])
        if hist:
            df_hist = pd.DataFrame(hist)
            fig_fb = go.Figure()

            # Colour points by correct/incorrect
            colors_fb = ["#22d3a0" if r["correct"] else "#f87171" for r in hist]
            fig_fb.add_trace(go.Scatter(
                x=df_hist["score"],
                y=df_hist["actual"],
                mode="markers",
                marker=dict(color=colors_fb, size=9, opacity=0.8),
                text=[f"{r['date']}<br>Score: {r['score']:+.3f}<br>Actual: {r['actual']:+.2f}%"
                      for r in hist],
                hovertemplate="%{text}<extra></extra>",
                name="Outcomes",
            ))

            # Perfect calibration line (if score = 0.1 → actual = +0.3%)
            xs = [-1.0, 1.0]
            fig_fb.add_trace(go.Scatter(
                x=xs, y=[x * 3 for x in xs],
                mode="lines",
                line=dict(color="rgba(255,255,255,0.15)", dash="dot", width=1),
                name="Perfect calibration",
                hoverinfo="skip",
            ))
            fig_fb.add_hline(y=0, line_color="rgba(255,255,255,0.2)", line_width=1)
            fig_fb.add_vline(x=0, line_color="rgba(255,255,255,0.2)", line_width=1)

            fig_fb.update_layout(
                height=300,
                margin=dict(l=10, r=10, t=30, b=10),
                title=dict(
                    text=f"{sel_tkr} — Score vs Next-Day Actual Return",
                    font=dict(size=12, family="IBM Plex Mono", color="#94a3b8"),
                ),
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)",
                font=dict(family="IBM Plex Mono", color="#94a3b8"),
                xaxis=dict(
                    title="Tradeability Score at Prediction",
                    gridcolor="rgba(255,255,255,0.05)",
                    zeroline=False,
                ),
                yaxis=dict(
                    title="Actual Next-Day Return (%)",
                    gridcolor="rgba(255,255,255,0.05)",
                    zeroline=False,
                    ticksuffix="%",
                ),
                showlegend=True,
                legend=dict(
                    font=dict(size=10, family="IBM Plex Mono", color="#64748b"),
                ),
            )
            st.plotly_chart(fig_fb, use_container_width=True)

            # Feedback note for selected ticker
            note = acc_stats_all[sel_tkr].get("feedback_note", "")
            if note:
                st.caption(f"Current adjustment: {note}")


# ═══════════════════════════════════════════════════════════════════════════════
# NEWS FEED PAGE
# ═══════════════════════════════════════════════════════════════════════════════
elif page == "News Feed":
    st.title("News Feed")

    session = get_session()
    companies = get_basket(session)
    tickers   = ["All", "MARKET (macro wire)"] + [c.ticker for c in companies]

    col1, col2, col3 = st.columns([2, 2, 1])
    with col1:
        selected_ticker = st.selectbox("Filter by ticker", tickers)
    with col2:
        selected_source = st.selectbox(
            "Filter by source", ["All", "reuters", "yahoo", "marketwatch", "finviz", "alphav", "benzinga", "wsj"]
        )
    with col3:
        limit = st.number_input("Show", 20, 500, 50)

    query = select(Article).order_by(desc(Article.scraped_at)).limit(limit)
    actual_ticker = "MARKET" if selected_ticker == "MARKET (macro wire)" else selected_ticker
    if selected_ticker != "All":
        query = query.where(Article.ticker == actual_ticker)
    if selected_source != "All":
        query = query.where(Article.source == selected_source)

    articles = session.execute(query).scalars().all()
    session.close()

    if not articles:
        st.info("No articles found. Run the scraper to populate.")
    else:
        for a in articles:
            score_str = ""
            if a.sentiment is not None:
                col = sentiment_color(a.sentiment)
                score_str = f'<span style="color:{col};font-family:\'IBM Plex Mono\',monospace;font-size:12px;font-weight:600">{a.sentiment:+.3f}</span>'

            pub = str(a.published_at)[:10] if a.published_at else "—"
            st.markdown(
                f'<div style="border-bottom:1px solid rgba(255,255,255,0.07);padding:10px 0">'
                f'<div style="font-size:14px;margin-bottom:4px">{a.headline}</div>'
                f'<div style="display:flex;gap:12px;align-items:center">'
                f'<span style="font-family:\'IBM Plex Mono\',monospace;font-size:10px;'
                f'color:#22d3a0;background:rgba(34,211,160,0.1);padding:1px 6px;border-radius:3px">'
                f'{a.ticker}</span>'
                f'<span style="font-size:11px;color:#64748b">{a.source}</span>'
                f'<span style="font-size:11px;color:#475569">{pub}</span>'
                f'{score_str}'
                f'{"<a href=" + chr(34) + a.url + chr(34) + " target=_blank style=font-size:11px;color:#60a5fa>↗ link</a>" if a.url else ""}'
                f'</div></div>',
                unsafe_allow_html=True,
            )


# ═══════════════════════════════════════════════════════════════════════════════
# EARNINGS PAGE
# ═══════════════════════════════════════════════════════════════════════════════
elif page == "Earnings":
    st.title("Earnings")

    tab_cal, tab_hist = st.tabs(["📅  Upcoming Calendar", "📊  Historical Results"])

    # ─────────────────────────────────────────────────────────────────────────
    # TAB 1 — UPCOMING CALENDAR
    # ─────────────────────────────────────────────────────────────────────────
    with tab_cal:
        st.subheader("Broad Market — Next 30 Days")
        st.caption(
            "Earnings dates for ~80 major companies over the next 30 days, "
            "sourced live from Yahoo Finance. Your basket companies are highlighted in green. "
            "Results are cached for 1 hour."
        )

        @st.cache_data(ttl=3600)
        def fetch_earnings_calendar():
            import yfinance as yf

            UNIVERSE = list(dict.fromkeys([
                # Mega-cap tech
                "AAPL","MSFT","NVDA","GOOG","GOOGL","META","AMZN","TSLA","AVGO","AMD","TSM",
                # Broader tech
                "ORCL","CRM","ADBE","INTC","QCOM","TXN","MU","AMAT","IBM","NOW","INTU",
                "PANW","CRWD","FTNT","NET","SNOW","DDOG","PLTR",
                # Finance
                "JPM","BAC","WFC","GS","MS","C","BLK","SCHW","AXP","V","MA","PYPL","COF",
                # Healthcare
                "JNJ","UNH","PFE","MRK","ABBV","LLY","TMO","ABT","BMY","AMGN","GILD",
                # Consumer
                "WMT","COST","TGT","HD","MCD","SBUX","NKE","PG","KO","PEP","NFLX","DIS",
                # Industrials / Energy
                "XOM","CVX","COP","CAT","GE","HON","BA","UPS","FDX","LMT",
            ]))

            today   = datetime.now().date()
            cutoff  = today + timedelta(days=30)
            results = []

            for sym in UNIVERSE:
                try:
                    t   = yf.Ticker(sym)
                    cal = t.calendar
                    if cal is None:
                        continue
                    if hasattr(cal, "to_dict"):
                        cal = cal.to_dict()

                    raw = cal.get("Earnings Date")
                    if not raw:
                        continue
                    if isinstance(raw, list):
                        raw = raw[0]
                    d = raw.date() if hasattr(raw, "date") else raw
                    if not (today <= d <= cutoff):
                        continue

                    def _fmt_range(lo, hi, fmt="${:.2f}", scale=1):
                        try:
                            return f"{fmt.format(float(lo)/scale)} – {fmt.format(float(hi)/scale)}"
                        except Exception:
                            return "—"

                    eps_str = _fmt_range(cal.get("EPS Estimate Low"), cal.get("EPS Estimate High"))
                    rev_str = _fmt_range(
                        cal.get("Revenue Estimate Low"), cal.get("Revenue Estimate High"),
                        fmt="${:.1f}B", scale=1e9,
                    )
                    results.append({"date": d, "ticker": sym,
                                    "eps_range": eps_str, "rev_range": rev_str})
                except Exception:
                    continue

            results.sort(key=lambda x: x["date"])
            return results

        with st.spinner("Loading earnings calendar…"):
            cal_data = fetch_earnings_calendar()

        if not cal_data:
            st.info(
                "No upcoming earnings found or Yahoo Finance is temporarily unavailable. "
                "Try refreshing — results are cached for 1 hour."
            )
        else:
            session        = get_session()
            basket_tickers = {c.ticker for c in get_basket(session)}
            session.close()

            today = datetime.now().date()

            # Group by week
            from collections import defaultdict
            week_groups = defaultdict(list)
            for item in cal_data:
                d      = item["date"]
                monday = d - timedelta(days=d.weekday())
                week_groups[monday].append(item)

            DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri"]

            for monday in sorted(week_groups):
                friday     = monday + timedelta(days=4)
                week_label = f"Week of {monday.strftime('%b %d')} — {friday.strftime('%b %d, %Y')}"
                items      = week_groups[monday]

                st.markdown(f"#### {week_label}")

                # Map day-of-week → items
                day_map = defaultdict(list)
                for item in items:
                    dow = item["date"].weekday()
                    if dow < 5:
                        day_map[dow].append(item)

                cols = st.columns(5)
                for i, (col, day_name) in enumerate(zip(cols, DAYS)):
                    day_date  = monday + timedelta(days=i)
                    is_today  = day_date == today
                    is_past   = day_date < today
                    day_items = day_map[i]

                    # Day header
                    if is_today:
                        hdr_color = "#22d3a0"
                        hdr_weight = "700"
                        hdr_border = "2px solid #22d3a0"
                    elif is_past:
                        hdr_color = "#475569"
                        hdr_weight = "400"
                        hdr_border = "1px solid rgba(255,255,255,0.05)"
                    else:
                        hdr_color = "#94a3b8"
                        hdr_weight = "400"
                        hdr_border = "1px solid rgba(255,255,255,0.08)"

                    col.markdown(
                        f'<div style="font-family:IBM Plex Mono,monospace;font-size:11px;'
                        f'color:{hdr_color};font-weight:{hdr_weight};padding:5px 8px;'
                        f'border-bottom:{hdr_border};margin-bottom:6px">'
                        f'{day_name} · {day_date.strftime("%b %d")}'
                        f'{"  ← today" if is_today else ""}</div>',
                        unsafe_allow_html=True,
                    )

                    if not day_items:
                        col.markdown(
                            '<div style="color:#334155;font-size:11px;'
                            'font-family:IBM Plex Mono,monospace;padding:4px 8px">—</div>',
                            unsafe_allow_html=True,
                        )
                    else:
                        for item in day_items:
                            in_basket  = item["ticker"] in basket_tickers
                            bg         = "rgba(34,211,160,0.10)" if in_basket else "rgba(255,255,255,0.03)"
                            tkr_color  = "#22d3a0"               if in_basket else "#94a3b8"
                            border_col = "rgba(34,211,160,0.35)" if in_basket else "rgba(255,255,255,0.07)"
                            col.markdown(
                                f'<div style="background:{bg};border:1px solid {border_col};'
                                f'border-radius:5px;padding:5px 8px;margin-bottom:5px">'
                                f'<div style="font-family:IBM Plex Mono,monospace;font-size:12px;'
                                f'font-weight:600;color:{tkr_color}">{item["ticker"]}'
                                f'{"  ★" if in_basket else ""}</div>'
                                f'<div style="font-size:10px;color:#64748b;margin-top:2px">'
                                f'EPS: {item["eps_range"]}</div>'
                                f'<div style="font-size:10px;color:#475569">'
                                f'Rev: {item["rev_range"]}</div>'
                                f'</div>',
                                unsafe_allow_html=True,
                            )

                st.markdown("<br>", unsafe_allow_html=True)

            # Full sortable table
            with st.expander("View as full table", expanded=False):
                rows = []
                for item in cal_data:
                    rows.append({
                        "Date":       item["date"].strftime("%Y-%m-%d"),
                        "Day":        item["date"].strftime("%A"),
                        "Ticker":     item["ticker"],
                        "In Basket":  "★" if item["ticker"] in basket_tickers else "",
                        "EPS Est":    item["eps_range"],
                        "Revenue Est":item["rev_range"],
                        "Days Away":  (item["date"] - today).days,
                    })
                st.dataframe(
                    pd.DataFrame(rows).sort_values("Date"),
                    use_container_width=True,
                    hide_index=True,
                )

    # ─────────────────────────────────────────────────────────────────────────
    # TAB 2 — HISTORICAL RESULTS
    # ─────────────────────────────────────────────────────────────────────────
    with tab_hist:
        st.subheader("Historical Earnings Results")

        session   = get_session()
        companies = get_basket(session)

        if not companies:
            st.warning("No companies in basket.")
            session.close()
        else:
            # Company selector
            ticker_options = ["All companies"] + [
                f"{c.ticker} — {c.name}" for c in companies
            ]
            selected = st.selectbox("Company", ticker_options, key="earn_hist_select")
            selected_ticker = None if selected == "All companies" else selected.split(" — ")[0]

            # Pull historical earnings from DB
            q = (
                select(EarningsEvent)
                .order_by(desc(EarningsEvent.report_date))
                .limit(100)
            )
            if selected_ticker:
                q = q.where(EarningsEvent.ticker == selected_ticker)
            else:
                basket_tickers = [c.ticker for c in companies]
                q = q.where(EarningsEvent.ticker.in_(basket_tickers))

            historical = session.execute(q).scalars().all()
            session.close()

            if not historical:
                st.info(
                    "No historical earnings data in the database yet. "
                    "Run the scraper (`python3 -m mktscan run --mode earnings`) to populate."
                )
            else:
                # ── KPI summary row ───────────────────────────────────────────
                beats  = sum(1 for e in historical if e.surprise_pct and e.surprise_pct > 0)
                misses = sum(1 for e in historical if e.surprise_pct and e.surprise_pct < 0)
                total  = len([e for e in historical if e.surprise_pct is not None])
                avg_surprise = (
                    sum(e.surprise_pct for e in historical if e.surprise_pct) /
                    len([e for e in historical if e.surprise_pct])
                    if any(e.surprise_pct for e in historical) else 0
                )

                k1, k2, k3, k4 = st.columns(4)
                k1.metric("Reports", len(historical))
                k2.metric("EPS Beats", beats,  help="Reported EPS > estimate")
                k3.metric("EPS Misses", misses, help="Reported EPS < estimate")
                k4.metric("Avg Surprise", f"{avg_surprise:+.1f}%" if total else "—")

                st.divider()

                # ── EPS surprise bar chart ────────────────────────────────────
                chart_data = [
                    {
                        "label":    f"{e.ticker} {e.period or ''}".strip(),
                        "surprise": e.surprise_pct,
                        "ticker":   e.ticker,
                    }
                    for e in historical if e.surprise_pct is not None
                ]

                if chart_data:
                    st.markdown("**EPS Surprise % by report**")
                    df_chart = pd.DataFrame(chart_data)
                    fig = go.Figure(go.Bar(
                        x=df_chart["label"],
                        y=df_chart["surprise"],
                        marker_color=[
                            "#22d3a0" if s >= 0 else "#f87171"
                            for s in df_chart["surprise"]
                        ],
                        text=[f"{s:+.1f}%" for s in df_chart["surprise"]],
                        textposition="outside",
                        hovertemplate="<b>%{x}</b><br>Surprise: %{y:+.2f}%<extra></extra>",
                    ))
                    fig.add_hline(y=0, line_color="rgba(255,255,255,0.25)", line_width=1)
                    fig.update_layout(
                        height=300,
                        margin=dict(l=10, r=10, t=20, b=90),
                        paper_bgcolor="rgba(0,0,0,0)",
                        plot_bgcolor="rgba(0,0,0,0)",
                        font=dict(family="IBM Plex Mono", color="#94a3b8"),
                        xaxis=dict(tickangle=-40, gridcolor="rgba(255,255,255,0.04)"),
                        yaxis=dict(
                            gridcolor="rgba(255,255,255,0.04)",
                            ticksuffix="%",
                            zeroline=False,
                        ),
                        showlegend=False,
                    )
                    st.plotly_chart(fig, use_container_width=True)

                # ── EPS estimate vs actual line chart (single ticker only) ────
                if selected_ticker and len(historical) >= 2:
                    eps_data = [
                        {
                            "Period":   e.period or str(e.report_date)[:7],
                            "Estimate": e.eps_estimate,
                            "Actual":   e.eps_actual,
                        }
                        for e in reversed(historical)
                        if e.eps_estimate is not None or e.eps_actual is not None
                    ]
                    if eps_data:
                        st.markdown(f"**EPS estimate vs actual — {selected_ticker}**")
                        df_eps = pd.DataFrame(eps_data)
                        fig2   = go.Figure()
                        if "Estimate" in df_eps and df_eps["Estimate"].notna().any():
                            fig2.add_trace(go.Scatter(
                                x=df_eps["Period"], y=df_eps["Estimate"],
                                name="Estimate",
                                mode="lines+markers",
                                line=dict(color="#60a5fa", width=2, dash="dot"),
                                marker=dict(size=7),
                            ))
                        if "Actual" in df_eps and df_eps["Actual"].notna().any():
                            fig2.add_trace(go.Scatter(
                                x=df_eps["Period"], y=df_eps["Actual"],
                                name="Actual",
                                mode="lines+markers",
                                line=dict(color="#22d3a0", width=2),
                                marker=dict(size=7),
                            ))
                        fig2.update_layout(
                            height=260,
                            margin=dict(l=10, r=10, t=10, b=40),
                            paper_bgcolor="rgba(0,0,0,0)",
                            plot_bgcolor="rgba(0,0,0,0)",
                            font=dict(family="IBM Plex Mono", color="#94a3b8"),
                            xaxis=dict(tickangle=-30, gridcolor="rgba(255,255,255,0.04)"),
                            yaxis=dict(gridcolor="rgba(255,255,255,0.04)", tickprefix="$"),
                            legend=dict(
                                orientation="h", yanchor="bottom",
                                y=1.02, xanchor="right", x=1,
                            ),
                        )
                        st.plotly_chart(fig2, use_container_width=True)

                # ── Detail table ──────────────────────────────────────────────
                st.markdown("**All reports**")
                table_rows = []
                for e in historical:
                    surprise_str = f"{e.surprise_pct:+.1f}%" if e.surprise_pct is not None else "—"
                    table_rows.append({
                        "Ticker":      e.ticker,
                        "Period":      e.period or "—",
                        "Report Date": str(e.report_date)[:10] if e.report_date else "—",
                        "EPS Est":     f"${e.eps_estimate:.2f}" if e.eps_estimate is not None else "—",
                        "EPS Actual":  f"${e.eps_actual:.2f}"   if e.eps_actual   is not None else "—",
                        "Surprise":    surprise_str,
                        "Rev Est":     f"${e.revenue_estimate/1e9:.2f}B" if e.revenue_estimate else "—",
                        "Rev Actual":  f"${e.revenue_actual/1e9:.2f}B"   if e.revenue_actual   else "—",
                    })

                st.dataframe(
                    pd.DataFrame(table_rows),
                    use_container_width=True,
                    hide_index=True,
                )


# ═══════════════════════════════════════════════════════════════════════════════
# ECONOMIC CALENDAR PAGE
# ═══════════════════════════════════════════════════════════════════════════════
elif page == "Economic Calendar":
    st.title("Economic Calendar")
    st.caption("Major macro events for the current month — FOMC, CPI, PCE, jobs, GDP and more. Sourced live from MarketWatch. Cached for 2 hours.")

    # ── Category colours & importance icons ──────────────────────────────────
    CAT_COLORS = {
        "Fed / Rates":   ("#4B8BFF", "rgba(75,139,255,0.12)"),
        "Inflation":     ("#f87171", "rgba(248,113,113,0.12)"),
        "Labour":        ("#22d3a0", "rgba(34,211,160,0.12)"),
        "GDP":           ("#a78bfa", "rgba(167,139,250,0.12)"),
        "Consumer":      ("#fbbf24", "rgba(251,191,36,0.10)"),
        "Manufacturing": ("#60a5fa", "rgba(96,165,250,0.10)"),
        "Housing":       ("#f59e0b", "rgba(245,158,11,0.10)"),
        "Trade":         ("#34d399", "rgba(52,211,153,0.10)"),
        "Treasury":      ("#94a3b8", "rgba(148,163,184,0.10)"),
        "Other":         ("#64748b", "rgba(100,116,139,0.08)"),
    }
    IMP_ICONS = {"High": "🔴", "Medium": "🟡", "Low": "⚪"}

    def _ref_month_name(year: int, month: int, months_back: int) -> str:
        """
        Name of the month ``months_back`` before (year, month).

        Replaces ``f"Month {month-2 or 11}"``, which was wrong twice over: it
        rendered a bare integer instead of a month name, and the ``or 11``
        fallback only worked for February by accident. In January it evaluated
        to -1.
        """
        total = year * 12 + (month - 1) - months_back
        return f"{calendar.month_abbr[total % 12 + 1]} {total // 12}"

    @st.cache_data(ttl=7200)
    def fetch_econ_calendar():
        """
        Fetch economic calendar from MarketWatch.
        Falls back to a curated static schedule of known recurring events
        if the live scrape fails or returns too few events.
        """
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from mktscan.scrapers.marketwatch import MarketWatchScraper

        mw = MarketWatchScraper({"enabled": True}, delay=2.0)
        try:
            live = mw.fetch_economic_calendar()
            if len(live) >= 5:
                return live, "marketwatch"
        except Exception as e:
            pass

        # ── Static fallback calendar ─────────────────────────────────────────
        # Pre-populated with the most important recurring US economic releases.
        # Dates are approximate; the live scrape will override these when available.
        from datetime import date
        today  = datetime.now().date()
        year   = today.year
        month  = today.month

        def d(day):
            try:
                return date(year, month, day)
            except ValueError:
                return date(year, month, 28)

        STATIC = [
            # Fed / Rates
            {"name": "FOMC Rate Decision",               "category": "Fed / Rates",   "importance": "High",   "day_offset": 10, "time_str": "2:00 PM ET",  "period": f"Month {month}", "consensus": "—", "prior": "—"},
            {"name": "Fed Chair Press Conference",       "category": "Fed / Rates",   "importance": "High",   "day_offset": 10, "time_str": "2:30 PM ET",  "period": f"Month {month}", "consensus": "—", "prior": "—"},
            {"name": "FOMC Meeting Minutes",             "category": "Fed / Rates",   "importance": "High",   "day_offset": 18, "time_str": "2:00 PM ET",  "period": "Prior meeting",  "consensus": "—", "prior": "—"},
            {"name": "Fed Beige Book",                   "category": "Fed / Rates",   "importance": "Medium", "day_offset": 14, "time_str": "2:00 PM ET",  "period": f"Month {month}", "consensus": "—", "prior": "—"},
            # Inflation
            {"name": "CPI — Consumer Price Index",       "category": "Inflation",     "importance": "High",   "day_offset": 11, "time_str": "8:30 AM ET",  "period": f"Month {month-1 or 12}", "consensus": "—", "prior": "—"},
            {"name": "Core CPI (ex Food & Energy)",      "category": "Inflation",     "importance": "High",   "day_offset": 11, "time_str": "8:30 AM ET",  "period": f"Month {month-1 or 12}", "consensus": "—", "prior": "—"},
            {"name": "PCE Price Index",                  "category": "Inflation",     "importance": "High",   "day_offset": 26, "time_str": "8:30 AM ET",  "period": f"Month {month-1 or 12}", "consensus": "—", "prior": "—"},
            {"name": "Core PCE Price Index",             "category": "Inflation",     "importance": "High",   "day_offset": 26, "time_str": "8:30 AM ET",  "period": f"Month {month-1 or 12}", "consensus": "—", "prior": "—"},
            {"name": "PPI — Producer Price Index",       "category": "Inflation",     "importance": "Medium", "day_offset": 13, "time_str": "8:30 AM ET",  "period": f"Month {month-1 or 12}", "consensus": "—", "prior": "—"},
            # Labour
            {"name": "Nonfarm Payrolls (NFP)",           "category": "Labour",        "importance": "High",   "day_offset":  4, "time_str": "8:30 AM ET",  "period": f"Month {month-1 or 12}", "consensus": "—", "prior": "—"},
            {"name": "Unemployment Rate",                "category": "Labour",        "importance": "High",   "day_offset":  4, "time_str": "8:30 AM ET",  "period": f"Month {month-1 or 12}", "consensus": "—", "prior": "—"},
            {"name": "Average Hourly Earnings",          "category": "Labour",        "importance": "High",   "day_offset":  4, "time_str": "8:30 AM ET",  "period": f"Month {month-1 or 12}", "consensus": "—", "prior": "—"},
            {"name": "Initial Jobless Claims",           "category": "Labour",        "importance": "Medium", "day_offset":  3, "time_str": "8:30 AM ET",  "period": "Weekly",         "consensus": "—", "prior": "—"},
            {"name": "Initial Jobless Claims",           "category": "Labour",        "importance": "Medium", "day_offset": 10, "time_str": "8:30 AM ET",  "period": "Weekly",         "consensus": "—", "prior": "—"},
            {"name": "Initial Jobless Claims",           "category": "Labour",        "importance": "Medium", "day_offset": 17, "time_str": "8:30 AM ET",  "period": "Weekly",         "consensus": "—", "prior": "—"},
            {"name": "Initial Jobless Claims",           "category": "Labour",        "importance": "Medium", "day_offset": 24, "time_str": "8:30 AM ET",  "period": "Weekly",         "consensus": "—", "prior": "—"},
            {"name": "ADP Employment Report",            "category": "Labour",        "importance": "Medium", "day_offset":  3, "time_str": "8:15 AM ET",  "period": f"Month {month-1 or 12}", "consensus": "—", "prior": "—"},
            {"name": "JOLTS Job Openings",               "category": "Labour",        "importance": "Medium", "day_offset":  8, "time_str": "10:00 AM ET", "period": f"{_ref_month_name(year, month, 2)}", "consensus": "—", "prior": "—"},
            # GDP
            {"name": "GDP — Advance Estimate",          "category": "GDP",           "importance": "High",   "day_offset": 25, "time_str": "8:30 AM ET",  "period": "Q1",             "consensus": "—", "prior": "—"},
            {"name": "GDP — Second Estimate",            "category": "GDP",           "importance": "High",   "day_offset": 28, "time_str": "8:30 AM ET",  "period": "Q4 Prior",       "consensus": "—", "prior": "—"},
            # Consumer
            {"name": "Retail Sales",                     "category": "Consumer",      "importance": "High",   "day_offset": 15, "time_str": "8:30 AM ET",  "period": f"Month {month-1 or 12}", "consensus": "—", "prior": "—"},
            {"name": "Consumer Confidence (Conference Board)", "category": "Consumer", "importance": "Medium", "day_offset": 28, "time_str": "10:00 AM ET", "period": f"Month {month}", "consensus": "—", "prior": "—"},
            {"name": "U. of Michigan Consumer Sentiment","category": "Consumer",      "importance": "Medium", "day_offset": 10, "time_str": "10:00 AM ET", "period": f"Month {month} Prelim", "consensus": "—", "prior": "—"},
            {"name": "Personal Spending",                "category": "Consumer",      "importance": "Medium", "day_offset": 26, "time_str": "8:30 AM ET",  "period": f"Month {month-1 or 12}", "consensus": "—", "prior": "—"},
            # Manufacturing / Services
            {"name": "ISM Manufacturing PMI",            "category": "Manufacturing", "importance": "Medium", "day_offset":  1, "time_str": "10:00 AM ET", "period": f"Month {month-1 or 12}", "consensus": "—", "prior": "—"},
            {"name": "ISM Services PMI",                 "category": "Manufacturing", "importance": "Medium", "day_offset":  5, "time_str": "10:00 AM ET", "period": f"Month {month-1 or 12}", "consensus": "—", "prior": "—"},
            {"name": "S&P Global Manufacturing PMI",     "category": "Manufacturing", "importance": "Low",    "day_offset": 22, "time_str": "9:45 AM ET",  "period": f"Month {month} Flash", "consensus": "—", "prior": "—"},
            {"name": "Durable Goods Orders",             "category": "Manufacturing", "importance": "Medium", "day_offset": 24, "time_str": "8:30 AM ET",  "period": f"Month {month-1 or 12}", "consensus": "—", "prior": "—"},
            # Housing
            {"name": "Existing Home Sales",              "category": "Housing",       "importance": "Medium", "day_offset": 21, "time_str": "10:00 AM ET", "period": f"Month {month-1 or 12}", "consensus": "—", "prior": "—"},
            {"name": "New Home Sales",                   "category": "Housing",       "importance": "Medium", "day_offset": 23, "time_str": "10:00 AM ET", "period": f"Month {month-1 or 12}", "consensus": "—", "prior": "—"},
            {"name": "Housing Starts & Building Permits","category": "Housing",       "importance": "Medium", "day_offset": 16, "time_str": "8:30 AM ET",  "period": f"Month {month-1 or 12}", "consensus": "—", "prior": "—"},
            # Trade
            {"name": "Trade Balance",                    "category": "Trade",         "importance": "Medium", "day_offset":  7, "time_str": "8:30 AM ET",  "period": f"{_ref_month_name(year, month, 2)}", "consensus": "—", "prior": "—"},
            # Treasury
            {"name": "10-Year Treasury Note Auction",    "category": "Treasury",      "importance": "Medium", "day_offset":  9, "time_str": "1:00 PM ET",  "period": f"Month {month}", "consensus": "—", "prior": "—"},
            {"name": "2-Year Treasury Note Auction",     "category": "Treasury",      "importance": "Low",    "day_offset": 22, "time_str": "1:00 PM ET",  "period": f"Month {month}", "consensus": "—", "prior": "—"},
        ]

        from datetime import date as date_type
        results = []
        for ev in STATIC:
            offset = ev.pop("day_offset")
            try:
                ev_date = date_type(year, month, min(offset, 28))
            except ValueError:
                ev_date = date_type(year, month, 28)
            ev["date"]     = ev_date
            ev["datetime"] = None
            ev["actual"]   = ""
            ev["source"]   = "static_fallback"
            results.append(ev)

        results.sort(key=lambda x: x["date"])
        return results, "static_fallback"

    with st.spinner("Loading economic calendar…"):
        econ_events, data_source = fetch_econ_calendar()

    if data_source == "static_fallback":
        st.info(
            "⚠️  Live MarketWatch data unavailable — showing a curated schedule of "
            "recurring US economic releases. Dates are approximate. "
            "Exact dates are confirmed closer to each release."
        )
    else:
        st.success(f"Live data from MarketWatch — {len(econ_events)} events loaded")

    if not econ_events:
        st.warning("No events to display.")
        st.stop()

    # ── Filter controls ───────────────────────────────────────────────────────
    today = datetime.now().date()

    all_cats = sorted({e["category"] for e in econ_events})
    col_f1, col_f2, col_f3 = st.columns([2, 2, 1])
    with col_f1:
        selected_cats = st.multiselect(
            "Filter by category",
            all_cats,
            default=all_cats,
            key="econ_cat_filter",
        )
    with col_f2:
        selected_imp = st.multiselect(
            "Importance",
            ["High", "Medium", "Low"],
            default=["High", "Medium"],
            key="econ_imp_filter",
        )
    with col_f3:
        show_past = st.checkbox("Show past events", value=True)

    filtered = [
        e for e in econ_events
        if e["category"] in selected_cats
        and e["importance"] in selected_imp
        and (show_past or e["date"] >= today)
    ]

    if not filtered:
        st.info("No events match your filters.")
        st.stop()

    # ── KPI summary strip ─────────────────────────────────────────────────────
    high_count   = sum(1 for e in filtered if e["importance"] == "High")
    remaining    = sum(1 for e in filtered if e["date"] >= today)
    next_high    = next((e for e in sorted(filtered, key=lambda x: x["date"])
                         if e["importance"] == "High" and e["date"] >= today), None)

    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Total Events",    len(filtered))
    k2.metric("High Importance", high_count)
    k3.metric("Upcoming",        remaining)
    k4.metric(
        "Next Major Event",
        next_high["name"][:22] + "…" if next_high and len(next_high["name"]) > 22
        else (next_high["name"] if next_high else "—"),
        delta=next_high["date"].strftime("%b %d") if next_high else None,
        delta_color="off",
    )

    st.divider()

    # ── Month view tab + List view tab ───────────────────────────────────────
    view_cal, view_list = st.tabs(["🗓  Month View", "📋  List View"])

    with view_cal:
        # Build full calendar grid for current month
        import calendar as cal_mod

        # Determine which month to show
        col_nav1, col_nav2, col_nav3 = st.columns([1, 3, 1])
        with col_nav1:
            if st.button("◀ Prev month"):
                if "econ_month_offset" not in st.session_state:
                    st.session_state.econ_month_offset = 0
                st.session_state.econ_month_offset -= 1
        with col_nav3:
            if st.button("Next month ▶"):
                if "econ_month_offset" not in st.session_state:
                    st.session_state.econ_month_offset = 0
                st.session_state.econ_month_offset += 1

        offset = st.session_state.get("econ_month_offset", 0)
        # Calendar-correct month arithmetic. `today.replace(day=1) + timedelta(days=32*offset)`
        # drifts because months are not 32 days long: it accumulated ~19 days of
        # error per year of navigation and skipped a month entirely after about
        # ten clicks in one direction.
        _total_months = (today.year * 12 + today.month - 1) + offset
        view_date = date(_total_months // 12, _total_months % 12 + 1, 1)
        view_month = view_date.month
        view_year  = view_date.year
        with col_nav2:
            st.markdown(
                f'<h3 style="text-align:center;font-family:IBM Plex Mono,monospace;'
                f'color:#e2e8f0;margin:0">'
                f'{view_date.strftime("%B %Y")}</h3>',
                unsafe_allow_html=True,
            )

        # Map date → events for this month
        from collections import defaultdict
        day_events: dict = defaultdict(list)
        for ev in filtered:
            if ev["date"].year == view_year and ev["date"].month == view_month:
                day_events[ev["date"].day].append(ev)

        # Calendar grid
        first_dow, days_in_month = cal_mod.monthrange(view_year, view_month)
        DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

        # Header row
        hdr_cols = st.columns(7)
        for col, dn in zip(hdr_cols, DAY_NAMES):
            is_weekend = dn in ("Sat", "Sun")
            col.markdown(
                f'<div style="text-align:center;font-family:IBM Plex Mono,monospace;'
                f'font-size:11px;font-weight:600;color:{"#334155" if is_weekend else "#64748b"};'
                f'padding:4px 0;border-bottom:1px solid rgba(255,255,255,0.07)">{dn}</div>',
                unsafe_allow_html=True,
            )

        # Build list of day slots (None = padding)
        slots = [None] * first_dow + list(range(1, days_in_month + 1))
        # Pad to complete last row
        while len(slots) % 7 != 0:
            slots.append(None)

        # Render weeks as rows
        for week_start in range(0, len(slots), 7):
            week_slots = slots[week_start:week_start + 7]
            row_cols   = st.columns(7)

            for col, day_num in zip(row_cols, week_slots):
                if day_num is None:
                    col.markdown(
                        '<div style="min-height:80px;border:1px solid rgba(255,255,255,0.04);'
                        'border-radius:4px;margin:1px"></div>',
                        unsafe_allow_html=True,
                    )
                    continue

                day_date   = today.replace(year=view_year, month=view_month, day=day_num)
                is_today   = day_date == today
                is_weekend = day_date.weekday() >= 5
                is_past    = day_date < today
                day_evs    = day_events.get(day_num, [])

                # Sort by importance
                imp_order  = {"High": 0, "Medium": 1, "Low": 2}
                day_evs    = sorted(day_evs, key=lambda x: imp_order.get(x["importance"], 3))

                # Cell background
                if is_today:
                    cell_bg     = "rgba(34,211,160,0.08)"
                    day_border  = "1px solid rgba(34,211,160,0.4)"
                    num_color   = "#22d3a0"
                    num_weight  = "700"
                elif is_weekend:
                    cell_bg     = "rgba(0,0,0,0)"
                    day_border  = "1px solid rgba(255,255,255,0.04)"
                    num_color   = "#334155"
                    num_weight  = "400"
                elif is_past:
                    cell_bg     = "rgba(0,0,0,0)"
                    day_border  = "1px solid rgba(255,255,255,0.05)"
                    num_color   = "#475569"
                    num_weight  = "400"
                else:
                    cell_bg     = "rgba(255,255,255,0.02)"
                    day_border  = "1px solid rgba(255,255,255,0.07)"
                    num_color   = "#94a3b8"
                    num_weight  = "400"

                # Build event pills HTML
                event_html = ""
                for ev in day_evs[:4]:  # max 4 pills per cell
                    cat_col, cat_bg = CAT_COLORS.get(ev["category"], ("#94a3b8", "rgba(148,163,184,0.1)"))
                    imp_dot = (
                        f'<span style="display:inline-block;width:5px;height:5px;'
                        f'border-radius:50%;background:{"#f87171" if ev["importance"]=="High" else "#fbbf24" if ev["importance"]=="Medium" else "#475569"};'
                        f'margin-right:3px;vertical-align:middle"></span>'
                    )
                    short_name = ev["name"][:22] + "…" if len(ev["name"]) > 22 else ev["name"]
                    time_label = f'<div style="font-size:8px;color:#475569;margin-top:1px">{ev.get("time_str","")}</div>' if ev.get("time_str") else ""
                    event_html += (
                        f'<div style="background:{cat_bg};border:1px solid {cat_col}33;'
                        f'border-radius:3px;padding:2px 5px;margin-bottom:3px;'
                        f'{"opacity:0.5;" if is_past else ""}">'
                        f'<div style="font-size:9px;color:{cat_col};line-height:1.3;font-family:IBM Plex Mono,monospace">'
                        f'{imp_dot}{short_name}</div>'
                        f'{time_label}'
                        f'</div>'
                    )

                if len(day_events.get(day_num, [])) > 4:
                    extra = len(day_events[day_num]) - 4
                    event_html += f'<div style="font-size:9px;color:#475569;padding:1px 4px">+{extra} more</div>'

                col.markdown(
                    f'<div style="background:{cell_bg};border:{day_border};border-radius:6px;'
                    f'padding:6px;min-height:80px;margin:1px">'
                    f'<div style="font-family:IBM Plex Mono,monospace;font-size:12px;'
                    f'color:{num_color};font-weight:{num_weight};margin-bottom:4px">{day_num}</div>'
                    f'{event_html}'
                    f'</div>',
                    unsafe_allow_html=True,
                )

        # Legend
        st.markdown("<br>", unsafe_allow_html=True)
        st.markdown("**Category legend**")
        leg_cols = st.columns(5)
        for i, (cat, (color, bg)) in enumerate(CAT_COLORS.items()):
            if cat == "Other":
                continue
            leg_cols[i % 5].markdown(
                f'<div style="display:flex;align-items:center;gap:6px;margin-bottom:6px">'
                f'<div style="width:10px;height:10px;border-radius:2px;background:{bg};'
                f'border:1px solid {color};flex-shrink:0"></div>'
                f'<span style="font-size:11px;color:#94a3b8">{cat}</span>'
                f'</div>',
                unsafe_allow_html=True,
            )

    with view_list:
        # Group by date
        from collections import defaultdict as dd2
        by_date = dd2(list)
        for ev in sorted(filtered, key=lambda x: x["date"]):
            by_date[ev["date"]].append(ev)

        for ev_date, evs in by_date.items():
            is_past = ev_date < today
            is_today_row = ev_date == today

            date_label = ev_date.strftime("%A, %B %d, %Y")
            if is_today_row:
                date_label += "  ← today"

            st.markdown(
                f'<div style="font-family:IBM Plex Mono,monospace;font-size:12px;'
                f'font-weight:600;color:{"#22d3a0" if is_today_row else "#475569" if is_past else "#94a3b8"};'
                f'margin:16px 0 6px 0;padding-bottom:4px;'
                f'border-bottom:1px solid rgba(255,255,255,0.07)">{date_label}</div>',
                unsafe_allow_html=True,
            )

            imp_order = {"High": 0, "Medium": 1, "Low": 2}
            for ev in sorted(evs, key=lambda x: imp_order.get(x["importance"], 3)):
                cat_col, cat_bg = CAT_COLORS.get(ev["category"], ("#94a3b8", "rgba(148,163,184,0.1)"))
                imp_icon = IMP_ICONS.get(ev["importance"], "⚪")
                actual_html = ""
                if ev.get("actual") and ev["actual"] not in ("", "—"):
                    actual_html = (
                        f'<span style="font-size:10px;color:#22d3a0;'
                        f'background:rgba(34,211,160,0.1);padding:1px 6px;'
                        f'border-radius:3px;margin-left:8px">Actual: {ev["actual"]}</span>'
                    )

                consensus_html = ""
                if ev.get("consensus") and ev["consensus"] not in ("", "—"):
                    consensus_html = f'<span style="font-size:10px;color:#64748b">Est: {ev["consensus"]}</span>'

                prior_html = ""
                if ev.get("prior") and ev["prior"] not in ("", "—"):
                    prior_html = f'<span style="font-size:10px;color:#475569">Prior: {ev["prior"]}</span>'

                st.markdown(
                    f'<div style="display:flex;align-items:flex-start;gap:12px;'
                    f'padding:8px 10px;margin-bottom:4px;border-radius:6px;'
                    f'background:{cat_bg if not is_past else "rgba(255,255,255,0.02)"};'
                    f'border:1px solid {cat_col}22;'
                    f'{"opacity:0.55;" if is_past else ""}">'
                    f'<div style="min-width:44px;font-family:IBM Plex Mono,monospace;'
                    f'font-size:10px;color:#64748b;padding-top:2px">{ev.get("time_str","")}</div>'
                    f'<div style="flex:1">'
                    f'<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">'
                    f'<span style="font-size:13px;color:#e2e8f0;font-weight:500">'
                    f'{imp_icon} {ev["name"]}</span>'
                    f'<span style="font-size:10px;color:{cat_col};background:{cat_bg};'
                    f'padding:1px 7px;border-radius:3px;border:1px solid {cat_col}44">'
                    f'{ev["category"]}</span>'
                    f'{actual_html}'
                    f'</div>'
                    f'<div style="display:flex;gap:12px;margin-top:4px;flex-wrap:wrap">'
                    f'<span style="font-size:10px;color:#64748b">{ev.get("period","")}</span>'
                    f'{consensus_html}{prior_html}'
                    f'</div>'
                    f'</div>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

# ═══════════════════════════════════════════════════════════════════════════════
# BASKET PAGE
# ═══════════════════════════════════════════════════════════════════════════════
elif page == "Basket":
    st.title("Watch Basket")

    session = get_session()
    companies = get_basket(session)

    # ── Add company form ──
    with st.expander("➕ Add Company", expanded=False):
        c1, c2, c3 = st.columns([1, 2, 2])
        new_ticker  = c1.text_input("Ticker", max_chars=10).upper()
        new_name    = c2.text_input("Company Name")
        new_sector  = c3.text_input("Sector (optional)")
        new_kw      = st.text_input("Search Keywords (comma-separated)",
                                     placeholder="e.g. Apple, iPhone, Tim Cook")
        if st.button("Add to Basket", type="primary"):
            if new_ticker and new_name:
                upsert_company(session, new_ticker, new_name, new_sector, new_kw or new_ticker)
                st.success(f"Added {new_ticker} — {new_name}")
                st.rerun()
            else:
                st.error("Ticker and name are required.")

    # ── Load preset ──
    col1, col2, col3 = st.columns(3)
    PRESETS = {
        "Magnificent 7": [
            ("AAPL","Apple Inc.","Technology","Apple, iPhone, Tim Cook"),
            ("MSFT","Microsoft","Technology","Microsoft, Azure, Copilot"),
            ("NVDA","NVIDIA","Semiconductors","NVIDIA, H100, Blackwell, Jensen Huang"),
            ("GOOGL","Alphabet","Technology","Google, Gemini, YouTube"),
            ("META","Meta Platforms","Social Media","Meta, Facebook, Instagram"),
            ("AMZN","Amazon","E-Commerce","Amazon, AWS, Prime"),
            ("TSLA","Tesla","EV / Auto","Tesla, Elon Musk, EV"),
        ],
        "US Financials": [
            ("JPM","JPMorgan Chase","Finance","JPMorgan, Jamie Dimon"),
            ("GS","Goldman Sachs","Finance","Goldman Sachs"),
            ("BAC","Bank of America","Finance","Bank of America, BofA"),
            ("V","Visa","Payments","Visa, payments"),
            ("BRK-B","Berkshire Hathaway","Finance","Berkshire, Warren Buffett"),
        ],
        "Energy": [
            ("XOM","ExxonMobil","Energy","ExxonMobil, oil"),
            ("CVX","Chevron","Energy","Chevron, oil"),
            ("NEE","NextEra Energy","Utilities","NextEra, wind, solar"),
            ("SLB","Schlumberger","Oil Services","Schlumberger, SLB"),
        ],
    }

    for i, (preset_name, data) in enumerate(PRESETS.items()):
        col = [col1, col2, col3][i % 3]
        if col.button(f"Load {preset_name}"):
            for ticker, name, sector, kw in data:
                upsert_company(session, ticker, name, sector, kw)
            st.success(f"Loaded {preset_name} preset ({len(data)} companies)")
            st.rerun()

    st.divider()

    # ── Company table with edit/remove ──
    if not companies:
        st.info("Basket is empty. Add companies above.")
    else:
        st.subheader(f"Current Basket ({len(companies)} companies)")
        for c in companies:
            with st.container():
                cols = st.columns([1, 2, 2, 3, 1])
                cols[0].markdown(f"**{c.ticker}**")
                cols[1].text(c.name)
                cols[2].text(c.sector or "—")
                cols[3].text(c.keywords or "—")
                if cols[4].button("✕", key=f"rm_{c.ticker}"):
                    c.active = False
                    session.commit()
                    st.rerun()

    session.close()


# ═══════════════════════════════════════════════════════════════════════════════
# BACKTEST PAGE
# ═══════════════════════════════════════════════════════════════════════════════
elif page == "Backtest":
    st.title("Score Accuracy Backtest")
    st.caption(
        "Measures whether the tradeability score correctly predicted the direction "
        "of price returns over 5, 10, 21, and 63 trading days. "
        "Uses price momentum, technical, and volume signals — the three signals "
        "that can be reconstructed from historical OHLCV data. "
        "Runs incrementally: the first run pulls 5 years of history; "
        "subsequent runs only fetch new data since the last update."
    )

    from mktscan.backtest_incremental import (
        run_incremental_backtest, get_summary, get_total_observations,
        BacktestObservation,
    )
    from sqlalchemy import select as _select

    session_bt = get_session()
    total_obs  = get_total_observations(session_bt)
    summary    = get_summary(session_bt)
    session_bt.close()

    # ── Status + run button ───────────────────────────────────────────────────
    status_col, btn_col = st.columns([3, 1])

    with status_col:
        if total_obs == 0:
            st.info(
                "No backtest data yet. Click **Run Backtest** to pull 5 years of "
                "historical data and compute signal accuracy. Takes 2–5 minutes on first run."
            )
        else:
            last_updated = summary[0]["updated_at"] if summary else None
            last_str = last_updated.strftime("%b %d, %Y at %H:%M UTC") if last_updated else "—"
            st.success(
                f"**{total_obs:,} observations** stored across your basket.  "
                f"Last updated: {last_str}.  "
                f"Updates automatically every Sunday at 02:00 UTC."
            )

    with btn_col:
        st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)
        run_bt = st.button(
            "▶ Run Backtest",
            type="primary",
            use_container_width=True,
            key="run_backtest_btn",
        )

    if run_bt:
        from mktscan.backtest_incremental import run_incremental_backtest
        bt_log   = st.empty()
        bt_lines: list[str] = []

        def _bt_cb(level: str, msg: str):
            bt_lines.append(f"· {msg}")
            bt_log.markdown("\n\n".join(bt_lines[-20:]))

        session_bt2 = get_session()
        basket_bt   = get_basket(session_bt2)
        tickers_bt  = [c.ticker for c in basket_bt]

        with st.spinner("Running backtest…"):
            try:
                bt_result = run_incremental_backtest(
                    session=session_bt2,
                    tickers=tickers_bt,
                    progress_cb=_bt_cb,
                )
                st.success(
                    f"✅ Backtest complete — "
                    f"{bt_result['new_observations']} new observations, "
                    f"{bt_result['tickers_processed']} tickers processed"
                )
                session_bt2.close()
                st.rerun()
            except Exception as bt_err:
                st.error(f"Backtest failed: {bt_err}")
                session_bt2.close()

    if not summary:
        st.stop()

    st.divider()

    # ── Summary tables by holding period ─────────────────────────────────────
    LABEL_ORDER  = ["STRONG_BUY", "BULLISH", "NEUTRAL", "BEARISH", "STRONG_SELL"]
    LABEL_COLORS = {
        "STRONG_BUY":  "#22d3a0",
        "BULLISH":     "#86efac",
        "NEUTRAL":     "#fbbf24",
        "BEARISH":     "#fca5a5",
        "STRONG_SELL": "#f87171",
    }
    HOLDING_LABELS = {5: "5-day", 10: "10-day", 21: "1-month", 63: "1-quarter"}

    holding_periods = sorted({r["holding_days"] for r in summary})
    tabs = st.tabs([HOLDING_LABELS.get(hp, f"{hp}d") for hp in holding_periods])

    for tab, hp in zip(tabs, holding_periods):
        with tab:
            hp_rows = [r for r in summary if r["holding_days"] == hp]
            hp_map  = {r["label"]: r for r in hp_rows}

            # ── KPI strip ─────────────────────────────────────────────────
            sb_data  = hp_map.get("STRONG_BUY", {})
            bull_data = hp_map.get("BULLISH", {})
            bear_data = hp_map.get("BEARISH", {})

            k1, k2, k3, k4, k5 = st.columns(5)
            k1.metric(
                "Strong Buy win rate",
                f"{sb_data.get('win_rate_pct', 0):.1f}%" if sb_data else "—",
                delta=(f"{sb_data['win_rate_pct'] - sb_data['benchmark_win_rate_pct']:+.1f} vs benchmark"
                       if sb_data.get("benchmark_win_rate_pct") is not None else None),
                help="% of STRONG_BUY signals followed by a positive return.",
            )
            k2.metric(
                "Strong Buy avg return",
                f"{sb_data.get('avg_return_pct', 0):+.2f}%" if sb_data else "—",
            )
            # The number that actually matters. A 55% win rate is not an edge if
            # the universe rose on 55% of days regardless — the previous version
            # reported the raw rate with nothing to compare it against.
            k3.metric(
                "Excess vs buy-and-hold",
                f"{sb_data.get('excess_return_pct', 0):+.2f}%" if sb_data.get("excess_return_pct") is not None else "—",
                help="Average return on STRONG_BUY signals minus the unconditional "
                     "average across all observations. This is the only figure here "
                     "that says whether the signal added anything.",
            )
            k4.metric(
                "Option P&L (21d spread)",
                f"{sb_data.get('option_avg_pnl_pct', 0):+.1f}%" if sb_data.get("option_avg_pnl_pct") is not None else "—",
                help="Simulated return on capital at risk for the debit spread the "
                     "strategy layer would have selected, net of an assumed 4% "
                     "round-trip spread cost. Uses realised vol as the IV estimate.",
            )
            k5.metric(
                "Total observations",
                f"{sum(r['n_observations'] for r in hp_rows):,}",
            )

            st.caption(
                "Reconstructed from price history using the production signal "
                "functions. Categories that need live data (news sentiment, analyst "
                "targets, short interest) cannot be rebuilt historically, so each "
                "observation records the fraction of model weight that was actually "
                "available — see the coverage column."
            )

            st.divider()

            # ── Bar chart: avg return by label ────────────────────────────
            chart_labels  = [l for l in LABEL_ORDER if l in hp_map]
            chart_returns = [hp_map[l]["avg_return_pct"] for l in chart_labels]
            chart_colors  = [LABEL_COLORS[l] for l in chart_labels]

            fig_bt = go.Figure(go.Bar(
                x=chart_labels,
                y=chart_returns,
                marker_color=chart_colors,
                text=[f"{r:+.2f}%" for r in chart_returns],
                textposition="outside",
                hovertemplate="<b>%{x}</b><br>Avg return: %{y:+.2f}%<extra></extra>",
            ))
            fig_bt.add_hline(y=0, line_color="rgba(255,255,255,0.2)", line_width=1)
            fig_bt.update_layout(
                height=280,
                margin=dict(l=10, r=10, t=30, b=10),
                title=dict(
                    text=f"Average {HOLDING_LABELS.get(hp, f'{hp}d')} return by score label",
                    font=dict(size=12, family="IBM Plex Mono", color="#94a3b8"),
                ),
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)",
                font=dict(family="IBM Plex Mono", color="#94a3b8"),
                xaxis=dict(gridcolor="rgba(255,255,255,0.05)"),
                yaxis=dict(
                    gridcolor="rgba(255,255,255,0.05)",
                    ticksuffix="%",
                    zeroline=False,
                ),
                showlegend=False,
            )
            st.plotly_chart(fig_bt, use_container_width=True)

            # ── Detail table ──────────────────────────────────────────────
            st.markdown("**Full breakdown**")
            table_rows = []
            for label in LABEL_ORDER:
                r = hp_map.get(label)
                if not r:
                    continue
                wr = r["win_rate_pct"]
                ar = r["avg_return_pct"]
                sh = r["sharpe"]
                excess = r.get("excess_return_pct")
                n = r["n_observations"]

                # Edge is judged against the benchmark, not against zero, and it
                # requires a sample large enough to mean anything. The previous
                # rule ("win rate > 52%") called a signal an edge in any rising
                # market, on any sample size.
                if excess is None or n < 100:
                    edge = "— Insufficient data"
                elif label in ("STRONG_BUY", "BULLISH"):
                    edge = ("✅ Edge" if excess > 0.5 else
                            "⚠️ Marginal" if excess > 0 else "❌ No edge")
                elif label in ("BEARISH", "STRONG_SELL"):
                    edge = ("✅ Edge" if excess < -0.5 else
                            "⚠️ Marginal" if excess < 0 else "❌ No edge")
                else:
                    edge = "— Neutral"

                table_rows.append({
                    "Label":        label,
                    "Obs":          n,
                    "Avg Return":   f"{ar:+.2f}%",
                    "Benchmark":    (f"{r['benchmark_avg_return_pct']:+.2f}%"
                                     if r.get("benchmark_avg_return_pct") is not None else "—"),
                    "Excess":       f"{excess:+.2f}%" if excess is not None else "—",
                    "Win Rate":     f"{wr:.1f}%",
                    "Sharpe":       f"{sh:.2f}",
                    "Option P&L":   (f"{r['option_avg_pnl_pct']:+.1f}%"
                                     if r.get("option_avg_pnl_pct") is not None else "—"),
                    "Opt Win":      (f"{r['option_win_rate']:.0f}%"
                                     if r.get("option_win_rate") is not None else "—"),
                    "Best":         f"{r['best_return_pct']:+.2f}%",
                    "Worst":        f"{r['worst_return_pct']:+.2f}%",
                    "Edge":         edge,
                })
            st.dataframe(
                pd.DataFrame(table_rows),
                use_container_width=True,
                hide_index=True,
            )

    st.divider()

    # ── Interpretation guide ──────────────────────────────────────────────────
    st.markdown("**How to interpret these results**")
    st.markdown(
        '<div style="font-size:13px;color:#94a3b8;line-height:1.8;padding:14px 18px;'
        'border-radius:8px;background:rgba(255,255,255,0.02);border:1px solid rgba(255,255,255,0.07)">'
        '<b style="color:#e2e8f0">Monotonicity</b> — the most important signal. '
        'A well-calibrated system shows: STRONG_BUY > BULLISH > NEUTRAL > BEARISH > STRONG_SELL '
        'in average return. If this ordering breaks down, the weights need recalibration.<br><br>'
        '<b style="color:#e2e8f0">Win Rate</b> — above 52% for STRONG_BUY/BULLISH is meaningful. '
        'Markets are roughly 50/50 in the short term so any consistent edge above 52% is real.<br><br>'
        '<b style="color:#e2e8f0">Sharpe above 0.5</b> — indicates risk-adjusted edge. '
        'Above 1.0 is strong. Below 0 means the signal is destructive on a risk-adjusted basis.<br><br>'
        '<b style="color:#e2e8f0">Note</b> — this backtest uses only price-based signals '
        '(momentum, technical, volume). The full tradeability score also includes sentiment, '
        'fundamentals, and earnings signals which cannot be reconstructed historically for free. '
        'Real performance may differ.'
        '</div>',
        unsafe_allow_html=True,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# DATA DEFINITIONS PAGE
# ═══════════════════════════════════════════════════════════════════════════════
elif page == "Data Definitions":
    st.title("Data Definitions")
    st.caption("A complete reference for every metric, signal, and score used in MktScan.")

    CAT_COLORS_DEF = {
        "sentiment":      "#60a5fa",
        "technical":      "#22d3a0",
        "price_momentum": "#34d399",
        "fundamental":    "#a78bfa",
        "event_driven":   "#f87171",
    }

    # ── Section: Tradeability Score ───────────────────────────────────────────
    st.markdown(
        '<div style="background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.08);'
        'border-radius:10px;padding:20px 24px;margin-bottom:24px">'
        '<div style="font-family:IBM Plex Mono,monospace;font-size:11px;color:#64748b;'
        'text-transform:uppercase;letter-spacing:0.08em;margin-bottom:8px">Overview</div>'
        '<div style="font-size:22px;font-weight:600;color:#e2e8f0;margin-bottom:10px">'
        'Tradeability Score</div>'
        '<div style="color:#94a3b8;line-height:1.7;font-size:14px">'
        'A composite score from <b style="color:#e2e8f0">-1.0</b> to <b style="color:#e2e8f0">+1.0</b> '
        'designed to surface the strongest options trading candidates from your basket. '
        'It combines five independently weighted signal categories — each measuring a different '
        'dimension of tradeable opportunity. The final score is a confidence-adjusted weighted '
        'average: categories with thin data (few articles, no price history) automatically '
        'contribute less so they do not distort the result.'
        '</div>'
        '</div>',
        unsafe_allow_html=True,
    )

    # Score scale
    st.markdown("#### Score interpretation")
    scale_items = [
        ("> +0.50", "STRONG BUY",  "#22d3a0", "Strong positive signal across multiple categories. High-conviction bullish trade candidate."),
        ("+0.20 to +0.50", "BULLISH", "#86efac", "Moderate positive lean. Worth watching — confirm with your own chart analysis."),
        ("-0.20 to +0.20", "NEUTRAL", "#fbbf24", "No clear directional edge. Signals are mixed or data is thin."),
        ("-0.50 to -0.20", "BEARISH", "#fca5a5", "Moderate negative lean. Caution advised — consider defensive or bearish strategies."),
        ("< -0.50", "STRONG SELL", "#f87171", "Strong negative signal. Avoid long exposure or consider put strategies."),
    ]
    for score_range, label, color, desc in scale_items:
        st.markdown(
            f'<div style="display:flex;align-items:flex-start;gap:16px;padding:10px 14px;'
            f'margin-bottom:6px;border-radius:6px;background:rgba(255,255,255,0.02);'
            f'border-left:3px solid {color}">'
            f'<div style="min-width:110px;font-family:IBM Plex Mono,monospace;font-size:12px;'
            f'color:{color};font-weight:600;padding-top:1px">{score_range}</div>'
            f'<div style="min-width:100px;font-family:IBM Plex Mono,monospace;font-size:12px;'
            f'color:{color};font-weight:600;padding-top:1px">{label}</div>'
            f'<div style="font-size:13px;color:#94a3b8;line-height:1.5">{desc}</div>'
            f'</div>',
            unsafe_allow_html=True,
        )

    st.divider()

    # ── Five categories ───────────────────────────────────────────────────────
    st.markdown("#### Signal categories")
    st.caption("Each category produces its own sub-score in [-1, +1] then feeds into the weighted composite.")

    CATEGORIES = [
        {
            "key":     "sentiment",
            "label":   "📰 Sentiment Signals",
            "weight":  "Default 30%",
            "color":   "#60a5fa",
            "summary": (
                "A unified sentiment signal combining raw news score, confidence adjustment, "
                "sentiment momentum, source diversity, and recency weighting — all in one category."
            ),
            "how": (
                "Five sub-signals are computed and combined with individual weights: "
                "(1) Raw score (1.5×) — VADER/FinBERT weighted avg across all articles, quality-weighted by source. "
                "(2) Confidence adjustment — raw score is discounted when fewer than 15 articles were found. "
                "(3) Sentiment momentum (1.0×) — compares latest score to oldest score in a 3-run window. "
                "A rising trend = positive momentum. "
                "(4) Source diversity (0.4×) — rewards coverage confirmed across multiple outlets. "
                "1 source = 0 bonus, 4+ sources = max. "
                "(5) Recency-weighted score (1.2×) — re-scores articles with exponential time decay "
                "(48h half-life) so today's news dominates week-old articles."
            ),
            "components": [
                ("Raw sentiment (conf-adjusted)", "VADER/FinBERT avg across articles × (0.4 + 0.6 × confidence). "
                 "Confidence = min(1.0, article_count / 15). Weight: 1.5×."),
                ("Sentiment momentum", "Latest score − oldest score in 3-run window, scaled × 2.5. "
                 "Positive = improving trend. Requires 2+ prior scraper runs. Weight: 1.0×."),
                ("Source diversity", "Fraction of possible sources contributing articles. "
                 "Rewards breadth over single-source stories. Caps at +0.5 contribution. Weight: 0.4×."),
                ("Recency-weighted score", "Exponential decay: articles from 2h ago = 98% weight, "
                 "48h ago = 50%, 1 week ago = 8%. Ensures today's news leads. Weight: 1.2×."),
            ],
            "interpret": (
                "A high combined sentiment score means recent coverage is broadly positive AND improving "
                "across multiple sources AND confirmed by recent articles. This is a stronger signal than "
                "raw sentiment alone. Watch the momentum sub-component — a rising trend on previously "
                "neutral coverage often precedes a price catalyst. "
                "A falling recency-weighted score while the raw score stays high means the most recent "
                "articles are turning negative — an early warning to re-evaluate the position."
            ),
            "caveats": [
                "VADER struggles with financial jargon. Switch to FinBERT in config.yaml for meaningfully better accuracy on this basket.",
                "Momentum requires 2+ prior scraper runs. Shows 0.0 on the first run.",
                "Positive sentiment ≠ price will go up. Markets are forward-looking and often price in news before it hits articles.",
                "Diversity bonus can be gamed by the same story being republished across many outlets — cross-reference with article headlines.",
            ],
        },
        {
            "key":     "technical",
            "label":   "📈 Technical Signals",
            "weight":  "Default 25%",
            "color":   "#22d3a0",
            "summary": "Derives trading signals from price data — where the stock sits in its range, momentum, and analyst consensus.",
            "how": (
                "Four sub-components are computed from the latest price snapshot fetched from Yahoo Finance. "
                "Each is normalised to [-1, +1] and combined with different weights: "
                "52-week position (1.2×), day momentum (0.6×), analyst rating (1.0×), breakout proximity (0.8×). "
                "The weighted average becomes the technical score."
            ),
            "components": [
                ("52-week range position", "Where is the current price within its 52-week high/low band? "
                 "At the 52w high = +1.0 (bullish momentum), at the 52w low = -1.0 (bearish). "
                 "Formula: ((price − 52w_low) / (52w_high − 52w_low) − 0.5) × 2"),
                ("Day momentum", "Today's % price change, clamped at ±5%. "
                 "+5% day = +1.0, -5% day = -1.0. Lower weight (0.6×) as single-day moves are noisy."),
                ("Analyst consensus", "Maps the analyst rating string to a score: "
                 "Strong Buy=+1.0, Buy=+0.6, Outperform=+0.5, Hold=0.0, Underperform=-0.5, Sell=-0.8, Strong Sell=-1.0."),
                ("Breakout proximity", "How close is the price to the 52-week high? "
                 "Within 2% = +0.7 (potential breakout), within 10% = +0.3, more than 20% below = 0.0."),
            ],
            "interpret": (
                "High technical scores favour momentum-based options strategies — buying calls when a stock "
                "is near its 52w high with analyst support. Low scores suggest the stock is in a downtrend "
                "or under distribution. A neutral analyst rating with a strong 52w position can still "
                "produce a high technical score — the price action is leading analyst opinions."
            ),
            "caveats": [
                "Uses end-of-day price data, not intraday. Run the scraper during market hours for fresher data.",
                "Day momentum (0.6× weight) is intentionally underweighted — single-day moves are mean-reverting.",
                "Analyst ratings lag price action by weeks. Use as confirmation, not a leading indicator.",
            ],
        },
        {
            "key":     "price_momentum",
            "label":   "📉 Price Momentum",
            "weight":  "Default 20%",
            "color":   "#34d399",
            "summary": "Quantifies the quality and direction of recent price movement using rolling 14-day daily returns fetched live from Yahoo Finance.",
            "how": (
                "Five sub-components are computed from the last 14 trading days of daily % returns "
                "fetched live from Yahoo Finance each time the Tradeability page loads. "
                "RSI is weighted highest (1.5x) as the most reliable momentum signal. "
                "Trend slope (1.2x), acceleration (1.0x), streak (0.9x), and volatility regime (0.8x) "
                "are combined into a weighted average score in [-1, +1]."
            ),
            "components": [
                ("RSI (14-day)", "Relative Strength Index over the 14-day window. "
                 "Measures average gains vs average losses. "
                 "Score mapping: RSI < 25 = +0.8 (oversold/bullish), 25-40 = +0.4, 40-60 = 0.0 (neutral), "
                 "60-70 = +0.4 (bullish momentum), 70-80 = +0.1 (overbought but still trending), >80 = -0.3 (mean reversion risk). "
                 "Weighted 1.5x — highest weight in this category."),
                ("Trend direction", "Ordinary least squares slope fit across the 14 daily returns. "
                 "A rising slope means returns are getting more positive over the window (building momentum). "
                 "Normalised: slope of +0.5%/day = +1.0, -0.5%/day = -1.0. Weighted 1.2x."),
                ("Annualised volatility", "Standard deviation of the 14 daily returns, annualised by multiplying by sqrt(252). "
                 "Low vol (<20%) = calm, tradeable environment (+0.3). "
                 "Mid vol (20-35%) = normal (+0.1). High vol (35-55%) = uncertain (-0.1). "
                 "Very high vol (>55%) = chaotic (-0.4). Weighted 0.8x."),
                ("Consecutive day streak", "How many trading days in a row has the stock moved in the same direction? "
                 "3+ up days = +0.4, 4+ = +0.6, 5+ = +0.7. Same in reverse for down days. "
                 "1-2 day streaks score near zero — too short to be meaningful. Weighted 0.9x."),
                ("5-day vs 14-day acceleration", "Compares the average daily return of the last 5 days against the full 14-day average. "
                 "Positive acceleration (recent days stronger) = momentum building (+). "
                 "Negative (recent days weaker) = momentum fading (-). Weighted 1.0x."),
            ],
            "interpret": (
                "High price momentum scores favour trend-following strategies — buying calls on stocks "
                "with RSI in the 55-70 range (bullish but not overbought), rising trend slope, and "
                "accelerating recent returns. Avoid chasing RSI > 80 — that is when mean reversion "
                "risk is highest and call premium is expensive. "
                "Low momentum (negative trend, high vol) suggests the stock is in a distribution phase "
                "— better candidates for put strategies or staying in cash. "
                "The streak component catches short-term momentum setups: 4+ consecutive up days with "
                "rising volume is a classic breakout confirmation signal."
            ),
            "caveats": [
                "Uses 14 trading days (~3 calendar weeks) — a short window that is sensitive to recent events. One earnings-day spike can dominate the signal.",
                "RSI at these timeframes is noisier than the traditional 14-week RSI used in charting. Use as one input, not a standalone trigger.",
                "Fetched live from Yahoo Finance on page load — if Yahoo is unavailable, the score defaults to 0.0 with zero confidence.",
                "Annualised vol from 14 days understates true vol for infrequently-moving stocks. Use the volatility reading directionally, not as an absolute measure.",
            ],
        },
        {
            "key":     "fundamental",
            "label":   "📊 Fundamental Signals",
            "weight":  "Default 15%",
            "color":   "#a78bfa",
            "summary": "Scores the company's valuation and earnings quality — P/E ratio, earnings surprise history, and beat consistency.",
            "how": (
                "Three sub-components from price snapshots and historical earnings data. "
                "Earnings surprise (1.2×) is slightly overweighted as consistent beats are a strong quality signal. "
                "Beat streak (0.8×) rewards consistency. P/E (1.0×) penalises stretched valuations."
            ),
            "components": [
                ("P/E ratio band", "Maps trailing P/E to a score based on value thresholds: "
                 "P/E < 15 = +0.6 (cheap), 15–25 = +0.2, 25–35 = 0.0 (fair), 35–50 = -0.3, 50–70 = -0.6, >70 = -0.9 (stretched). "
                 "Note: high-growth tech tends to have elevated P/E — consider adjusting your Fundamental weight down for this sector."),
                ("Avg earnings surprise", "Average EPS surprise % across the last 4 reported quarters. "
                 "Normalised so +10% avg surprise = +1.0, -10% = -1.0. "
                 "Formula: clamp(avg_surprise / 10.0, -1, +1)"),
                ("Beat streak", "Fraction of last 4 quarters with a positive EPS surprise. "
                 "4/4 beats = +1.0, 3/4 = +0.5, 2/4 = 0.0, 1/4 = -0.5, 0/4 = -1.0."),
            ],
            "interpret": (
                "A strong fundamental score means the company is reporting better than expected results "
                "consistently, which tends to attract institutional buying and support options premiums. "
                "A low score combined with a high P/E is a warning sign — expensive stock with disappointing "
                "execution. High growth stocks (NVDA, AMD) will naturally score lower on P/E — reduce the "
                "Fundamental weight or increase Technical/Event weights for growth-focused portfolios."
            ),
            "caveats": [
                "P/E thresholds are calibrated for broad market — high-growth tech typically trades at 40–80× P/E legitimately.",
                "Earnings history is only as complete as what the scraper has collected. Run --mode earnings to populate it.",
                "One massive beat can dominate the avg surprise — look at the beat streak for consistency.",
            ],
        },

        {
            "key":     "event_driven",
            "label":   "⚡ Event-Driven Signals",
            "weight":  "Default 15%",
            "color":   "#f87171",
            "summary": "Captures near-term catalysts — upcoming earnings, recent results, and price breakout potential.",
            "how": (
                "Three sub-components from earnings calendar and price data. "
                "Earnings proximity and last result have equal 1.0× weight; breakout proximity is 0.6×."
            ),
            "components": [
                ("Earnings proximity", "Days until next scheduled earnings report. "
                 "1–7 days away = +0.50 (high IV expansion opportunity), 8–21 days = +0.25, >21 days = 0.0. "
                 "Near-term earnings = elevated implied volatility = richer options premiums."),
                ("Last earnings result", "Was the most recent report a beat or miss, and by how much? "
                 "Formula: clamp(surprise_pct / 15.0, -1, +1). "
                 "+15% EPS beat = +1.0, -15% miss = -1.0. "
                 "Companies that just beat strongly often exhibit post-earnings drift."),
                ("Near 52w high breakout", "Is the price close to breaking to new highs? "
                 "Within 2% of 52w high = +0.70 (breakout candidate), within 10% = +0.30, >20% below = 0.0. "
                 "Breakouts near all-time highs often accelerate with volume."),
            ],
            "interpret": (
                "The event-driven score is most powerful in the 1–3 weeks before an earnings date. "
                "A high score here combined with a strong technical score is the classic setup for a "
                "pre-earnings call buying strategy. After earnings pass, this score resets low until "
                "the next report date is populated by the scraper. "
                "The 52w high component adds a breakout filter — the best opportunities often combine "
                "an upcoming catalyst with a stock already in price discovery."
            ),
            "caveats": [
                "Earnings proximity requires earnings dates in the database — run --mode earnings to populate.",
                "High event score near earnings means high risk too — IV crush after the report can hurt options buyers.",
                "Last result uses surprise %, not absolute EPS — a small company beating by $0.01 on $0.02 estimate is +50% surprise.",
            ],
        },
    ]

    for cat in CATEGORIES:
        color = cat["color"]
        with st.expander(f"{cat['label']}  ·  {cat['weight']}", expanded=False):
            # Header strip
            st.markdown(
                f'<div style="background:{color}18;border:1px solid {color}33;border-radius:8px;'
                f'padding:14px 18px;margin-bottom:16px">'
                f'<div style="font-size:14px;color:#e2e8f0;font-weight:500;margin-bottom:6px">'
                f'{cat["summary"]}</div>'
                f'<div style="font-size:12px;color:#64748b;line-height:1.65">{cat["how"]}</div>'
                f'</div>',
                unsafe_allow_html=True,
            )

            # Components table
            st.markdown("**Sub-components**")
            for comp_name, comp_desc in cat["components"]:
                st.markdown(
                    f'<div style="display:flex;gap:14px;padding:9px 12px;margin-bottom:5px;'
                    f'border-radius:5px;background:rgba(255,255,255,0.02);'
                    f'border-left:2px solid {color}66">'
                    f'<div style="min-width:170px;font-family:IBM Plex Mono,monospace;'
                    f'font-size:11px;color:{color};font-weight:600;padding-top:1px">{comp_name}</div>'
                    f'<div style="font-size:12px;color:#94a3b8;line-height:1.55">{comp_desc}</div>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

            st.markdown("<br>", unsafe_allow_html=True)

            # Interpretation
            st.markdown("**How to interpret it**")
            st.markdown(
                f'<div style="font-size:13px;color:#94a3b8;line-height:1.7;padding:10px 14px;'
                f'border-radius:6px;background:rgba(255,255,255,0.02);border:1px solid rgba(255,255,255,0.06)">'
                f'{cat["interpret"]}</div>',
                unsafe_allow_html=True,
            )

            st.markdown("<br>", unsafe_allow_html=True)

            # Caveats
            st.markdown("**Caveats**")
            for caveat in cat["caveats"]:
                st.markdown(
                    f'<div style="display:flex;gap:10px;padding:6px 0;border-bottom:1px solid rgba(255,255,255,0.05)">'
                    f'<span style="color:#f87171;font-size:12px;flex-shrink:0;padding-top:1px">⚠</span>'
                    f'<span style="font-size:12px;color:#64748b;line-height:1.55">{caveat}</span>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

    st.divider()

    # ── Confidence scoring explained ──────────────────────────────────────────
    st.markdown("#### Confidence adjustment")
    st.markdown(
        '<div style="font-size:13px;color:#94a3b8;line-height:1.7;padding:14px 18px;'
        'border-radius:8px;background:rgba(255,255,255,0.02);border:1px solid rgba(255,255,255,0.07)">'
        'Every category reports a <b style="color:#e2e8f0">confidence score</b> from 0.0 to 1.0 alongside its signal score. '
        'When confidence is low — because there are few articles, no price history, or insufficient earnings data — '
        'the category effective weight in the composite is automatically reduced. '
        'The formula is: <code style="color:#22d3a0;font-family:IBM Plex Mono,monospace">'
        'effective_weight = category_weight × (0.3 + 0.7 × confidence)</code>. '
        'This means even a zero-confidence category contributes 30% of its nominal weight rather than nothing, '
        'avoiding division-by-zero while still penalising thin data. '
        'Confidence scores are shown in the Category Breakdown panel on the Tradeability page.'
        '</div>',
        unsafe_allow_html=True,
    )

    st.divider()

    # ── Sentiment model comparison ────────────────────────────────────────────
    st.markdown("#### Sentiment models")
    models = [
        ("VADER", "Current default", "#fbbf24",
         "Rule-based dictionary lookup. Fast, no download. Scores words like 'beat', 'miss', 'surge', 'plunge'. "
         "Weakness: doesn't understand financial context — 'revenue in line' scores near zero, "
         "'margin compression' is unrecognised. Good for getting started.",
         "config.yaml → sentiment: model: vader"),
        ("FinBERT", "Recommended", "#22d3a0",
         "BERT model fine-tuned on 10,000+ financial news articles, analyst reports, and earnings call transcripts. "
         "Understands jargon: 'beat by a whisker', 'raised full-year guidance', 'supply headwinds'. "
         "~440MB one-time download. Runs locally on CPU in ~2–5 seconds per batch. "
         "Significantly more accurate than VADER for financial text.",
         "config.yaml → sentiment: model: finbert"),
        ("GPT-4o-mini", "Highest quality", "#a78bfa",
         "OpenAI API call per batch of 20 articles. Reads headlines in context, handles sarcasm and nuance. "
         "Best accuracy but costs ~$0.001 per 20 articles (~$0.05 per full basket run). "
         "Requires OPENAI_API_KEY. Ideal for infrequent high-quality runs.",
         "config.yaml → sentiment: model: openai + set openai_api_key"),
    ]
    for name, tag, color, desc, cfg_note in models:
        st.markdown(
            f'<div style="display:flex;gap:16px;padding:14px 16px;margin-bottom:8px;'
            f'border-radius:8px;background:rgba(255,255,255,0.02);border:1px solid rgba(255,255,255,0.07)">'
            f'<div style="min-width:110px">'
            f'<div style="font-family:IBM Plex Mono,monospace;font-size:14px;font-weight:600;color:{color}">{name}</div>'
            f'<div style="font-size:10px;color:#475569;margin-top:3px">{tag}</div>'
            f'</div>'
            f'<div>'
            f'<div style="font-size:13px;color:#94a3b8;line-height:1.6;margin-bottom:6px">{desc}</div>'
            f'<div style="font-family:IBM Plex Mono,monospace;font-size:10px;color:#334155">'
            f'To enable: {cfg_note}</div>'
            f'</div>'
            f'</div>',
            unsafe_allow_html=True,
        )

    st.divider()

    # ── Data sources ──────────────────────────────────────────────────────────
    st.markdown("#### Data sources")
    sources = [
        ("Wire feeds",      "Free",          "#22d3a0", "Business, technology and world news via free RSS. Stored under the source name reuters for historical reasons, but the configured feeds are Dow Jones/WSJ, NYT and MarketWatch — not Reuters. Articles are matched to tickers by keyword and stored as MARKET for macro events."),
        ("Yahoo Finance",   "Free",          "#22d3a0", "Prices, news, earnings calendar. Always enabled. Uses the yfinance Python library — no API key needed."),
        ("FinViz",          "Free (public)", "#22d3a0", "News headlines and fundamentals snapshot. Public endpoint used by default. Elite subscription adds screener data."),
        ("MarketWatch",     "Free",          "#22d3a0", "News headlines and economic calendar. Public pages scraped with polite delay."),
        ("Alpha Vantage",   "Free key",      "#fbbf24", "Price quotes, earnings, curated news feed. Free tier: 25 requests/day. Get a key at alphavantage.co."),
        ("Benzinga Pro",    "~$50/mo",       "#f87171", "High-quality earnings data and news. Best source for earnings calendar accuracy."),
        ("Wall Street Journal", "~$39/mo",   "#f87171", "Long-form market analysis. Requires WSJ+ subscription and session cookie."),
    ]
    src_rows = []
    for name, cost, color, desc in sources:
        src_rows.append({"Source": name, "Cost": cost, "Description": desc})
    st.dataframe(pd.DataFrame(src_rows), use_container_width=True, hide_index=True)

# ═══════════════════════════════════════════════════════════════════════════════
# RUN SCRAPER PAGE
# ═══════════════════════════════════════════════════════════════════════════════
elif page == "Run Scraper":
    st.title("Run Scraper")

    col1, col2 = st.columns(2)
    mode  = col1.selectbox("Scrape mode", ["all", "news", "earnings", "prices"])
    model = col2.selectbox("Sentiment model", ["finbert", "vader", "openai"])

    st.info(
        "**Note:** This runs the scraper in-process. For production use, "
        "run `python -m mktscan schedule` in a separate terminal."
    )

    if st.button("▶ Run Now", type="primary", use_container_width=True):
        from mktscan.engine import ScrapeEngine

        log_placeholder = st.empty()
        log_lines: list[str] = []

        def stream_log(level: str, msg: str):
            icons = {"ok": "✅", "warn": "⚠️", "err": "❌", "info": "ℹ️"}
            icon = icons.get(level, "·")
            ts = datetime.utcnow().strftime("%H:%M:%S")
            log_lines.append(f"`{ts}` {icon}  {msg}")
            log_placeholder.markdown("\n\n".join(log_lines[-25:]))

        try:
            cfg = load_config()
            cfg.setdefault("sentiment", {})["model"] = model
            engine = ScrapeEngine(cfg=cfg, progress_cb=stream_log)
            with st.spinner("Running..."):
                result = engine.run(mode=mode)

            st.success(
                f"✅ Run complete — "
                f"{result['articles_new']} new articles, "
                f"{result['tickers_scored']} tickers scored in "
                f"{result['elapsed_seconds']:.0f}s"
            )

            if result.get("errors"):
                with st.expander("Errors"):
                    for e in result["errors"]:
                        st.error(e)

            if result.get("sentiment"):
                st.subheader("Sentiment Results")
                rows = [
                    {"Ticker": t, "Score": f"{v['score']:+.3f}",
                     "Signal": v["label"], "Articles": v["article_count"]}
                    for t, v in sorted(result["sentiment"].items(), key=lambda x: -x[1]["score"])
                ]
                st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

        except FileNotFoundError:
            st.error("config.yaml not found. Copy config.yaml and fill in your API keys.")
        except Exception as e:
            st.error(f"Run failed: {e}")
            raise

    st.divider()
    st.subheader("Run History")
    session = get_session()
    runs = session.execute(
        select(ScraperRun).order_by(desc(ScraperRun.started_at)).limit(10)
    ).scalars().all()
    session.close()

    if runs:
        run_rows = []
        for r in runs:
            dur = ""
            if r.finished_at and r.started_at:
                dur = f"{(r.finished_at - r.started_at).total_seconds():.0f}s"
            run_rows.append({
                "Run #":    r.id,
                "Started":  str(r.started_at)[:16],
                "Status":   r.status,
                "Articles": r.articles_new or 0,
                "Scored":   r.tickers_scored or 0,
                "Duration": dur,
            })
        st.dataframe(pd.DataFrame(run_rows), use_container_width=True, hide_index=True)
    else:
        st.info("No runs yet.")
