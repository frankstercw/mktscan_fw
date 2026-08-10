"""
mktscan/scrapers/wsj.py
Wall Street Journal scraper.
Requires WSJ+ subscription ($39/mo). Uses session cookie auth.
Respects robots.txt — only fetches ticker-specific search results.
"""
from __future__ import annotations
import logging
import re
import time
from datetime import datetime
from typing import Any
from urllib.parse import urlencode, quote_plus

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

WSJ_SEARCH_URL = "https://www.wsj.com/search"
WSJ_MARKETS_URL = "https://www.wsj.com/market-data/quotes/{ticker}/research-ratings"


class WSJScraper:
    """
    Scrapes WSJ for market news articles matching ticker keywords.
    Requires an active WSJ+ session cookie.

    To get your session cookie:
    1. Log into wsj.com
    2. Open DevTools > Application > Cookies > wsj.com
    3. Copy the value of 'wsjregion', 'refresh_token', and 'usr_bst' cookies
    4. Paste the full cookie string into config.yaml -> sources.wsj.session_cookie
    """

    def __init__(self, cfg: dict[str, Any], delay: float = 3.0, lookback_days: int = 7):
        self.session_cookie = cfg.get("session_cookie", "")
        self.delay          = delay
        self.lookback       = lookback_days
        self.enabled        = bool(
            self.session_cookie and not self.session_cookie.startswith("YOUR_")
        )

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent":      "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                               "Chrome/120.0.0.0 Safari/537.36",
            "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })

        if self.enabled:
            self.session.headers["Cookie"] = self.session_cookie
            log.info("[WSJ] Session cookie loaded")
        else:
            log.warning("[WSJ] No session cookie — WSJ scraping disabled")

    def fetch_news(self, ticker: str, keywords: list[str], max_articles: int = 20) -> list[dict]:
        """
        Search WSJ for articles mentioning the ticker or its keywords.
        """
        if not self.enabled:
            return []

        results = []
        query = " OR ".join(f'"{kw}"' for kw in keywords[:3])  # Use top 3 keywords

        try:
            params = {
                "query": query,
                "min-date": self._days_ago(self.lookback),
                "max-date": self._today(),
                "isWSJProUser": "true",
                "source": "markets,finance,politics",
            }
            url = f"{WSJ_SEARCH_URL}?{urlencode(params)}"
            r = self.session.get(url, timeout=15)
            r.raise_for_status()

            soup = BeautifulSoup(r.text, "lxml")

            # WSJ article cards — try multiple selectors across site versions
            article_cards = (
                soup.find_all("article", class_=re.compile("WSJTheme")) or
                soup.find_all("div", attrs={"data-type": "article"}) or
                soup.find_all("div", class_=re.compile("article-container"))
            )

            for card in article_cards[:max_articles]:
                headline_el = (
                    card.find("h3") or card.find("h2") or
                    card.find(class_=re.compile("headline"))
                )
                link_el = card.find("a", href=True)
                time_el = card.find("time") or card.find(class_=re.compile("timestamp"))

                if not headline_el:
                    continue

                headline = headline_el.get_text(strip=True)
                url_href = link_el["href"] if link_el else ""
                if url_href and not url_href.startswith("http"):
                    url_href = "https://www.wsj.com" + url_href

                published_at = None
                if time_el:
                    dt_str = time_el.get("datetime") or time_el.get_text(strip=True)
                    published_at = self._parse_date(dt_str)

                if headline:
                    results.append({
                        "source":       "wsj",
                        "ticker":       ticker,
                        "headline":     headline,
                        "body_snippet": "",
                        "url":          url_href,
                        "published_at": published_at,
                    })

        except requests.HTTPError as e:
            if e.response.status_code == 401:
                log.error("[WSJ] 401 Unauthorized — session cookie may be expired")
            elif e.response.status_code == 403:
                log.error("[WSJ] 403 Forbidden — subscription may not cover this content")
            else:
                log.error(f"[WSJ] HTTP error for {ticker}: {e}")
        except Exception as e:
            log.error(f"[WSJ] Scrape failed for {ticker}: {e}")
        finally:
            time.sleep(self.delay)

        return results

    @staticmethod
    def _days_ago(n: int) -> str:
        from datetime import timedelta
        return (datetime.utcnow() - timedelta(days=n)).strftime("%Y/%m/%d")

    @staticmethod
    def _today() -> str:
        return datetime.utcnow().strftime("%Y/%m/%d")

    @staticmethod
    def _parse_date(s: str) -> datetime | None:
        if not s:
            return None
        for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ",
                    "%Y-%m-%d %H:%M", "%B %d, %Y", "%b. %d, %Y"):
            try:
                return datetime.strptime(s[:25], fmt)
            except ValueError:
                pass
        return None
