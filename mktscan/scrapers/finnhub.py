"""
mktscan/scrapers/finnhub.py
Finnhub.io scraper — free tier gives 60 API calls/minute.

Fetches two types of data per ticker:
  1. Company news  — recent headlines with summary and source
  2. Earnings surprises — actual vs estimated EPS (high-signal events)

Free API key at: https://finnhub.io (takes ~30 seconds to sign up)
Add to config.yaml:
  finnhub:
    enabled: true
    api_key: "YOUR_FINNHUB_KEY"
"""
from __future__ import annotations
import logging
import time
from datetime import datetime, timedelta
from typing import Any

import requests

log = logging.getLogger(__name__)

FINNHUB_BASE = "https://finnhub.io/api/v1"


class FinnhubScraper:
    """
    Fetches company news and earnings surprises from Finnhub.
    Free tier: 60 calls/minute, no daily cap on news.
    """

    def __init__(self, cfg: dict[str, Any], delay: float = 1.0, lookback_days: int = 7):
        self.enabled     = cfg.get("enabled", False)
        self.api_key     = cfg.get("api_key", "")
        self.delay       = delay
        self.lookback    = lookback_days
        self.session     = requests.Session()
        self.session.headers.update({"User-Agent": "MktScan/1.0"})

    # ── News ─────────────────────────────────────────────────────────────────

    def fetch_news(self, ticker: str) -> list[dict]:
        """
        Fetch recent company news for a ticker.
        Returns list of article dicts compatible with the engine's article format.
        """
        if not self.enabled or not self.api_key:
            return []

        end   = datetime.utcnow().date()
        start = end - timedelta(days=self.lookback)

        try:
            resp = self.session.get(
                f"{FINNHUB_BASE}/company-news",
                params={
                    "symbol": ticker,
                    "from":   str(start),
                    "to":     str(end),
                    "token":  self.api_key,
                },
                timeout=10,
            )
            resp.raise_for_status()
            items = resp.json()

            if not isinstance(items, list):
                log.warning(f"[Finnhub] Unexpected response for {ticker}: {items}")
                return []

            articles = []
            for item in items[:20]:  # cap at 20 per ticker
                headline = item.get("headline", "").strip()
                summary  = item.get("summary", "").strip()
                url      = item.get("url", "")
                source   = item.get("source", "finnhub")
                ts       = item.get("datetime", 0)

                if not headline:
                    continue

                try:
                    published_at = datetime.utcfromtimestamp(ts) if ts else datetime.utcnow()
                except (OSError, ValueError, OverflowError):
                    published_at = datetime.utcnow()

                articles.append({
                    "source":       "finnhub",
                    "ticker":       ticker,
                    "headline":     headline,
                    "body_snippet": summary[:500] if summary else headline,
                    "url":          url,
                    "published_at": published_at,
                })

            log.info(f"[Finnhub] {ticker}: {len(articles)} news articles")
            time.sleep(self.delay)
            return articles

        except requests.exceptions.RequestException as e:
            log.warning(f"[Finnhub] News request failed for {ticker}: {e}")
            return []
        except Exception as e:
            log.warning(f"[Finnhub] Unexpected error for {ticker}: {e}")
            return []

    # ── Earnings surprises ───────────────────────────────────────────────────

    def fetch_earnings_surprise(self, ticker: str) -> dict | None:
        """
        Fetch the most recent earnings surprise for a ticker.
        Returns a dict with actual vs estimated EPS and surprise %.
        High surprise values are strong next-day price move signals.
        """
        if not self.enabled or not self.api_key:
            return None

        try:
            resp = self.session.get(
                f"{FINNHUB_BASE}/stock/earnings",
                params={
                    "symbol": ticker,
                    "limit":  4,  # last 4 quarters
                    "token":  self.api_key,
                },
                timeout=10,
            )
            resp.raise_for_status()
            items = resp.json()

            if not isinstance(items, list) or not items:
                return None

            # Most recent quarter
            latest = items[0]
            actual    = latest.get("actual")
            estimate  = latest.get("estimate")
            period    = latest.get("period", "")
            surprise  = latest.get("surprisePercent")

            if actual is None or estimate is None:
                return None

            result = {
                "ticker":           ticker,
                "period":           period,
                "actual_eps":       actual,
                "estimated_eps":    estimate,
                "surprise_pct":     surprise,
                "beat":             actual > estimate if actual is not None and estimate is not None else None,
            }

            log.info(
                f"[Finnhub] {ticker} earnings: actual={actual} est={estimate} "
                f"surprise={surprise:.1f}%" if surprise is not None else
                f"[Finnhub] {ticker} earnings: actual={actual} est={estimate}"
            )

            time.sleep(self.delay)
            return result

        except requests.exceptions.RequestException as e:
            log.warning(f"[Finnhub] Earnings request failed for {ticker}: {e}")
            return None
        except Exception as e:
            log.warning(f"[Finnhub] Unexpected earnings error for {ticker}: {e}")
            return None

    # ── Bulk helpers ─────────────────────────────────────────────────────────

    def fetch_all_earnings_surprises(self, tickers: list[str]) -> dict[str, dict]:
        """Fetch earnings surprises for all tickers. Returns {ticker: surprise_dict}."""
        results = {}
        for ticker in tickers:
            surprise = self.fetch_earnings_surprise(ticker)
            if surprise:
                results[ticker] = surprise
        return results
