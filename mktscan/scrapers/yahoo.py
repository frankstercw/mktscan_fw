"""
mktscan/scrapers/yahoo.py
Yahoo Finance scraper using the yfinance library.
No API key required. Covers: prices, news headlines, earnings.
"""
from __future__ import annotations
import logging
import math
from datetime import date as _date, datetime
from typing import Any

log = logging.getLogger(__name__)


def _safe_float(value: Any) -> float | None:
    """Coerce to float, mapping NaN/None/non-numeric to None."""
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(out) else out

try:
    import yfinance as yf
    YF_AVAILABLE = True
except ImportError:
    YF_AVAILABLE = False
    log.warning("yfinance not installed. Run: pip install yfinance")


class YahooScraper:
    """
    Fetches price data, news, and earnings info from Yahoo Finance
    via the yfinance Python library.
    """

    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        self.fetch_prices   = cfg.get("fetch_prices", True)
        self.fetch_news     = cfg.get("fetch_news", True)
        self.fetch_earnings = cfg.get("fetch_earnings", True)

    def fetch_ticker(self, ticker: str) -> dict[str, Any]:
        """
        Returns a dict with keys: prices, news, earnings
        Each may be None if fetching was disabled or failed.
        """
        if not YF_AVAILABLE:
            raise RuntimeError("yfinance is not installed.")

        result: dict[str, Any] = {
            "ticker": ticker,
            "prices": None,
            "news": [],
            "earnings": None,
        }

        try:
            t = yf.Ticker(ticker)
            info = t.info or {}

            if self.fetch_prices:
                result["prices"] = self._parse_prices(ticker, info)

            if self.fetch_news:
                raw_news = t.news or []
                result["news"] = self._parse_news(ticker, raw_news)

            if self.fetch_earnings:
                result["earnings"] = self._parse_earnings(ticker, t)

        except Exception as e:
            log.error(f"[Yahoo] Error fetching {ticker}: {e}")

        return result

    def _parse_prices(self, ticker: str, info: dict) -> dict:
        # Volume anomaly: ratio of today's volume to 30-day average
        volume     = info.get("regularMarketVolume")
        avg_volume = info.get("averageDailyVolume30Day") or info.get("averageVolume")
        volume_ratio = round(volume / avg_volume, 3) if volume and avg_volume and avg_volume > 0 else None

        return {
            "ticker":              ticker,
            "price":               info.get("regularMarketPrice") or info.get("currentPrice"),
            "change_pct":          info.get("regularMarketChangePercent"),
            "volume":              volume,
            "avg_volume_30d":      avg_volume,
            "volume_ratio":        volume_ratio,       # >1.5 = unusual volume
            "market_cap":          info.get("marketCap"),
            "pe_ratio":            info.get("trailingPE"),
            "week_52_high":        info.get("fiftyTwoWeekHigh"),
            "week_52_low":         info.get("fiftyTwoWeekLow"),
            "analyst_rating":      info.get("recommendationKey"),
            "analyst_mean_score":  info.get("recommendationMean"),  # 1=strong buy, 5=sell
            "target_price":        info.get("targetMeanPrice"),      # analyst consensus target
            "short_ratio":         info.get("shortRatio"),           # days to cover
            "short_pct_float":     info.get("shortPercentOfFloat"),  # % of float shorted
            # NOTE: info["impliedVolatility"] is essentially never populated for
            # equities — relying on it is why the options-IV signal always came
            # back empty. Real ATM IV now comes from the option chain via
            # iv_rank.update_iv_snapshot() and is attached by the engine.
            "implied_volatility":  None,
            "beta":                info.get("beta"),                  # market sensitivity
        }

    def _parse_news(self, ticker: str, raw_news: list) -> list[dict]:
        articles = []
        for item in raw_news:
            try:
                content = item.get("content") or {}
                title = (
                    content.get("title")
                    or item.get("title")
                    or ""
                )
                url = (
                    content.get("canonicalUrl", {}).get("url")
                    or item.get("link")
                    or ""
                )
                # Published time
                pub_raw = (
                    content.get("pubDate")
                    or content.get("displayTime")
                    or item.get("providerPublishTime")
                )
                published_at = None
                if isinstance(pub_raw, int):
                    published_at = datetime.utcfromtimestamp(pub_raw)
                elif isinstance(pub_raw, str):
                    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%d"):
                        try:
                            published_at = datetime.strptime(pub_raw, fmt)
                            break
                        except ValueError:
                            pass

                if title:
                    articles.append({
                        "source":       "yahoo",
                        "ticker":       ticker,
                        "headline":     title,
                        "body_snippet": content.get("summary") or item.get("summary", ""),
                        "url":          url,
                        "published_at": published_at,
                    })
            except Exception as e:
                log.debug(f"[Yahoo] Skipping malformed news item: {e}")
        return articles

    @staticmethod
    def _coerce_date(value: Any) -> datetime | None:
        """Normalise the assorted date shapes yfinance returns into a datetime."""
        if value is None:
            return None
        if isinstance(value, list):
            value = value[0] if value else None
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.replace(tzinfo=None)
        if isinstance(value, _date):
            return datetime(value.year, value.month, value.day)
        if hasattr(value, "to_pydatetime"):          # pandas Timestamp
            try:
                return value.to_pydatetime().replace(tzinfo=None)
            except Exception:
                return None
        if isinstance(value, str):
            for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
                try:
                    return datetime.strptime(value[:len(fmt) + 2].strip(), fmt)
                except ValueError:
                    continue
        return None

    @staticmethod
    def _surprise_pct(actual: Any, estimate: Any, difference: Any) -> float | None:
        """
        Convert an EPS beat/miss into a true percentage.

        yfinance's ``epsDifference`` is an absolute dollar amount (actual minus
        estimate), but it was being stored straight into ``surprise_pct`` and then
        consumed as a percentage — ``surprise_pct / 10.0`` in the fundamental
        signal and ``/ 15.0`` in the event signal. A $0.05 beat therefore read as
        a 0.5% surprise, which pinned both categories near zero for every ticker.

        Percentage surprise is (actual - estimate) / |estimate| * 100. Guarded
        against a near-zero estimate, where the percentage explodes and is not
        meaningful.
        """
        try:
            if actual is None or estimate is None:
                if difference is None or estimate in (None, 0):
                    return None
                actual = float(estimate) + float(difference)
            actual   = float(actual)
            estimate = float(estimate)
        except (TypeError, ValueError):
            return None

        if math.isnan(actual) or math.isnan(estimate):
            return None
        # Below a penny the denominator makes the ratio meaningless.
        if abs(estimate) < 0.01:
            return None

        pct = (actual - estimate) / abs(estimate) * 100.0
        # Clamp: a 4000% "surprise" off a $0.01 estimate is noise, not signal.
        return round(max(-200.0, min(200.0, pct)), 4)

    def fetch_earnings_calendar(self, ticker: str) -> list[dict]:
        """Fetch only the earnings calendar/history for a ticker.

        This is intentionally lighter than ``fetch_ticker`` so the Key Events
        page can refresh upcoming earnings without also pulling news/prices.
        """
        try:
            import yfinance as yf
            t = yf.Ticker(ticker)
            return self._parse_earnings(ticker, t)
        except Exception as exc:
            log.debug(f"[Yahoo] Earnings calendar fetch failed for {ticker}: {exc}")
            return []

    def _parse_earnings(self, ticker: str, t: Any) -> list[dict]:
        events = []
        try:
            # ── Upcoming earnings date ────────────────────────────────────────
            cal = t.calendar
            if cal is not None:
                if hasattr(cal, "to_dict"):
                    cal = cal.to_dict()
                earn_date = self._coerce_date(cal.get("Earnings Date"))
                if earn_date:
                    # The period key used to be the literal string "Upcoming".
                    # Since _save_earnings deduplicates on (ticker, period) and
                    # skips on match, the first date ever fetched was frozen
                    # forever — and days_to_earnings, which drives the "avoid
                    # within 3 days of earnings" guardrail, ran on stale data.
                    # Keying on the actual date makes each report a distinct row,
                    # and the engine now upserts so a rescheduled date updates.
                    events.append({
                        "ticker":       ticker,
                        "report_date":  earn_date,
                        "period":       f"UPCOMING-{earn_date.date().isoformat()}",
                        "is_upcoming":  True,
                        "eps_estimate": _safe_float(cal.get("Earnings Average")),
                        "source":       "yahoo",
                    })
        except Exception as e:
            log.debug(f"[Yahoo] Calendar fetch failed for {ticker}: {e}")

        try:
            history = t.earnings_history
            if history is not None and not history.empty:
                for idx, row in history.iterrows():
                    estimate = _safe_float(row.get("epsEstimate"))
                    actual   = _safe_float(row.get("epsActual"))
                    diff     = _safe_float(row.get("epsDifference"))

                    # earnings_history is indexed by the report date. Leaving
                    # report_date NULL meant calc_event_driven_signal's
                    # "most recent past result" lookup filtered every row out.
                    report_date = self._coerce_date(idx)
                    period      = str(row.get("period") or "").strip()
                    if not period:
                        period = (f"Q-{report_date.date().isoformat()}"
                                  if report_date else "")
                    if not period:
                        continue

                    events.append({
                        "ticker":       ticker,
                        "period":       period,
                        "report_date":  report_date,
                        "eps_estimate": estimate,
                        "eps_actual":   actual,
                        "surprise_pct": self._surprise_pct(actual, estimate, diff),
                        "is_upcoming":  False,
                        "source":       "yahoo",
                    })
        except Exception as e:
            log.debug(f"[Yahoo] Earnings history failed for {ticker}: {e}")

        return events
