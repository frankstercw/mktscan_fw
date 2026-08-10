"""
mktscan/scrapers/finviz.py
FinViz scraper — HTML parsing with BeautifulSoup.
Free tier: basic data. FinViz Elite (~$40/mo): full screener + news.
Uses requests + BeautifulSoup for static pages.
"""
from __future__ import annotations
import logging
import re
import time
from datetime import datetime
from typing import Any

import requests
from bs4 import BeautifulSoup
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

log = logging.getLogger(__name__)

FINVIZ_BASE  = "https://finviz.com"
ELITE_BASE   = "https://elite.finviz.com"
NEWS_URL     = "{base}/quote.ashx?t={ticker}"
SCREENER_URL = "{base}/screener.ashx?v=111&f=idx_sp500&o=-marketcap"


class FinVizScraper:
    """
    Scrapes FinViz for news headlines, analyst ratings, and screener data.
    Pass session_cookie for Elite access; otherwise uses public endpoint.
    """

    def __init__(self, cfg: dict[str, Any], delay: float = 3.0):
        self.session_cookie = cfg.get("session_cookie", "")
        self.delay = delay
        self.elite = bool(self.session_cookie and not self.session_cookie.startswith("YOUR_"))
        self.base  = ELITE_BASE if self.elite else FINVIZ_BASE

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent":      "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                               "Chrome/120.0.0.0 Safari/537.36",
            "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer":         "https://finviz.com/",
        })

        if self.elite:
            self.session.headers["Cookie"] = self.session_cookie
            log.info("[FinViz] Using Elite session")
        else:
            log.info("[FinViz] Using public endpoint (limited data)")

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=3, max=15),
        retry=retry_if_exception_type(requests.RequestException),
        reraise=True,
    )
    def _get(self, url: str) -> BeautifulSoup:
        r = self.session.get(url, timeout=15)
        if r.status_code == 429:
            log.warning("[FinViz] Rate limit hit — waiting 30s")
            time.sleep(30)
            r = self.session.get(url, timeout=15)
        r.raise_for_status()
        return BeautifulSoup(r.text, "lxml")

    def fetch_news(self, ticker: str, max_articles: int = 30) -> list[dict]:
        """Scrape news table from FinViz ticker quote page."""
        results = []
        url = NEWS_URL.format(base=self.base, ticker=ticker)

        try:
            soup = self._get(url)

            # FinViz renders news in a table with id="news-table"
            news_table = soup.find("table", id="news-table")
            if not news_table:
                log.debug(f"[FinViz] No news table found for {ticker}")
                return []

            current_date = None
            for row in news_table.find_all("tr"):
                cells = row.find_all("td")
                if len(cells) < 2:
                    continue

                date_cell = cells[0].get_text(strip=True)
                link_cell = cells[1]

                # Date cell may contain "May-01-24 08:30AM" or just "08:30AM"
                if re.match(r"[A-Za-z]", date_cell):
                    current_date = date_cell.split()[0]

                time_part = date_cell.split()[-1] if date_cell else ""
                published_at = None
                if current_date and time_part:
                    try:
                        published_at = datetime.strptime(
                            f"{current_date} {time_part}", "%b-%d-%y %I:%M%p"
                        )
                    except ValueError:
                        pass

                a_tag = link_cell.find("a")
                if not a_tag:
                    continue

                headline = a_tag.get_text(strip=True)
                url_href = a_tag.get("href", "")

                # Source tag (small element after headline)
                source_tag = link_cell.find("span", class_="news-link-right")
                sub_source = source_tag.get_text(strip=True) if source_tag else "FinViz"

                if headline:
                    results.append({
                        "source":       "finviz",
                        "ticker":       ticker,
                        "headline":     headline,
                        "body_snippet": f"[via {sub_source}]",
                        "url":          url_href,
                        "published_at": published_at,
                    })

                if len(results) >= max_articles:
                    break

        except Exception as e:
            log.error(f"[FinViz] News scrape failed for {ticker}: {e}")
        finally:
            time.sleep(self.delay)

        return results

    def fetch_fundamentals(self, ticker: str) -> dict | None:
        """Scrape fundamental data snapshot table."""
        url = NEWS_URL.format(base=self.base, ticker=ticker)
        try:
            soup = self._get(url)

            # FinViz snapshot table — pairs of label/value cells
            snapshot = soup.find("table", class_="snapshot-table2")
            if not snapshot:
                # Try alternate class names across FinViz versions
                snapshot = soup.find("table", attrs={"class": re.compile("snapshot")})
            if not snapshot:
                return None

            cells = snapshot.find_all("td")
            data: dict[str, str] = {}
            for i in range(0, len(cells) - 1, 2):
                key = cells[i].get_text(strip=True)
                val = cells[i + 1].get_text(strip=True)
                data[key] = val

            def _f(k: str) -> float | None:
                v = data.get(k, "").replace(",", "").replace("%", "").replace("B", "e9").replace("M", "e6")
                try:
                    return float(v)
                except ValueError:
                    return None

            return {
                "ticker":          ticker,
                "price":           _f("Price"),
                "change_pct":      _f("Change"),
                "pe_ratio":        _f("P/E"),
                "eps_ttm":         _f("EPS (ttm)"),
                "analyst_rating":  data.get("Recom"),
                "target_price":    _f("Target Price"),
                "52w_high":        _f("52W High"),
                "52w_low":         _f("52W Low"),
                "avg_volume":      _f("Avg Volume"),
                "market_cap":      _f("Market Cap"),
                "source":          "finviz",
            }

        except Exception as e:
            log.error(f"[FinViz] Fundamentals failed for {ticker}: {e}")
            return None
        finally:
            time.sleep(self.delay)

    def fetch_analyst_ratings(self, ticker: str) -> list[dict]:
        """Parse analyst upgrade/downgrade table."""
        url = NEWS_URL.format(base=self.base, ticker=ticker)
        results = []
        try:
            soup = self._get(url)
            ratings_table = soup.find("table", class_=re.compile("ratings"))
            if not ratings_table:
                return []

            for row in ratings_table.find_all("tr")[1:]:  # skip header
                cells = [c.get_text(strip=True) for c in row.find_all("td")]
                if len(cells) >= 4:
                    results.append({
                        "ticker":  ticker,
                        "date":    cells[0],
                        "action":  cells[1],
                        "firm":    cells[2],
                        "rating":  cells[3],
                        "pt":      cells[4] if len(cells) > 4 else None,
                        "source":  "finviz",
                    })
        except Exception as e:
            log.debug(f"[FinViz] Ratings parse failed for {ticker}: {e}")
        finally:
            time.sleep(self.delay)
        return results
