"""
mktscan/scrapers/alphavantage.py
Alpha Vantage REST API scraper.
Free tier: 25 requests/day. Premium: higher limits.
API key: https://www.alphavantage.co/support/#api-key
"""
from __future__ import annotations
import logging
import time
from datetime import datetime
from typing import Any

import requests
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

log = logging.getLogger(__name__)

AV_BASE = "https://www.alphavantage.co/query"


class AlphaVantageScraper:
    """
    Fetches stock quotes, earnings, and news sentiment
    from the Alpha Vantage API.
    """

    def __init__(self, cfg: dict[str, Any], delay: float = 2.5):
        self.api_key  = cfg.get("api_key", "")
        self.base_url = cfg.get("base_url", AV_BASE)
        self.delay    = delay
        self.session  = requests.Session()
        self.session.headers.update({"User-Agent": "MktScan/1.0"})

        if not self.api_key or self.api_key.startswith("YOUR_"):
            log.warning("[AV] No valid API key configured. Alpha Vantage calls will fail.")

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(requests.RequestException),
        reraise=True,
    )
    def _get(self, params: dict) -> dict:
        params["apikey"] = self.api_key
        r = self.session.get(self.base_url, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        # AV returns {"Information": "..."} on rate limit
        if "Information" in data or "Note" in data:
            msg = data.get("Information") or data.get("Note", "Rate limited")
            raise RuntimeError(f"Alpha Vantage limit: {msg}")
        return data

    def fetch_quote(self, ticker: str) -> dict | None:
        """Global quote — price, change, volume."""
        try:
            data = self._get({"function": "GLOBAL_QUOTE", "symbol": ticker})
            q = data.get("Global Quote", {})
            if not q:
                return None
            return {
                "ticker":     ticker,
                "price":      float(q.get("05. price", 0) or 0),
                "change_pct": float(q.get("10. change percent", "0%").replace("%", "") or 0),
                "volume":     int(q.get("06. volume", 0) or 0),
            }
        except Exception as e:
            log.error(f"[AV] Quote failed for {ticker}: {e}")
            return None
        finally:
            time.sleep(self.delay)

    def fetch_earnings(self, ticker: str) -> list[dict]:
        """Annual and quarterly earnings."""
        results = []
        try:
            data = self._get({"function": "EARNINGS", "symbol": ticker})
            for report in (data.get("quarterlyEarnings") or [])[:8]:
                results.append({
                    "ticker":         ticker,
                    "period":         report.get("fiscalDateEnding", ""),
                    "eps_estimate":   self._safe_float(report.get("estimatedEPS")),
                    "eps_actual":     self._safe_float(report.get("reportedEPS")),
                    "surprise_pct":   self._safe_float(report.get("surprisePercentage")),
                    "report_date":    self._parse_date(report.get("reportedDate")),
                    "source":         "alphav",
                })
        except Exception as e:
            log.error(f"[AV] Earnings failed for {ticker}: {e}")
        finally:
            time.sleep(self.delay)
        return results

    def fetch_news(self, ticker: str, limit: int = 50) -> list[dict]:
        """News sentiment feed from Alpha Vantage."""
        results = []
        try:
            data = self._get({
                "function": "NEWS_SENTIMENT",
                "tickers":  ticker,
                "limit":    limit,
                "sort":     "LATEST",
            })
            for item in (data.get("feed") or []):
                # Check if this ticker is actually relevant
                ticker_sentiments = item.get("ticker_sentiment", [])
                relevance = next(
                    (float(ts.get("relevance_score", 0))
                     for ts in ticker_sentiments
                     if ts.get("ticker") == ticker),
                    0.0
                )
                if relevance < 0.1:
                    continue

                pub_raw = item.get("time_published", "")
                published_at = None
                if pub_raw:
                    try:
                        published_at = datetime.strptime(pub_raw, "%Y%m%dT%H%M%S")
                    except ValueError:
                        pass

                results.append({
                    "source":       "alphav",
                    "ticker":       ticker,
                    "headline":     item.get("title", ""),
                    "body_snippet": item.get("summary", ""),
                    "url":          item.get("url", ""),
                    "published_at": published_at,
                })
        except Exception as e:
            log.error(f"[AV] News failed for {ticker}: {e}")
        finally:
            time.sleep(self.delay)
        return results

    def fetch_overview(self, ticker: str) -> dict | None:
        """Company overview — sector, P/E, market cap, etc."""
        try:
            data = self._get({"function": "OVERVIEW", "symbol": ticker})
            if not data or "Symbol" not in data:
                return None
            return {
                "ticker":    ticker,
                "sector":    data.get("Sector", ""),
                "pe_ratio":  self._safe_float(data.get("PERatio")),
                "market_cap":self._safe_float(data.get("MarketCapitalization")),
                "52wk_high": self._safe_float(data.get("52WeekHigh")),
                "52wk_low":  self._safe_float(data.get("52WeekLow")),
                "analyst_target": self._safe_float(data.get("AnalystTargetPrice")),
            }
        except Exception as e:
            log.error(f"[AV] Overview failed for {ticker}: {e}")
            return None
        finally:
            time.sleep(self.delay)

    @staticmethod
    def _safe_float(val) -> float | None:
        try:
            v = float(val)
            return None if v == 0 else v
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _parse_date(s: str | None) -> datetime | None:
        if not s:
            return None
        for fmt in ("%Y-%m-%d", "%Y%m%d"):
            try:
                return datetime.strptime(s, fmt)
            except ValueError:
                pass
        return None
