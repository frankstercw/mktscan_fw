"""On-demand MktScan review for any supported U.S. ticker.

This module intentionally does not add the symbol to the persistent basket. It
assembles the same core inputs used by MktScan (news sentiment, Yahoo
price/fundamental data, earnings, daily momentum, technical opportunity,
implied-volatility context and trade construction) and returns one ephemeral
review payload for the dashboard.
"""
from __future__ import annotations

import re
from typing import Any

from .config import get_config
from .iv_rank import compute_iv_rank
from .options import generate_trade_setup
from .options_market import build_yahoo_options_market
from .scrapers.marketwatch import MarketWatchScraper
from .scrapers.yahoo import YahooScraper
from .sentiment import aggregate_scores, build_scorer
from .terminal import technical_opportunity
from .tradeability import compute_tradeability, fetch_daily_returns

_TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,14}$")


def normalize_ticker(value: str) -> str:
    ticker = (value or "").strip().upper()
    if not _TICKER_RE.fullmatch(ticker):
        raise ValueError("Enter a valid ticker symbol, e.g. AAPL, BRK-B, NVDA.")
    return ticker


def run_on_demand_review(session, ticker: str) -> dict[str, Any]:
    """Run the MktScan review pipeline for a ticker without adding it to the basket."""
    ticker = normalize_ticker(ticker)
    cfg = get_config()
    sources = cfg.get("sources", {})

    yahoo_cfg = dict(sources.get("yahoo_finance", {}))
    yahoo_cfg.setdefault("fetch_prices", True)
    yahoo_cfg.setdefault("fetch_news", True)
    yahoo_cfg.setdefault("fetch_earnings", True)
    yahoo = YahooScraper(yahoo_cfg)
    ydata = yahoo.fetch_ticker(ticker)

    price_data = ydata.get("prices") or {}
    if not price_data.get("price"):
        raise ValueError(
            f"No current market data was returned for {ticker}. "
            "Check the symbol and make sure Yahoo Finance supports it."
        )

    articles = list(ydata.get("news") or [])
    # Add MarketWatch ticker news when available. Failure here is non-fatal:
    # Yahoo alone still allows the rest of the review to run.
    try:
        mw_cfg = dict(sources.get("marketwatch", {}))
        mw = MarketWatchScraper(mw_cfg, delay=0.0, lookback_days=7)
        articles.extend(mw.fetch_news(ticker, max_articles=20))
    except Exception:
        pass

    scorer = build_scorer(cfg.get("sentiment", {}))
    sentiment = aggregate_scores(
        articles,
        scorer,
        source_weights={
            "marketwatch": 1.1,
            "yahoo": 1.0,
            "reuters": 1.3,
            "finnhub": 1.2,
            "benzinga": 1.2,
            "wsj": 1.5,
        },
    )

    earnings = list(ydata.get("earnings") or [])
    daily_returns = fetch_daily_returns(ticker)

    # Use any IV history already stored for the ticker. An arbitrary ticker will
    # usually have no rank history on first review, which is surfaced honestly.
    iv_rank = compute_iv_rank(session, ticker)

    tradeability = compute_tradeability(
        ticker=ticker,
        sentiment_score=sentiment.get("score"),
        article_count=sentiment.get("article_count", 0),
        articles=articles,
        sentiment_history=[],
        price_data=price_data,
        earnings_events=earnings,
        daily_returns=daily_returns,
        xs=None,  # no basket-relative cross-sectional rank for an ad-hoc ticker
        iv_rank_data=iv_rank,
    )

    tech = technical_opportunity(ticker)

    options_market = None
    options_error = None
    try:
        options_market = build_yahoo_options_market(session, ticker)
    except Exception as exc:
        options_error = str(exc)

    trade_setup = None
    trade_setup_error = None
    try:
        trade_setup = generate_trade_setup(ticker, tradeability)
    except Exception as exc:
        trade_setup_error = str(exc)

    return {
        "ticker": ticker,
        "price_data": price_data,
        "earnings": earnings,
        "articles": articles,
        "sentiment": sentiment,
        "daily_returns": daily_returns,
        "iv_rank": iv_rank,
        "tradeability": tradeability,
        "technical": tech,
        "options_market": options_market,
        "options_error": options_error,
        "trade_setup": trade_setup,
        "trade_setup_error": trade_setup_error,
        "is_ephemeral": True,
    }
