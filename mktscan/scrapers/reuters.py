"""
mktscan/scrapers/reuters.py
Reuters RSS feed scraper — completely free, no API key or subscription needed.

Reuters publishes structured RSS/Atom feeds across business, technology,
markets, and geopolitics. This scraper:
  1. Pulls the configured feed URLs on every run
  2. Filters articles by ticker keywords to match them to basket companies
  3. Also stores unmatched articles as ticker="MARKET" for the News Feed page
     so geopolitical / macro articles still appear in the dashboard

Free RSS feed URLs used by default:
  - Business / Finance  : feeds.reuters.com/reuters/businessNews
  - Technology          : feeds.reuters.com/reuters/technologyNews
  - Markets             : feeds.reuters.com/reuters/marketsNews (via fallback)
  - Top News            : feeds.reuters.com/reuters/topNews
  - Geopolitics / World : feeds.reuters.com/reuters/worldNews
"""
from __future__ import annotations
import logging
import re
import time
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any
from xml.etree import ElementTree as ET

import requests
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

log = logging.getLogger(__name__)

# Default feed list — all free, no auth required
DEFAULT_FEEDS = [
    "https://feeds.reuters.com/reuters/businessNews",
    "https://feeds.reuters.com/reuters/technologyNews",
    "https://feeds.reuters.com/reuters/topNews",
    "https://feeds.reuters.com/reuters/worldNews",
]

# Fallback feeds if Reuters primary URLs are unavailable (they occasionally rotate)
FALLBACK_FEEDS = [
    "https://feeds.reuters.com/Reuters/worldNews",
    "https://www.reutersagency.com/feed/?taxonomy=best-topics&post_type=best",
]

# Namespaces used in Reuters Atom/RSS feeds
NS = {
    "atom":    "http://www.w3.org/2005/Atom",
    "media":   "http://search.yahoo.com/mrss/",
    "dc":      "http://purl.org/dc/elements/1.1/",
    "reuters": "http://www.reuters.com/",
    "content": "http://purl.org/rss/1.0/modules/content/",
}

# Geopolitical / macro keywords that are always market-relevant
# even when they don't mention a specific ticker
MACRO_KEYWORDS = [
    "federal reserve", "fed rate", "interest rate", "fomc", "inflation", "cpi", "pce",
    "gdp", "recession", "tariff", "trade war", "sanctions", "chip ban", "export control",
    "war", "conflict", "missile", "nato", "ukraine", "russia", "china", "taiwan",
    "opec", "oil price", "crude", "semiconductor", "supply chain", "pandemic", "outbreak",
    "ai", "artificial intelligence", "antitrust", "regulation", "congress", "treasury",
    "dollar", "yuan", "yen", "currency", "debt ceiling", "budget", "deficit",
]


class ReutersScraper:
    """
    Pulls articles from Reuters RSS feeds and matches them to basket companies.

    Articles that mention a ticker's keywords are tagged with that ticker.
    Macro/geopolitical articles that don't match any ticker are tagged
    as ticker='MARKET' so they still surface in the News Feed.
    """

    def __init__(self, cfg: dict[str, Any], delay: float = 2.0, lookback_days: int = 7):
        self.delay       = delay
        self.lookback    = lookback_days
        self.enabled     = cfg.get("enabled", True)

        # Allow custom feed URLs in config, fall back to defaults
        self.feed_urls = cfg.get("feed_urls", DEFAULT_FEEDS)

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent":      "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                               "Chrome/124.0.0.0 Safari/537.36",
            "Accept":          "application/rss+xml, application/xml, text/xml, */*",
            "Accept-Language": "en-US,en;q=0.9",
        })

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(requests.RequestException),
        reraise=True,
    )
    def _fetch_feed(self, url: str) -> list[dict]:
        """Fetch and parse a single RSS/Atom feed URL."""
        r = self.session.get(url, timeout=15)
        r.raise_for_status()
        return self._parse_feed(r.text, url)

    def _parse_feed(self, xml_text: str, source_url: str) -> list[dict]:
        """Parse RSS 2.0 or Atom feed XML into a list of article dicts."""
        articles = []
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as e:
            log.warning(f"[Reuters] XML parse error from {source_url}: {e}")
            return []

        # Detect feed format
        tag = root.tag.lower()
        if "feed" in tag:
            # Atom format
            items = root.findall("{http://www.w3.org/2005/Atom}entry")
            for item in items:
                article = self._parse_atom_entry(item)
                if article:
                    articles.append(article)
        else:
            # RSS 2.0 format — items are inside <channel>
            channel = root.find("channel")
            if channel is None:
                channel = root
            items = channel.findall("item")
            for item in items:
                article = self._parse_rss_item(item)
                if article:
                    articles.append(article)

        return articles

    def _parse_rss_item(self, item: ET.Element) -> dict | None:
        """Parse a single RSS <item> element."""
        try:
            title   = self._text(item, "title")
            link    = self._text(item, "link")
            desc    = self._text(item, "description") or ""
            pub_raw = self._text(item, "pubDate")

            if not title:
                return None

            # Clean HTML tags from description
            desc_clean = re.sub(r"<[^>]+>", " ", desc).strip()
            desc_clean = re.sub(r"\s+", " ", desc_clean)[:400]

            published_at = self._parse_pub_date(pub_raw)

            # Skip articles older than lookback window
            if published_at and self._too_old(published_at):
                return None

            # Reuters-specific category tag
            category = self._text(item, "category") or ""

            return {
                "headline":     title,
                "body_snippet": desc_clean,
                "url":          link or "",
                "published_at": published_at,
                "category":     category,
                "raw_text":     f"{title} {desc_clean}".lower(),
            }
        except Exception as e:
            log.debug(f"[Reuters] RSS item parse error: {e}")
            return None

    def _parse_atom_entry(self, entry: ET.Element) -> dict | None:
        """Parse a single Atom <entry> element."""
        try:
            title_el = entry.find("{http://www.w3.org/2005/Atom}title")
            title    = title_el.text.strip() if title_el is not None and title_el.text else ""

            link_el  = entry.find("{http://www.w3.org/2005/Atom}link")
            link     = link_el.get("href", "") if link_el is not None else ""

            summary_el = entry.find("{http://www.w3.org/2005/Atom}summary")
            summary    = (summary_el.text or "") if summary_el is not None else ""
            summary    = re.sub(r"<[^>]+>", " ", summary).strip()[:400]

            updated_el = (
                entry.find("{http://www.w3.org/2005/Atom}published") or
                entry.find("{http://www.w3.org/2005/Atom}updated")
            )
            pub_raw      = updated_el.text if updated_el is not None else None
            published_at = self._parse_pub_date(pub_raw)

            if not title:
                return None
            if published_at and self._too_old(published_at):
                return None

            return {
                "headline":     title,
                "body_snippet": summary,
                "url":          link,
                "published_at": published_at,
                "category":     "",
                "raw_text":     f"{title} {summary}".lower(),
            }
        except Exception as e:
            log.debug(f"[Reuters] Atom entry parse error: {e}")
            return None

    # ── Public API ────────────────────────────────────────────────────────────

    def fetch_all_feeds(self) -> list[dict]:
        """
        Pull all configured feeds and return de-duplicated raw articles.
        Called once per scraper run — results are then matched per ticker.
        """
        if not self.enabled:
            return []

        all_articles: list[dict] = []
        seen_urls: set[str] = set()

        for url in self.feed_urls:
            try:
                articles = self._fetch_feed(url)
                for a in articles:
                    uid = a.get("url") or a.get("headline", "")
                    if uid and uid not in seen_urls:
                        seen_urls.add(uid)
                        all_articles.append(a)
                log.info(f"[Reuters] {url.split('/')[-1]}: {len(articles)} articles")
                time.sleep(self.delay)
            except Exception as e:
                log.warning(f"[Reuters] Feed failed {url}: {e}")
                # Try fallback feeds if primary fails
                for fallback_url in FALLBACK_FEEDS:
                    try:
                        articles = self._fetch_feed(fallback_url)
                        for a in articles:
                            uid = a.get("url") or a.get("headline", "")
                            if uid and uid not in seen_urls:
                                seen_urls.add(uid)
                                all_articles.append(a)
                        log.info(f"[Reuters] Fallback {fallback_url.split('/')[-1]}: {len(articles)} articles")
                        break
                    except Exception:
                        continue

        log.info(f"[Reuters] Total unique articles fetched: {len(all_articles)}")
        return all_articles

    def match_to_ticker(
        self,
        all_articles: list[dict],
        ticker: str,
        keywords: list[str],
    ) -> list[dict]:
        """
        Filter pre-fetched articles to those relevant to a specific ticker.
        Matching is keyword-based: any article whose title+snippet contains
        the ticker symbol or any of its configured keywords is included.
        """
        matched = []
        search_terms = [ticker.lower()] + [k.lower() for k in keywords]

        for article in all_articles:
            raw = article.get("raw_text", "")
            if any(term in raw for term in search_terms):
                matched.append({
                    "source":       "reuters",
                    "ticker":       ticker,
                    "headline":     article["headline"],
                    "body_snippet": article["body_snippet"],
                    "url":          article["url"],
                    "published_at": article["published_at"],
                })

        return matched

    def get_macro_articles(self, all_articles: list[dict]) -> list[dict]:
        """
        Return articles that are macro/geopolitical in nature but don't
        match any specific ticker. Tagged as ticker='MARKET' so they
        appear in the News Feed without being tied to a company.
        """
        macro = []
        for article in all_articles:
            raw = article.get("raw_text", "")
            if any(kw in raw for kw in MACRO_KEYWORDS):
                macro.append({
                    "source":       "reuters",
                    "ticker":       "MARKET",
                    "headline":     article["headline"],
                    "body_snippet": article["body_snippet"],
                    "url":          article["url"],
                    "published_at": article["published_at"],
                })
        return macro

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _text(self, el: ET.Element, tag: str) -> str:
        child = el.find(tag)
        if child is not None and child.text:
            return child.text.strip()
        return ""

    def _parse_pub_date(self, raw: str | None) -> datetime | None:
        if not raw:
            return None
        raw = raw.strip()

        # RFC 2822 (standard RSS pubDate)
        try:
            return parsedate_to_datetime(raw).replace(tzinfo=None)
        except Exception:
            pass

        # ISO 8601 variants (Atom)
        for fmt in (
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%dT%H:%M:%S.%fZ",
            "%Y-%m-%d",
        ):
            try:
                return datetime.strptime(raw[:25], fmt)
            except ValueError:
                pass

        return None

    def _too_old(self, pub: datetime) -> bool:
        from datetime import timedelta
        cutoff = datetime.utcnow() - timedelta(days=self.lookback)
        return pub < cutoff
