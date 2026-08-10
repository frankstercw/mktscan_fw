"""
tests/test_scrapers.py
Unit tests for scraper modules using mocked HTTP responses.
Run: pytest tests/ -v
"""
from __future__ import annotations
import json
from datetime import datetime
from unittest.mock import MagicMock, patch, PropertyMock

import pytest


# ── Yahoo Finance ──────────────────────────────────────────────────────────────

class TestYahooScraper:
    def setup_method(self):
        from mktscan.scrapers.yahoo import YahooScraper
        self.scraper = YahooScraper({
            "fetch_prices": True,
            "fetch_news": True,
            "fetch_earnings": True,
        })

    def test_parse_prices(self):
        info = {
            "regularMarketPrice": 195.50,
            "regularMarketChangePercent": 1.23,
            "regularMarketVolume": 55_000_000,
            "marketCap": 3_000_000_000_000,
            "trailingPE": 29.5,
            "fiftyTwoWeekHigh": 220.0,
            "fiftyTwoWeekLow": 165.0,
            "recommendationKey": "buy",
        }
        result = self.scraper._parse_prices("AAPL", info)
        assert result["ticker"] == "AAPL"
        assert result["price"] == 195.50
        assert result["change_pct"] == 1.23
        assert result["analyst_rating"] == "buy"
        assert result["week_52_high"] == 220.0

    def test_parse_news_modern_format(self):
        """Test parsing the newer yfinance news format with content dict."""
        raw_news = [{
            "content": {
                "title": "Apple beats Q4 expectations",
                "canonicalUrl": {"url": "https://example.com/apple-q4"},
                "pubDate": "2024-11-01T20:00:00Z",
                "summary": "Apple reported record revenue...",
            }
        }]
        articles = self.scraper._parse_news("AAPL", raw_news)
        assert len(articles) == 1
        assert articles[0]["headline"] == "Apple beats Q4 expectations"
        assert articles[0]["source"] == "yahoo"
        assert articles[0]["ticker"] == "AAPL"

    def test_parse_news_legacy_format(self):
        """Test parsing older yfinance format with top-level fields."""
        raw_news = [{
            "title": "Apple launches new product",
            "link": "https://example.com/apple-new",
            "providerPublishTime": 1700000000,
        }]
        articles = self.scraper._parse_news("AAPL", raw_news)
        assert len(articles) == 1
        assert articles[0]["headline"] == "Apple launches new product"
        assert isinstance(articles[0]["published_at"], datetime)

    def test_parse_news_skips_empty_titles(self):
        raw_news = [{"content": {"title": "", "pubDate": "2024-01-01T00:00:00Z"}}]
        articles = self.scraper._parse_news("AAPL", raw_news)
        assert len(articles) == 0

    @patch("mktscan.scrapers.yahoo.yf")
    def test_fetch_ticker_integration(self, mock_yf):
        mock_ticker = MagicMock()
        mock_ticker.info = {"regularMarketPrice": 150.0, "marketCap": 2e12}
        mock_ticker.news = []
        mock_ticker.calendar = None
        mock_ticker.earnings_history = None
        mock_yf.Ticker.return_value = mock_ticker

        result = self.scraper.fetch_ticker("AAPL")
        assert result["ticker"] == "AAPL"
        assert result["prices"]["price"] == 150.0
        assert result["news"] == []

    @patch("mktscan.scrapers.yahoo.yf")
    def test_fetch_ticker_handles_error(self, mock_yf):
        mock_yf.Ticker.side_effect = Exception("Network error")
        result = self.scraper.fetch_ticker("AAPL")
        # Should return empty result, not raise
        assert result["ticker"] == "AAPL"
        assert result["prices"] is None


# ── Alpha Vantage ──────────────────────────────────────────────────────────────

class TestAlphaVantageScraper:
    def setup_method(self):
        from mktscan.scrapers.alphavantage import AlphaVantageScraper
        self.scraper = AlphaVantageScraper(
            {"api_key": "TEST_KEY", "base_url": "https://www.alphavantage.co/query"},
            delay=0,
        )

    @patch("requests.Session.get")
    def test_fetch_quote(self, mock_get):
        mock_get.return_value.json.return_value = {
            "Global Quote": {
                "05. price": "195.50",
                "10. change percent": "1.23%",
                "06. volume": "55000000",
            }
        }
        mock_get.return_value.raise_for_status = MagicMock()

        result = self.scraper.fetch_quote("AAPL")
        assert result is not None
        assert result["price"] == 195.50
        assert result["change_pct"] == 1.23
        assert result["volume"] == 55_000_000

    @patch("requests.Session.get")
    def test_fetch_quote_handles_rate_limit(self, mock_get):
        mock_get.return_value.json.return_value = {
            "Information": "API rate limit reached."
        }
        mock_get.return_value.raise_for_status = MagicMock()

        result = self.scraper.fetch_quote("AAPL")
        assert result is None  # Should handle gracefully

    @patch("requests.Session.get")
    def test_fetch_earnings(self, mock_get):
        mock_get.return_value.json.return_value = {
            "quarterlyEarnings": [
                {
                    "fiscalDateEnding": "2024-09-30",
                    "reportedDate":     "2024-10-31",
                    "reportedEPS":      "1.64",
                    "estimatedEPS":     "1.60",
                    "surprisePercentage": "2.5",
                }
            ]
        }
        mock_get.return_value.raise_for_status = MagicMock()

        results = self.scraper.fetch_earnings("AAPL")
        assert len(results) == 1
        assert results[0]["eps_actual"] == 1.64
        assert results[0]["eps_estimate"] == 1.60
        assert results[0]["surprise_pct"] == 2.5
        assert results[0]["source"] == "alphav"

    @patch("requests.Session.get")
    def test_fetch_news_filters_low_relevance(self, mock_get):
        mock_get.return_value.json.return_value = {
            "feed": [
                {
                    "title": "Relevant article",
                    "time_published": "20241101T120000",
                    "ticker_sentiment": [{"ticker": "AAPL", "relevance_score": "0.9"}],
                },
                {
                    "title": "Irrelevant article",
                    "time_published": "20241101T120000",
                    "ticker_sentiment": [{"ticker": "AAPL", "relevance_score": "0.05"}],
                },
            ]
        }
        mock_get.return_value.raise_for_status = MagicMock()

        results = self.scraper.fetch_news("AAPL")
        # Only the high-relevance article should be included
        assert len(results) == 1
        assert results[0]["headline"] == "Relevant article"

    def test_safe_float(self):
        from mktscan.scrapers.alphavantage import AlphaVantageScraper
        assert AlphaVantageScraper._safe_float("1.64") == 1.64
        assert AlphaVantageScraper._safe_float("None") is None
        assert AlphaVantageScraper._safe_float(None) is None
        assert AlphaVantageScraper._safe_float("0") is None  # treats 0 as None


# ── Benzinga ───────────────────────────────────────────────────────────────────

class TestBenzingaScraper:
    def setup_method(self):
        from mktscan.scrapers.benzinga import BenzingaScraper
        self.scraper = BenzingaScraper(
            {"api_key": "TEST_KEY", "base_url": "https://api.benzinga.com/api/v2"},
            delay=0,
            lookback_days=7,
        )

    @patch("requests.Session.get")
    def test_fetch_news(self, mock_get):
        mock_get.return_value.json.return_value = [
            {
                "title":   "NVDA Beats Estimates",
                "teaser":  "NVIDIA reported record earnings...",
                "url":     "https://benzinga.com/nvda-q3",
                "created": "2024-11-20T18:00:00",
            }
        ]
        mock_get.return_value.raise_for_status = MagicMock()

        results = self.scraper.fetch_news("NVDA")
        assert len(results) == 1
        assert results[0]["headline"] == "NVDA Beats Estimates"
        assert results[0]["source"] == "benzinga"
        assert results[0]["ticker"] == "NVDA"

    @patch("requests.Session.get")
    def test_fetch_earnings(self, mock_get):
        mock_get.return_value.json.return_value = {
            "earnings": [{
                "date":                "2024-11-20",
                "period":              "Q3 2024",
                "eps_est":             "0.74",
                "eps":                 "0.81",
                "revenue_est":         "32500000000",
                "revenue":             "35100000000",
                "eps_surprise_percent":"9.46",
            }]
        }
        mock_get.return_value.raise_for_status = MagicMock()

        results = self.scraper.fetch_earnings("NVDA")
        assert len(results) == 1
        assert results[0]["eps_estimate"] == 0.74
        assert results[0]["eps_actual"] == 0.81
        assert results[0]["surprise_pct"] == 9.46

    def test_parse_date(self):
        from mktscan.scrapers.benzinga import BenzingaScraper
        dt = BenzingaScraper._parse_date("2024-11-20T18:30:00")
        assert dt.year == 2024
        assert dt.month == 11
        assert dt.day == 20

        assert BenzingaScraper._parse_date("") is None
        assert BenzingaScraper._parse_date(None) is None


# ── FinViz ─────────────────────────────────────────────────────────────────────

class TestFinVizScraper:
    def setup_method(self):
        from mktscan.scrapers.finviz import FinVizScraper
        # No session cookie → public mode
        self.scraper = FinVizScraper({"session_cookie": ""}, delay=0)

    def test_initialises_public_mode(self):
        assert not self.scraper.elite
        assert self.scraper.base == "https://finviz.com"

    def test_initialises_elite_mode(self):
        from mktscan.scrapers.finviz import FinVizScraper
        scraper = FinVizScraper({"session_cookie": "real_cookie_value"}, delay=0)
        assert scraper.elite
        assert scraper.base == "https://elite.finviz.com"

    @patch("requests.Session.get")
    def test_fetch_news_parses_table(self, mock_get):
        # Minimal HTML mimicking FinViz news table structure
        html = """
        <html><body>
        <table id="news-table">
          <tr>
            <td>May-01-24 09:00AM</td>
            <td><a href="https://example.com/1">Apple hits new high on earnings beat</a>
              <span class="news-link-right">Reuters</span></td>
          </tr>
          <tr>
            <td>09:30AM</td>
            <td><a href="https://example.com/2">iPhone demand remains strong in Q3</a>
              <span class="news-link-right">Bloomberg</span></td>
          </tr>
        </table>
        </body></html>
        """
        mock_get.return_value.text = html
        mock_get.return_value.status_code = 200
        mock_get.return_value.raise_for_status = MagicMock()

        articles = self.scraper.fetch_news("AAPL")
        assert len(articles) == 2
        assert articles[0]["headline"] == "Apple hits new high on earnings beat"
        assert articles[0]["source"] == "finviz"

    @patch("requests.Session.get")
    def test_fetch_news_empty_table(self, mock_get):
        mock_get.return_value.text = "<html><body><p>No news</p></body></html>"
        mock_get.return_value.status_code = 200
        mock_get.return_value.raise_for_status = MagicMock()

        articles = self.scraper.fetch_news("AAPL")
        assert articles == []


# ── Sentiment Engine ───────────────────────────────────────────────────────────

class TestSentiment:
    # NOTE: thresholds are now symmetric at ±0.20. They used to be +0.3 / -0.1,
    # which labelled a -0.15 reading BEARISH while its mirror image at +0.15 was
    # NEUTRAL — a systematic bearish bias with no stated rationale that
    # propagated into every "how many tickers are bearish" count.
    def test_classify_score_bullish(self):
        from mktscan.sentiment import classify_score
        assert classify_score(0.5)  == "BULLISH"
        assert classify_score(0.31) == "BULLISH"
        assert classify_score(0.20) == "BULLISH"

    def test_classify_score_bearish(self):
        from mktscan.sentiment import classify_score
        assert classify_score(-0.5)  == "BEARISH"
        assert classify_score(-0.21) == "BEARISH"
        assert classify_score(-0.20) == "BEARISH"

    def test_classify_score_neutral(self):
        from mktscan.sentiment import classify_score
        assert classify_score(0.0)   == "NEUTRAL"
        assert classify_score(0.1)   == "NEUTRAL"
        assert classify_score(-0.05) == "NEUTRAL"
        assert classify_score(-0.11) == "NEUTRAL"   # was BEARISH under the old band

    def test_classify_score_is_symmetric(self):
        """A reading and its mirror image must never get opposite-signed labels."""
        from mktscan.sentiment import classify_score
        for value in (0.05, 0.15, 0.19, 0.25, 0.6):
            pos, neg = classify_score(value), classify_score(-value)
            assert {pos, neg} in ({"NEUTRAL"}, {"BULLISH", "BEARISH"})

    def test_vader_scorer(self):
        pytest.importorskip("vaderSentiment")
        from mktscan.sentiment import VADERScorer
        scorer = VADERScorer()

        positive = scorer.score_text("Incredible earnings surge, profits soar, analysts thrilled")
        negative = scorer.score_text("Terrible miss, catastrophic losses, stock crashes badly")
        neutral  = scorer.score_text("The company released its quarterly report on Thursday")

        assert positive > 0, f"Expected positive > 0, got {positive}"
        assert negative < 0, f"Expected negative < 0, got {negative}"
        assert -0.5 < neutral < 0.5

    def test_vader_batch(self):
        pytest.importorskip("vaderSentiment")
        from mktscan.sentiment import VADERScorer
        scorer = VADERScorer()
        texts = [
            "Outstanding results, record profits",
            "Disappointing miss, guidance cut",
            "Quarterly earnings release",
        ]
        scores = scorer.score_batch(texts)
        assert len(scores) == 3
        assert scores[0] > scores[1]

    def test_aggregate_scores_empty(self):
        from mktscan.sentiment import aggregate_scores, VADERScorer
        result = aggregate_scores([], VADERScorer())
        assert result["score"] == 0.0
        assert result["label"] == "NEUTRAL"
        assert result["article_count"] == 0

    def test_aggregate_scores_with_articles(self):
        pytest.importorskip("vaderSentiment")
        from mktscan.sentiment import aggregate_scores, VADERScorer
        articles = [
            {"source": "yahoo",    "headline": "Record earnings beat", "body_snippet": ""},
            {"source": "benzinga", "headline": "Strong guidance raise", "body_snippet": ""},
            {"source": "finviz",   "headline": "Analyst upgrades stock", "body_snippet": ""},
        ]
        result = aggregate_scores(articles, VADERScorer())
        assert result["article_count"] == 3
        assert result["score"] != 0.0
        assert result["label"] in ("BULLISH", "NEUTRAL", "BEARISH")
        assert "yahoo" in result["source_breakdown"]

    def test_aggregate_respects_source_weights(self):
        pytest.importorskip("vaderSentiment")
        from mktscan.sentiment import aggregate_scores, VADERScorer

        articles = [
            {"source": "wsj",   "headline": "Incredible surge, profits soar, outstanding results", "body_snippet": ""},
            {"source": "yahoo", "headline": "Terrible crash, devastating losses, catastrophic miss",  "body_snippet": ""},
        ]
        # WSJ weighted 10x — its strong positive score should dominate
        result_weighted = aggregate_scores(
            articles, VADERScorer(), source_weights={"wsj": 10.0, "yahoo": 1.0}
        )
        result_equal = aggregate_scores(
            [{"source": "wsj",   "headline": "Incredible surge, profits soar, outstanding results", "body_snippet": ""},
             {"source": "yahoo", "headline": "Terrible crash, devastating losses, catastrophic miss",  "body_snippet": ""}],
            VADERScorer()
        )

        # Weighted result should be pulled more toward WSJ's positive sentiment
        assert result_weighted["score"] > result_equal["score"], (
            f"Weighted {result_weighted['score']:.4f} should be > equal {result_equal['score']:.4f}"
        )

    def test_clean_text(self):
        from mktscan.sentiment import _clean_text
        dirty = "<b>Apple</b> reports <i>record</i>  quarterly  earnings"
        clean = _clean_text(dirty)
        assert "<b>" not in clean
        assert "  " not in clean
        assert "Apple" in clean

    def test_build_scorer_finbert(self):
        from mktscan.sentiment import build_scorer, FinBERTScorer
        scorer = build_scorer({"model": "finbert"})
        assert isinstance(scorer, FinBERTScorer)

    def test_build_scorer_vader(self):
        from mktscan.sentiment import build_scorer, VADERScorer
        scorer = build_scorer({"model": "vader"})
        assert isinstance(scorer, VADERScorer)

    def test_build_scorer_unknown_raises(self):
        from mktscan.sentiment import build_scorer
        with pytest.raises(ValueError, match="Unknown sentiment model"):
            build_scorer({"model": "gpt-99"})

    def test_build_scorer_openai_no_key_raises(self):
        from mktscan.sentiment import build_scorer
        with pytest.raises(ValueError, match="OpenAI API key"):
            build_scorer({"model": "openai", "openai_api_key": "YOUR_KEY"})


# ── Database ───────────────────────────────────────────────────────────────────

class TestDatabase:
    def setup_method(self):
        """Use an in-memory SQLite DB for each test."""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from mktscan.database import Base, get_engine
        import mktscan.database as db_module

        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        db_module._engine = engine
        db_module._Session = sessionmaker(bind=engine)

    def test_seed_default_basket(self):
        from mktscan.database import get_session, get_basket, seed_default_basket
        session = get_session()
        seed_default_basket(session)
        companies = get_basket(session)
        session.close()
        assert len(companies) == 19
        tickers = [c.ticker for c in companies]
        assert "AAPL" in tickers
        assert "NVDA" in tickers
        assert "PLTR" in tickers
        assert "PANW" in tickers
        assert "CELH" in tickers

    def test_seed_is_idempotent(self):
        from mktscan.database import get_session, get_basket, seed_default_basket
        session = get_session()
        seed_default_basket(session)
        seed_default_basket(session)  # calling twice should not duplicate
        companies = get_basket(session)
        session.close()
        assert len(companies) == 19

    def test_upsert_company_creates(self):
        from mktscan.database import get_session, upsert_company, get_basket
        session = get_session()
        upsert_company(session, "TSMC", "Taiwan Semiconductor", "Semiconductors", "TSMC, chips")
        companies = get_basket(session)
        session.close()
        assert any(c.ticker == "TSMC" for c in companies)

    def test_upsert_company_updates(self):
        from mktscan.database import get_session, upsert_company, Company
        from sqlalchemy import select
        session = get_session()
        upsert_company(session, "TEST", "Test Co", "Tech", "test")
        upsert_company(session, "TEST", "Test Co Updated", "Finance", "test, updated")
        company = session.execute(select(Company).where(Company.ticker == "TEST")).scalar_one()
        session.close()
        assert company.name == "Test Co Updated"
        assert company.sector == "Finance"

    def test_company_keyword_list(self):
        from mktscan.database import Company
        c = Company(ticker="AAPL", name="Apple", keywords="Apple, iPhone, Tim Cook")
        kws = c.keyword_list()
        assert "Apple" in kws
        assert "iPhone" in kws
        assert "Tim Cook" in kws

    def test_company_keyword_list_fallback(self):
        from mktscan.database import Company
        c = Company(ticker="AAPL", name="Apple", keywords=None)
        assert c.keyword_list() == ["AAPL"]

    def test_save_and_retrieve_sentiment(self):
        from mktscan.database import get_session, SentimentScore, get_latest_scores
        session = get_session()
        score = SentimentScore(
            run_id=1, ticker="AAPL", score=0.72,
            label="BULLISH", article_count=25,
            source_breakdown='{"yahoo":15,"benzinga":10}',
        )
        session.add(score)
        session.commit()

        latest = get_latest_scores(session)
        session.close()
        assert len(latest) == 1
        assert latest[0].ticker == "AAPL"
        assert abs(latest[0].score - 0.72) < 0.001

    def test_article_deduplication_by_url(self):
        """The engine should not insert duplicate URLs."""
        from mktscan.database import get_session, Article
        from sqlalchemy import select
        session = get_session()

        a1 = Article(source="yahoo", ticker="AAPL",
                     headline="Apple earnings",   url="https://example.com/1")
        a2 = Article(source="yahoo", ticker="AAPL",
                     headline="Apple earnings v2", url="https://example.com/1")
        session.add(a1)
        session.commit()

        # Try inserting duplicate URL — should be caught by unique constraint
        from sqlalchemy.exc import IntegrityError
        with pytest.raises((IntegrityError, Exception)):
            session.add(a2)
            session.commit()

        session.rollback()
        count = session.execute(select(Article)).scalars().all()
        session.close()
        assert len(count) == 1


# ── Config ─────────────────────────────────────────────────────────────────────

class TestConfig:
    def test_get_dotted_path(self, tmp_path):
        import yaml
        from mktscan import config as cfg_module
        cfg_module._CONFIG = None

        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(yaml.dump({
            "scraper": {"delay_seconds": 3.0},
            "sources": {"benzinga": {"api_key": "bz-test"}},
        }))
        cfg_module.load_config(str(cfg_file))

        assert cfg_module.get("scraper.delay_seconds") == 3.0
        assert cfg_module.get("sources.benzinga.api_key") == "bz-test"
        assert cfg_module.get("nonexistent.key", "default") == "default"
        cfg_module._CONFIG = None

    def test_env_override(self, tmp_path, monkeypatch):
        import yaml
        from mktscan import config as cfg_module
        cfg_module._CONFIG = None

        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(yaml.dump({"sources": {"benzinga": {"api_key": "original"}}}))
        monkeypatch.setenv("MKTSCAN_BENZINGA_KEY", "from-env-override")

        cfg_module.load_config(str(cfg_file))
        assert cfg_module.get("sources.benzinga.api_key") == "from-env-override"
        cfg_module._CONFIG = None
