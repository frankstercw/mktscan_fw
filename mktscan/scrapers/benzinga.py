"""
mktscan/scrapers/benzinga.py
Benzinga Pro API scraper.
Docs: https://docs.benzinga.io/benzinga-apis/
Subscription: benzinga.com/apis (~$50/mo for news + earnings)
"""
from __future__ import annotations
import logging
import time
from datetime import datetime, timedelta
from typing import Any

import requests
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

log = logging.getLogger(__name__)


class BenzingaScraper:
    """
    Fetches news, earnings, and analyst ratings from the Benzinga Pro API.
    """

    def __init__(self, cfg: dict[str, Any], delay: float = 2.5, lookback_days: int = 7):
        self.api_key     = cfg.get("api_key", "")
        self.base_url    = cfg.get("base_url", "https://api.benzinga.com/api/v2").rstrip("/")
        self.delay       = delay
        self.lookback    = lookback_days
        self.session     = requests.Session()
        self.session.headers.update({
            "accept":     "application/json",
            "User-Agent": "MktScan/1.0",
        })

        if not self.api_key or self.api_key.startswith("YOUR_"):
            log.warning("[Benzinga] No valid API key. Calls will fail.")

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(requests.RequestException),
        reraise=True,
    )
    def _get(self, endpoint: str, params: dict) -> Any:
        params["token"] = self.api_key
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        r = self.session.get(url, params=params, timeout=15)
        r.raise_for_status()
        return r.json()

    def fetch_news(self, ticker: str, max_articles: int = 50) -> list[dict]:
        """Fetch recent news articles mentioning the ticker."""
        results = []
        date_from = (datetime.utcnow() - timedelta(days=self.lookback)).strftime("%Y-%m-%d")

        try:
            data = self._get("news", {
                "tickers":   ticker,
                "dateFrom":  date_from,
                "pageSize":  min(max_articles, 100),
                "displayOutput": "full",
            })

            items = data if isinstance(data, list) else data.get("data", [])
            for item in items[:max_articles]:
                published_at = self._parse_date(
                    item.get("created") or item.get("updated") or item.get("date", "")
                )
                results.append({
                    "source":       "benzinga",
                    "ticker":       ticker,
                    "headline":     item.get("title", ""),
                    "body_snippet": (item.get("teaser") or item.get("body", ""))[:500],
                    "url":          item.get("url", ""),
                    "published_at": published_at,
                })
        except Exception as e:
            log.error(f"[Benzinga] News failed for {ticker}: {e}")
        finally:
            time.sleep(self.delay)

        return results

    def fetch_earnings(self, ticker: str) -> list[dict]:
        """Upcoming and recent earnings calendar."""
        results = []
        date_from = (datetime.utcnow() - timedelta(days=90)).strftime("%Y-%m-%d")
        date_to   = (datetime.utcnow() + timedelta(days=30)).strftime("%Y-%m-%d")

        try:
            data = self._get("calendar/earnings", {
                "parameters[tickers]": ticker,
                "parameters[dateFrom]": date_from,
                "parameters[dateTo]":   date_to,
            })

            items = (data.get("earnings") or []) if isinstance(data, dict) else []
            for item in items:
                results.append({
                    "ticker":          ticker,
                    "period":          item.get("period", ""),
                    "report_date":     self._parse_date(item.get("date", "")),
                    "eps_estimate":    self._safe_float(item.get("eps_est")),
                    "eps_actual":      self._safe_float(item.get("eps")),
                    "revenue_estimate":self._safe_float(item.get("revenue_est")),
                    "revenue_actual":  self._safe_float(item.get("revenue")),
                    "surprise_pct":    self._safe_float(item.get("eps_surprise_percent")),
                    "source":          "benzinga",
                })
        except Exception as e:
            log.error(f"[Benzinga] Earnings failed for {ticker}: {e}")
        finally:
            time.sleep(self.delay)

        return results

    def fetch_ratings(self, ticker: str, lookback_days: int = 30, max_items: int = 100) -> list[dict]:
        """Fetch Benzinga analyst rating and price-target actions.

        Endpoint documented by Benzinga:
        GET /api/v2/calendar/ratings
        """
        results = []
        date_from = (datetime.utcnow() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
        date_to = datetime.utcnow().strftime("%Y-%m-%d")

        try:
            data = self._get("calendar/ratings", {
                "parameters[tickers]": ticker,
                "parameters[dateFrom]": date_from,
                "parameters[dateTo]": date_to,
                "pagesize": min(max_items, 1000),
            })

            items = (data.get("ratings") or []) if isinstance(data, dict) else []
            for item in items[:max_items]:
                published_at = None
                updated = item.get("updated")
                if updated is not None:
                    try:
                        ts = float(updated)
                        # Benzinga's updated field is documented as Unix time;
                        # tolerate millisecond/microsecond representations too.
                        while ts > 10_000_000_000:
                            ts /= 1000.0
                        published_at = datetime.utcfromtimestamp(ts)
                    except (TypeError, ValueError, OSError):
                        published_at = None
                if published_at is None:
                    date_part = str(item.get("date") or "")
                    time_part = str(item.get("time") or "")
                    published_at = self._parse_date(
                        f"{date_part} {time_part}".strip()
                    ) or self._parse_date(date_part)

                results.append({
                    "external_id": str(item.get("id") or ""),
                    "ticker": str(item.get("ticker") or ticker).upper(),
                    "published_at": published_at or datetime.utcnow(),
                    "firm": item.get("analyst") or "",
                    "analyst_name": item.get("analyst_name") or "",
                    "action_company": item.get("action_company") or "",
                    "action_pt": item.get("action_pt") or "",
                    "rating_prior": item.get("rating_prior") or "",
                    "rating_current": item.get("rating_current") or "",
                    "pt_prior": self._safe_float(item.get("pt_prior")),
                    "pt_current": self._safe_float(item.get("pt_current")),
                    "importance": self._safe_int(item.get("importance")),
                    "url": item.get("url") or item.get("url_news") or item.get("url_calendar") or "",
                    "source": "benzinga",
                    "raw": item,
                })
        except Exception as e:
            log.error(f"[Benzinga] Ratings failed for {ticker}: {e}")
        finally:
            time.sleep(self.delay)

        return results

    @staticmethod
    def _safe_float(val) -> float | None:
        try:
            return float(val)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _safe_int(val) -> int | None:
        try:
            return int(float(val))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _parse_date(s: str) -> datetime | None:
        if not s:
            return None
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(s[:19], fmt)
            except ValueError:
                pass
        return None
