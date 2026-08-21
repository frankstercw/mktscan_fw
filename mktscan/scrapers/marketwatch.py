"""
mktscan/scrapers/marketwatch.py
MarketWatch scraper — no subscription required for public pages.
Fetches:
  - News headlines per ticker (search page)
  - Economic calendar events (FOMC, CPI, PCE, jobs, GDP etc.)

MarketWatch's public pages are freely accessible. This scraper
respects a polite delay between requests and only hits public URLs.
"""
from __future__ import annotations
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Any
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

log = logging.getLogger(__name__)

MW_BASE         = "https://www.marketwatch.com"
MW_SEARCH_URL   = "https://www.marketwatch.com/search?q={query}&ts=0&tab=All%20News"
MW_TICKER_URL   = "https://www.marketwatch.com/investing/stock/{ticker}/news"
MW_ECON_CAL_URL = "https://www.marketwatch.com/economy-politics/calendar"


class MarketWatchScraper:
    """
    Scrapes MarketWatch for ticker news and the economic events calendar.
    No API key or subscription required.
    """

    def __init__(self, cfg: dict[str, Any], delay: float = 3.0, lookback_days: int = 7):
        self.delay       = delay
        self.lookback    = lookback_days
        self.enabled     = cfg.get("enabled", True)

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent":      "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                               "Chrome/124.0.0.0 Safari/537.36",
            "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "DNT":             "1",
            "Referer":         "https://www.marketwatch.com/",
        })

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=3, max=15),
        retry=retry_if_exception_type(requests.RequestException),
        reraise=True,
    )
    def _get(self, url: str) -> BeautifulSoup:
        r = self.session.get(url, timeout=15)
        if r.status_code == 429:
            log.warning("[MarketWatch] Rate limited — waiting 20s")
            time.sleep(3)  # reduced from 20s — already blocked on cloud, no point waiting
            r = self.session.get(url, timeout=15)
        r.raise_for_status()
        return BeautifulSoup(r.text, "lxml")

    # ── News ──────────────────────────────────────────────────────────────────

    def fetch_news(self, ticker: str, max_articles: int = 30) -> list[dict]:
        """
        Scrape MarketWatch news headlines for a ticker.
        Uses the ticker-specific news page first, falls back to search.
        """
        if not self.enabled:
            return []

        results = []
        url = MW_TICKER_URL.format(ticker=ticker.lower())

        try:
            soup = self._get(url)
            results = self._parse_news_page(soup, ticker, max_articles)

            # Fallback to search if ticker page returned nothing
            if not results:
                search_url = MW_SEARCH_URL.format(query=quote_plus(ticker))
                soup       = self._get(search_url)
                results    = self._parse_search_results(soup, ticker, max_articles)

        except Exception as e:
            log.error(f"[MarketWatch] News failed for {ticker}: {e}")
        finally:
            time.sleep(self.delay)

        log.debug(f"[MarketWatch] {ticker}: {len(results)} articles")
        return results

    def _parse_news_page(self, soup: BeautifulSoup, ticker: str, limit: int) -> list[dict]:
        """Parse the /news page for a specific ticker."""
        articles = []

        # MarketWatch uses several article card patterns across site versions
        selectors = [
            ("article", {"class": re.compile(r"article__content|story")}),
            ("div",     {"class": re.compile(r"article__content")}),
            ("div",     {"class": re.compile(r"element--article")}),
        ]

        cards = []
        for tag, attrs in selectors:
            cards = soup.find_all(tag, attrs)
            if cards:
                break

        # Wider fallback — any <a> with a news-looking href
        if not cards:
            all_links = soup.find_all("a", href=re.compile(r"/story/|/articles/|/amp/story/"))
            for link in all_links[:limit]:
                headline = link.get_text(strip=True)
                if len(headline) > 20:
                    articles.append({
                        "source":       "marketwatch",
                        "ticker":       ticker,
                        "headline":     headline,
                        "body_snippet": "",
                        "url":          self._abs(link.get("href", "")),
                        "published_at": None,
                    })
            return articles

        for card in cards[:limit]:
            try:
                headline_el = (
                    card.find("h3") or card.find("h2") or
                    card.find(class_=re.compile(r"headline|title"))
                )
                link_el    = card.find("a", href=True)
                time_el    = card.find("time") or card.find(class_=re.compile(r"timestamp|date"))
                snippet_el = card.find("p") or card.find(class_=re.compile(r"summary|description"))

                if not headline_el:
                    continue
                headline = headline_el.get_text(strip=True)
                if not headline or len(headline) < 10:
                    continue

                url_href     = self._abs(link_el["href"]) if link_el else ""
                published_at = self._parse_time(time_el)
                snippet      = snippet_el.get_text(strip=True)[:300] if snippet_el else ""

                articles.append({
                    "source":       "marketwatch",
                    "ticker":       ticker,
                    "headline":     headline,
                    "body_snippet": snippet,
                    "url":          url_href,
                    "published_at": published_at,
                })
            except Exception as e:
                log.debug(f"[MarketWatch] Card parse error: {e}")

        return articles

    def _parse_search_results(self, soup: BeautifulSoup, ticker: str, limit: int) -> list[dict]:
        """Parse search results page."""
        articles = []
        results  = soup.find_all("div", class_=re.compile(r"element--article|article__content"))

        for item in results[:limit]:
            try:
                h_el = item.find(["h3", "h2"]) or item.find(class_=re.compile(r"headline"))
                a_el = item.find("a", href=True)
                t_el = item.find("time") or item.find(class_=re.compile(r"timestamp"))
                if not h_el:
                    continue
                articles.append({
                    "source":       "marketwatch",
                    "ticker":       ticker,
                    "headline":     h_el.get_text(strip=True),
                    "body_snippet": "",
                    "url":          self._abs(a_el["href"]) if a_el else "",
                    "published_at": self._parse_time(t_el),
                })
            except Exception:
                pass
        return articles

    # ── Economic Calendar ─────────────────────────────────────────────────────

    def fetch_economic_calendar(self) -> list[dict]:
        """
        Scrape the MarketWatch economic calendar.
        Returns a list of upcoming economic events with date, time, name,
        period, consensus estimate, and prior reading.
        """
        events = []
        try:
            soup = self._get(MW_ECON_CAL_URL)
            events = self._parse_calendar(soup)
            log.info(f"[MarketWatch] Economic calendar: {len(events)} events found")
        except Exception as e:
            log.error(f"[MarketWatch] Economic calendar failed: {e}")
        finally:
            time.sleep(self.delay)
        return events

    def _parse_calendar(self, soup: BeautifulSoup) -> list[dict]:
        """
        Parse the economic calendar table from MarketWatch.
        The table structure groups rows by date header then event rows.
        """
        events = []

        # Try the main calendar table
        table = (
            soup.find("table", class_=re.compile(r"calendar|economic")) or
            soup.find("div",   class_=re.compile(r"calendar__table|economicCalendar")) or
            soup.find("table")
        )

        if not table:
            # Fallback: scan all rows in the page for event-like data
            return self._parse_calendar_fallback(soup)

        current_date = None
        rows = table.find_all("tr")

        for row in rows:
            cells = row.find_all(["td", "th"])
            if not cells:
                continue

            row_text = row.get_text(strip=True)

            # Date header row — e.g. "Monday, April 28, 2025"
            if len(cells) == 1 or row.find(class_=re.compile(r"date|day")):
                parsed = self._parse_date_header(row_text)
                if parsed:
                    current_date = parsed
                    continue

            # Event row — needs at least 3 cells (time, name, prior/estimate)
            if len(cells) < 3 or current_date is None:
                continue

            try:
                cell_texts = [c.get_text(strip=True) for c in cells]

                # Identify columns heuristically based on cell count
                if len(cell_texts) >= 6:
                    # Current MarketWatch schema:
                    # Time (ET) | Report | Period | Actual | Forecast | Previous
                    time_str    = cell_texts[0]
                    event_name  = cell_texts[1]
                    period      = cell_texts[2]
                    actual      = cell_texts[3]
                    consensus   = cell_texts[4]
                    prior       = cell_texts[5]
                elif len(cell_texts) == 5:
                    # Defensive fallback when one trailing column is omitted.
                    time_str    = cell_texts[0]
                    event_name  = cell_texts[1]
                    period      = cell_texts[2]
                    actual      = ""
                    consensus   = cell_texts[3]
                    prior       = cell_texts[4]
                elif len(cell_texts) == 4:
                    time_str    = cell_texts[0]
                    event_name  = cell_texts[1]
                    consensus   = cell_texts[2]
                    prior       = cell_texts[3]
                    period      = ""
                    actual      = ""
                else:
                    time_str    = ""
                    event_name  = cell_texts[0]
                    consensus   = cell_texts[-1] if len(cell_texts) > 1 else ""
                    prior       = ""
                    period      = ""
                    actual      = ""

                if not event_name or len(event_name) < 3:
                    continue

                # Skip obvious header rows
                if event_name.lower() in ("event", "release", "indicator", "name"):
                    continue

                event_dt = self._combine_datetime(current_date, time_str)
                category = self._categorise(event_name)
                importance = self._importance(event_name)

                events.append({
                    "date":        current_date,
                    "datetime":    event_dt,
                    "time_str":    time_str,
                    "name":        event_name,
                    "period":      period,
                    "consensus":   consensus,
                    "prior":       prior,
                    "actual":      actual,
                    "category":    category,
                    "importance":  importance,
                    "source":      "marketwatch",
                })
            except Exception as e:
                log.debug(f"[MarketWatch] Calendar row parse error: {e}")

        return events

    def _parse_calendar_fallback(self, soup: BeautifulSoup) -> list[dict]:
        """
        Fallback parser — scans the page for known economic event keywords
        and extracts surrounding context. Used when table structure changes.
        """
        events = []
        HIGH_IMPORTANCE = [
            "FOMC", "Federal Reserve", "Fed Rate", "Interest Rate Decision",
            "CPI", "Consumer Price Index", "PCE", "Personal Consumption",
            "GDP", "Gross Domestic Product", "Nonfarm Payroll", "NFP",
            "Unemployment", "Jobs Report", "Retail Sales", "PPI",
            "Producer Price", "ISM", "PMI", "Housing Starts", "Durable Goods",
            "Trade Balance", "Consumer Confidence", "Treasury", "Jobless Claims",
        ]

        text_blocks = soup.find_all(["div", "li", "tr"],
                                     string=re.compile("|".join(HIGH_IMPORTANCE), re.I))

        seen = set()
        for block in text_blocks:
            name = block.get_text(strip=True)[:100]
            if name in seen or len(name) < 5:
                continue
            seen.add(name)
            category   = self._categorise(name)
            importance = self._importance(name)
            events.append({
                "date":       datetime.now().date(),
                "datetime":   None,
                "time_str":   "",
                "name":       name,
                "period":     "",
                "consensus":  "",
                "prior":      "",
                "actual":     "",
                "category":   category,
                "importance": importance,
                "source":     "marketwatch",
            })

        return events

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _abs(self, href: str) -> str:
        if not href:
            return ""
        if href.startswith("http"):
            return href
        return MW_BASE + href if href.startswith("/") else href

    def _parse_time(self, el) -> datetime | None:
        if el is None:
            return None
        dt_str = el.get("datetime") or el.get("data-est") or el.get_text(strip=True)
        for fmt in (
            "%Y-%m-%dT%H:%M:%S",  "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%dT%H:%M:%S%z", "%B %d, %Y %I:%M %p ET",
            "%b. %d, %Y", "%b %d, %Y", "%m/%d/%Y",
        ):
            try:
                return datetime.strptime(dt_str[:25].strip(), fmt)
            except ValueError:
                pass
        return None

    def _parse_date_header(self, text: str):
        """Parse MarketWatch date headers, including the common 'Monday, Aug. 17' form."""
        text = re.sub(r"\s+", " ", text.strip())
        candidates = (
            ("%A, %B %d, %Y", text),
            ("%A, %b %d, %Y", text.replace(".", "")),
            ("%B %d, %Y", text),
            ("%b %d, %Y", text.replace(".", "")),
            ("%m/%d/%Y", text),
            ("%Y-%m-%d", text),
        )
        for fmt, raw in candidates:
            try:
                return datetime.strptime(raw[:30], fmt).date()
            except ValueError:
                pass

        # MarketWatch currently renders headers like "Monday, Aug. 17" without
        # the year. Infer the year from today's date and handle Dec/Jan rollover.
        raw = text.replace(".", "")
        for fmt in ("%A, %b %d", "%b %d"):
            try:
                parsed = datetime.strptime(raw, fmt)
                now = datetime.now()
                year = now.year
                candidate = parsed.replace(year=year).date()
                # If the inferred date is implausibly far behind/ahead, assume
                # the calendar window crossed a year boundary.
                if (candidate - now.date()).days < -180:
                    candidate = candidate.replace(year=year + 1)
                elif (candidate - now.date()).days > 180:
                    candidate = candidate.replace(year=year - 1)
                return candidate
            except ValueError:
                pass
        return None

    def _combine_datetime(self, date, time_str: str) -> datetime | None:
        """Convert MarketWatch's ET calendar time into naive UTC for persistence."""
        if date is None:
            return None
        cleaned = time_str.strip().upper().replace("ET", "").strip()
        eastern = ZoneInfo("America/New_York")
        for fmt in ("%I:%M %p", "%I:%M%p", "%H:%M"):
            try:
                t = datetime.strptime(cleaned, fmt)
                local_dt = datetime.combine(date, t.time()).replace(tzinfo=eastern)
                return local_dt.astimezone(timezone.utc).replace(tzinfo=None)
            except ValueError:
                pass
        # Date-only events remain at midnight UTC when no reliable time exists.
        try:
            return datetime.combine(date, datetime.min.time())
        except Exception:
            return None

    @staticmethod
    def _categorise(name: str) -> str:
        """Map event name to a broad category."""
        name_lower = name.lower()
        if any(k in name_lower for k in ["fomc", "federal reserve", "fed rate",
                                          "interest rate", "fed chair", "minutes"]):
            return "Fed / Rates"
        if any(k in name_lower for k in ["cpi", "consumer price", "pce",
                                          "personal consumption", "ppi", "producer price",
                                          "inflation"]):
            return "Inflation"
        if any(k in name_lower for k in ["nonfarm", "payroll", "unemployment",
                                          "jobs", "jobless", "labor", "employment",
                                          "adp", "jolts"]):
            return "Labour"
        if any(k in name_lower for k in ["gdp", "gross domestic"]):
            return "GDP"
        if any(k in name_lower for k in ["retail sales", "consumer confidence",
                                          "consumer sentiment", "spending"]):
            return "Consumer"
        if any(k in name_lower for k in ["ism", "pmi", "manufacturing",
                                          "services", "industrial"]):
            return "Manufacturing"
        if any(k in name_lower for k in ["housing", "home sales", "building permits",
                                          "starts", "existing home"]):
            return "Housing"
        if any(k in name_lower for k in ["trade", "balance", "imports", "exports"]):
            return "Trade"
        if any(k in name_lower for k in ["treasury", "auction", "bond", "yield"]):
            return "Treasury"
        return "Other"

    @staticmethod
    def _importance(name: str) -> str:
        """Rate event importance: High / Medium / Low."""
        name_lower = name.lower()
        high = ["fomc", "federal reserve", "interest rate decision", "nonfarm payroll",
                "cpi", "pce", "gdp", "unemployment rate", "fed chair"]
        medium = ["ppi", "retail sales", "ism", "pmi", "consumer confidence",
                  "housing starts", "jobless claims", "adp", "jolts", "durable goods"]
        if any(k in name_lower for k in high):
            return "High"
        if any(k in name_lower for k in medium):
            return "Medium"
        return "Low"
