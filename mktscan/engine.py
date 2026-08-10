"""
mktscan/engine.py
Main scraper orchestration engine.
Coordinates all sources, persists data, triggers sentiment scoring.
"""
from __future__ import annotations
import json
import logging
import time
from datetime import datetime, timedelta
from typing import Any, Callable

from .config import get_config
from .database import (
    init_db, get_session, get_basket,
    Article, SentimentScore, PriceSnapshot, EarningsEvent, ScraperRun
)
from .scrapers import (
    YahooScraper, AlphaVantageScraper, BenzingaScraper,
    FinVizScraper, WSJScraper, MarketWatchScraper, ReutersScraper,
    FinnhubScraper
)
from .sentiment import build_scorer, aggregate_scores
from . import alerts as alert_module
from . import feedback as feedback_module

log = logging.getLogger(__name__)


class ScrapeEngine:
    """
    Orchestrates a full scrape + sentiment run.
    """

    def __init__(
        self,
        cfg: dict[str, Any] | None = None,
        progress_cb: Callable[[str, str], None] | None = None,
    ):
        self.cfg         = cfg or get_config()
        self.progress_cb = progress_cb or (lambda level, msg: log.info(msg))

        scraper_cfg = self.cfg.get("scraper", {})
        self.delay         = float(scraper_cfg.get("delay_seconds", 2.5))
        self.max_articles  = int(scraper_cfg.get("max_articles_per_source", 50))
        self.lookback_days = int(scraper_cfg.get("lookback_days", 7))

        sources = self.cfg.get("sources", {})

        self.yahoo  = YahooScraper(sources.get("yahoo_finance", {})) \
            if sources.get("yahoo_finance", {}).get("enabled") else None
        self.av     = AlphaVantageScraper(sources.get("alpha_vantage", {}), self.delay) \
            if sources.get("alpha_vantage", {}).get("enabled") else None
        self.bz     = BenzingaScraper(sources.get("benzinga", {}), self.delay, self.lookback_days) \
            if sources.get("benzinga", {}).get("enabled") else None
        self.fv     = FinVizScraper(sources.get("finviz", {}), self.delay) \
            if sources.get("finviz", {}).get("enabled") else None
        self.wsj    = WSJScraper(sources.get("wsj", {}), self.delay, self.lookback_days) \
            if sources.get("wsj", {}).get("enabled") else None
        self.mw      = MarketWatchScraper(sources.get("marketwatch", {}), self.delay, self.lookback_days) \
            if sources.get("marketwatch", {}).get("enabled", True) else None
        self.reuters = ReutersScraper(sources.get("reuters", {}), self.delay, self.lookback_days) \
            if sources.get("reuters", {}).get("enabled", True) else None
        self._reuters_cache: list | None = None  # fetch feeds once, match per ticker
        import os as _os
        # Enable Finnhub if env var is set, even without config change
        _fh_cfg = sources.get("finnhub", {})
        _fh_key = _os.environ.get("FINNHUB_API_KEY") or _fh_cfg.get("api_key", "")
        _fh_enabled = bool(_fh_key and _fh_key != "YOUR_FINNHUB_KEY")
        self.finnhub = FinnhubScraper(_fh_cfg, self.delay, self.lookback_days)             if _fh_enabled else None
        self._finnhub_earnings_cache: dict | None = None  # fetch once per run

        self.scorer = build_scorer(self.cfg.get("sentiment", {}))

    def _log(self, level: str, msg: str):
        self.progress_cb(level, msg)
        getattr(log, level.lower() if level != "ok" else "info")(msg)

    def run(self, mode: str = "all") -> dict[str, Any]:
        """
        Execute a full scrape.
        mode: all | news | earnings | prices
        Returns summary dict.
        """
        init_db()
        # Adds any column present on the models but missing in the live database.
        # create_all() only ever creates whole tables, so without this an older
        # database silently lacks every column added since it was first created.
        try:
            from .database import ensure_schema
            ensure_schema()
        except Exception as e:
            log.warning(f"[Engine] Schema check failed: {e}")

        session = get_session()

        run = ScraperRun(
            started_at=datetime.utcnow(),
            status="running",
            config_snapshot=json.dumps({"mode": mode}),
        )
        session.add(run)
        session.commit()
        run_id = run.id

        companies = get_basket(session)
        if not companies:
            from .database import seed_default_basket
            seed_default_basket(session)
            companies = get_basket(session)

        self._log("info", f"Starting run #{run_id} [{mode}] — {len(companies)} companies")

        total_articles_new = 0
        tickers_scored     = 0
        errors             = []
        sentiment_results  = {}
        tradeability_results: dict = {}
        self._reuters_cache    = None  # reset feed cache for fresh run
        self._finnhub_earnings_cache = None  # reset earnings cache for fresh run

        # ── Pre-fetch Reuters feeds once before parallel ticker loop ──
        if self.reuters and mode in ("all", "news"):
            try:
                self._log("info", "Reuters: fetching feeds (once per run)...")
                self._reuters_cache = self.reuters.fetch_all_feeds()
                self._log("ok", f"Reuters: {len(self._reuters_cache)} total articles cached")
            except Exception as e:
                self._reuters_cache = []
                self._log("warn", f"Reuters feed fetch failed: {e}")

        # ── Fetch all ticker data in parallel ──────────────────────────
        from concurrent.futures import ThreadPoolExecutor, as_completed
        import threading

        # Thread-safe containers for results
        _lock = threading.Lock()
        ticker_results: dict = {}  # ticker -> {articles, prices, earnings, errors}

        def _fetch_ticker_data(company) -> tuple[str, dict]:
            """Fetch all source data for one ticker. Runs in a thread."""
            tk      = company.ticker
            kws     = company.keyword_list()
            arts    = []
            prices  = None
            earnings = None
            errs    = []

            # Yahoo Finance
            if self.yahoo and mode in ("all", "news", "prices", "earnings"):
                try:
                    data = self.yahoo.fetch_ticker(tk)
                    if mode in ("all", "prices") and data.get("prices"):
                        prices = data["prices"]
                    if mode in ("all", "news", "earnings") and data.get("news"):
                        arts.extend(data["news"])
                    if mode in ("all", "earnings") and data.get("earnings"):
                        earnings = data["earnings"]
                except Exception as e:
                    errs.append(f"Yahoo/{tk}: {e}")

            # Reuters (uses pre-fetched cache — thread safe read)
            if self._reuters_cache is not None and mode in ("all", "news"):
                try:
                    rtr = self.reuters.match_to_ticker(self._reuters_cache, tk, kws)
                    arts.extend(rtr)
                except Exception as e:
                    errs.append(f"Reuters/{tk}: {e}")

            # Finnhub
            if self.finnhub and mode in ("all", "news"):
                try:
                    fh = self.finnhub.fetch_news(tk)
                    arts.extend(fh)
                except Exception as e:
                    errs.append(f"Finnhub/{tk}: {e}")

            return tk, {"articles": arts, "prices": prices, "earnings": earnings, "errors": errs}

        # Run up to 8 tickers concurrently (network I/O bound — safe to parallelize)
        max_workers = min(8, len(companies))
        self._log("info", f"Fetching {len(companies)} tickers with {max_workers} parallel workers...")

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(_fetch_ticker_data, c): c for c in companies}
            for future in as_completed(futures):
                try:
                    tk, result = future.result(timeout=60)
                    ticker_results[tk] = result
                except Exception as e:
                    c = futures[future]
                    errors.append(f"Fetch/{c.ticker}: {e}")

        try:
            for company in companies:
                ticker   = company.ticker
                keywords = company.keyword_list()
                self._log("info", f"[{ticker}] Processing {company.name}...")

                result_data  = ticker_results.get(ticker, {})
                all_articles = result_data.get("articles", [])
                errors.extend(result_data.get("errors", []))

                # Save prices and earnings from pre-fetched data
                if result_data.get("prices"):
                    self._save_price(session, result_data["prices"])
                if result_data.get("earnings"):
                    self._save_earnings(session, result_data["earnings"], run_id)
                    self._purge_stale_upcoming(session, ticker)

                if all_articles:
                    self._log("ok", f"  Fetched {len(all_articles)} total articles")

                # Per-source counts, for the run log only — the data itself was
                # already fetched in the parallel pre-fetch above.
                for src in ("yahoo", "reuters", "finnhub"):
                    count = sum(1 for a in all_articles if a.get("source") == src)
                    if count:
                        self._log("ok", f"  {src.title()}: {count} articles")

                # ── Alpha Vantage ──
                if self.av and mode in ("all", "news", "prices", "earnings"):
                    try:
                        if mode in ("all", "news"):
                            av_news = self.av.fetch_news(ticker, self.max_articles)
                            all_articles.extend(av_news)
                            self._log("ok", f"  AlphaVantage: {len(av_news)} articles")

                        if mode in ("all", "earnings"):
                            av_earn = self.av.fetch_earnings(ticker)
                            self._save_earnings(session, av_earn, run_id)
                    except Exception as e:
                        errors.append(f"AlphaVantage/{ticker}: {e}")
                        self._log("warn", f"  AlphaVantage error: {e}")

                # ── Benzinga ──
                if self.bz and mode in ("all", "news", "earnings"):
                    try:
                        if mode in ("all", "news"):
                            bz_news = self.bz.fetch_news(ticker, self.max_articles)
                            all_articles.extend(bz_news)
                            self._log("ok", f"  Benzinga: {len(bz_news)} articles")

                        if mode in ("all", "earnings"):
                            bz_earn = self.bz.fetch_earnings(ticker)
                            self._save_earnings(session, bz_earn, run_id)
                    except Exception as e:
                        errors.append(f"Benzinga/{ticker}: {e}")
                        self._log("warn", f"  Benzinga error: {e}")

                # ── FinViz ──
                if self.fv and mode in ("all", "news", "prices"):
                    try:
                        fv_news = self.fv.fetch_news(ticker, self.max_articles)
                        all_articles.extend(fv_news)
                        self._log("ok", f"  FinViz: {len(fv_news)} articles")
                    except Exception as e:
                        errors.append(f"FinViz/{ticker}: {e}")
                        self._log("warn", f"  FinViz error: {e}")

                # ── WSJ ──
                if self.wsj and self.wsj.enabled and mode in ("all", "news"):
                    try:
                        wsj_news = self.wsj.fetch_news(ticker, keywords, self.max_articles)
                        all_articles.extend(wsj_news)
                        self._log("ok", f"  WSJ: {len(wsj_news)} articles")
                    except Exception as e:
                        errors.append(f"WSJ/{ticker}: {e}")
                        self._log("warn", f"  WSJ error: {e}")

                # ── MarketWatch ──
                if self.mw and mode in ("all", "news"):
                    try:
                        mw_news = self.mw.fetch_news(ticker, self.max_articles)
                        all_articles.extend(mw_news)
                        self._log("ok", f"  MarketWatch: {len(mw_news)} articles")
                    except Exception as e:
                        errors.append(f"MarketWatch/{ticker}: {e}")
                        self._log("warn", f"  MarketWatch error: {e}")

                # Reuters and Finnhub are fetched in the parallel pre-fetch above

                # ── Persist articles ──
                new_count = self._save_articles(session, all_articles, run_id)
                total_articles_new += new_count
                self._log("info", f"  Saved {new_count} new articles (total: {len(all_articles)} fetched)")

                # ── Sentiment scoring ──
                if all_articles and mode in ("all", "news"):
                    self._log("info", f"  Scoring sentiment for {ticker}...")
                    try:
                        result = aggregate_scores(
                            all_articles,
                            self.scorer,
                            source_weights={"wsj": 1.5, "benzinga": 1.2, "reuters": 1.3, "yahoo": 1.0, "alphav": 1.0, "marketwatch": 1.1, "finviz": 0.9, "finnhub": 1.2},
                        )
                        self._save_sentiment(session, ticker, result, run_id)
                        sentiment_results[ticker] = result
                        tickers_scored += 1
                        self._log(
                            "ok",
                            f"  {ticker}: {result['score']:+.3f} — "
                            f"{result['label']} ({result['article_count']} articles)"
                        )

                        # NOTE: predictions are no longer recorded here. This
                        # block used to write the *sentiment* score into
                        # `tradeability_outcomes`, so the "Signal Accuracy" panel
                        # was measuring how well news sentiment alone predicted
                        # returns — not the composite tradeability score it
                        # claimed to be tracking. Recording now happens once per
                        # run, after the composite is computed. See below.

                        # Check alert thresholds
                        alert_module.check_and_fire(
                            self.cfg.get("alerts", {}),
                            ticker, company.name,
                            result["score"], result["label"]
                        )
                    except Exception as e:
                        errors.append(f"Sentiment/{ticker}: {e}")
                        self._log("warn", f"  Sentiment error: {e}")

            # ── Record today's composite score as a pending prediction ────
            # Once per run, and the outcome table enforces one row per ticker
            # per calendar day, so a 15-minute scheduler produces one
            # observation a day rather than ninety-six copies of the same one.
            if mode in ("all", "news"):
                try:
                    from .tradeability import compute_basket_tradeability

                    scores = compute_basket_tradeability(session)
                    recorded = 0
                    for tk, result in scores.items():
                        if feedback_module.record_prediction(
                            session, tk,
                            score=result["score"],
                            label=result["label"],
                            run_id=run_id,
                        ):
                            recorded += 1
                    session.commit()
                    tradeability_results = scores
                    if recorded:
                        self._log("ok", f"  Feedback: recorded {recorded} new daily predictions")
                except Exception as fe:
                    session.rollback()
                    log.warning(f"[Feedback] Prediction recording failed: {fe}")

            # ── Resolve pending outcome records ──────────────────────────
            # Only worth doing once a day: predictions are recorded at most once
            # per ticker per calendar day and resolve over a 5-trading-day
            # horizon, so re-running this every 15 minutes is pure overhead.
            try:
                if self._should_resolve_outcomes(session):
                    from concurrent.futures import ThreadPoolExecutor

                    tickers = [c.ticker for c in companies]
                    with ThreadPoolExecutor(max_workers=min(8, len(tickers) or 1)) as pool:
                        histories = pool.map(
                            lambda t: (t, feedback_module.fetch_close_history(t, days=30)),
                            tickers,
                        )
                        price_history = {t: h for t, h in histories if h}

                    resolved = feedback_module.resolve_pending_outcomes(session, price_history)
                    if resolved:
                        self._log("ok", f"  Feedback: resolved {resolved} outcome records")
            except Exception as fe:
                log.warning(f"[Feedback] Outcome resolution failed: {fe}")

            # ── Finalise run ──
            run.finished_at    = datetime.utcnow()
            run.status         = "ok" if not errors else "partial"
            run.articles_new   = total_articles_new
            run.tickers_scored = tickers_scored
            if errors:
                run.error_msg = "\n".join(errors[:20])
            session.commit()

        except Exception as e:
            run.status    = "error"
            run.error_msg = str(e)
            run.finished_at = datetime.utcnow()
            session.commit()
            self._log("err", f"Run failed: {e}")
            raise
        finally:
            # Read timestamps before session closes
            _finished_at = run.finished_at
            _started_at  = run.started_at
            session.close()

        elapsed = (_finished_at - _started_at).total_seconds()
        self._log("ok", f"Run #{run_id} complete — {total_articles_new} new articles, "
                        f"{tickers_scored} scored in {elapsed:.0f}s")

        return {
            "run_id":          run_id,
            "articles_new":    total_articles_new,
            "tickers_scored":  tickers_scored,
            "sentiment":       sentiment_results,
            "tradeability":    tradeability_results,
            "errors":          errors,
            "elapsed_seconds": elapsed,
        }

    def _should_resolve_outcomes(self, session) -> bool:
        """
        True at most once per calendar day.

        Outcome resolution walks the whole basket's price history. With a
        15-minute schedule that is 96 redundant passes a day over data that
        changes once, at the close.
        """
        from sqlalchemy import select, func
        from .database import TradeabilityOutcome
        from .clock import market_date

        last_resolved = session.execute(
            select(func.max(TradeabilityOutcome.outcome_date))
        ).scalar()

        if last_resolved is None:
            return True
        return last_resolved.date() < market_date()

    # ── Private persistence helpers ────────────────────────────────────────────

    def _save_articles(self, session, articles: list[dict], run_id: int) -> int:
        """
        Persist new articles, deduplicating on URL *and* on normalised headline.

        Three problems with the previous version:

        1. It issued one SELECT per article (N+1).
        2. It wrote ``url=""`` when a source gave no URL. With a
           UNIQUE(source, url) constraint, two URL-less articles from the same
           source in one run raised IntegrityError at the single commit at the
           end — discarding *every* article in the batch. URLs are now NULL when
           absent, and NULLs do not collide under a unique constraint.
        3. It only deduplicated by URL, so syndicated wire copy was counted once
           per outlet and inflated the source-diversity bonus.
        """
        from sqlalchemy import select
        from .database import headline_key

        if not articles:
            return 0

        urls = {a["url"] for a in articles if a.get("url")}
        keys = set()
        for a in articles:
            k = headline_key(a.get("headline", ""))
            if k:
                keys.add(k)

        # Two bulk lookups instead of one per article.
        existing_urls: set[str] = set()
        if urls:
            existing_urls = {
                u for (u,) in session.execute(
                    select(Article.url).where(Article.url.in_(urls))
                ).all() if u
            }

        existing_keys: set[tuple[str, str]] = set()
        if keys:
            existing_keys = {
                (t, k) for (t, k) in session.execute(
                    select(Article.ticker, Article.headline_key)
                    .where(Article.headline_key.in_(keys))
                ).all() if k
            }

        new_count = 0
        seen_this_batch: set[tuple[str, str]] = set()

        for a in articles:
            url = (a.get("url") or "").strip() or None
            if url and url in existing_urls:
                continue

            ticker = a.get("ticker", "")
            hkey   = headline_key(a.get("headline", ""))
            if hkey:
                if (ticker, hkey) in existing_keys or (ticker, hkey) in seen_this_batch:
                    continue
                seen_this_batch.add((ticker, hkey))

            session.add(Article(
                source       =a.get("source", "unknown"),
                ticker       =ticker,
                headline     =a.get("headline", "")[:500],
                body_snippet =(a.get("body_snippet") or "")[:1000],
                url          =url[:800] if url else None,
                headline_key =hkey or None,
                published_at =a.get("published_at"),
                sentiment    =a.get("sentiment"),
                used_in_run  =run_id,
            ))
            if url:
                existing_urls.add(url)
            new_count += 1

        try:
            session.commit()
        except Exception as e:
            # A losing race on the unique constraint should cost one article,
            # not the whole batch — retry row by row.
            session.rollback()
            log.warning(f"[Engine] Batch article insert failed ({e}); retrying individually")
            new_count = self._save_articles_individually(session, articles, run_id)

        return new_count

    def _save_articles_individually(self, session, articles: list[dict], run_id: int) -> int:
        """Row-at-a-time fallback so one bad record cannot discard a whole batch."""
        from .database import headline_key
        saved = 0
        for a in articles:
            url = (a.get("url") or "").strip() or None
            try:
                session.add(Article(
                    source       =a.get("source", "unknown"),
                    ticker       =a.get("ticker", ""),
                    headline     =a.get("headline", "")[:500],
                    body_snippet =(a.get("body_snippet") or "")[:1000],
                    url          =url[:800] if url else None,
                    headline_key =headline_key(a.get("headline", "")) or None,
                    published_at =a.get("published_at"),
                    sentiment    =a.get("sentiment"),
                    used_in_run  =run_id,
                ))
                session.commit()
                saved += 1
            except Exception:
                session.rollback()
        return saved

    def _save_price(self, session, prices: dict):
        """
        Persist a price snapshot, enriched with IV rank from the iv_snapshots
        history so the options-IV signal has something real to read.
        """
        ticker = prices["ticker"]

        iv_rank_data = {}
        try:
            from .iv_rank import compute_iv_rank
            iv_rank_data = compute_iv_rank(session, ticker)
        except Exception as e:
            log.debug(f"[Engine] IV rank lookup failed for {ticker}: {e}")

        snap = PriceSnapshot(
            ticker             =ticker,
            price              =prices.get("price"),
            change_pct         =prices.get("change_pct"),
            volume             =prices.get("volume"),
            avg_volume_30d     =prices.get("avg_volume_30d"),
            volume_ratio       =prices.get("volume_ratio"),
            market_cap         =prices.get("market_cap"),
            pe_ratio           =prices.get("pe_ratio"),
            week_52_high       =prices.get("week_52_high"),
            week_52_low        =prices.get("week_52_low"),
            analyst_rating     =prices.get("analyst_rating"),
            analyst_mean_score =prices.get("analyst_mean_score"),
            target_price       =prices.get("target_price"),
            short_ratio        =prices.get("short_ratio"),
            short_pct_float    =prices.get("short_pct_float"),
            implied_volatility =iv_rank_data.get("iv_current") or prices.get("implied_volatility"),
            iv_52w_low         =iv_rank_data.get("iv_52w_low"),
            iv_52w_high        =iv_rank_data.get("iv_52w_high"),
            iv_rank            =iv_rank_data.get("iv_rank"),
            iv_percentile      =iv_rank_data.get("iv_pct"),
            iv_history_days    =iv_rank_data.get("data_days"),
            beta               =prices.get("beta"),
        )
        session.add(snap)
        session.commit()

    def _save_earnings(self, session, events: list[dict], run_id: int):
        """
        Upsert earnings events.

        The previous version skipped on any (ticker, period) match. Combined with
        the scraper writing the literal period ``"Upcoming"``, the first earnings
        date ever fetched was frozen permanently — so ``days_to_earnings``, and
        therefore the "avoid within 3 days of earnings" guardrail, ran on a date
        that could be months stale. Now a rescheduled date, a newly reported
        actual, or a revised estimate all update the existing row.
        """
        from sqlalchemy import select
        from datetime import datetime as _dt

        for ev in events:
            if not ev.get("period"):
                continue
            existing = session.execute(
                select(EarningsEvent).where(
                    EarningsEvent.ticker == ev["ticker"],
                    EarningsEvent.period == ev["period"],
                )
            ).scalar_one_or_none()

            if existing is None:
                session.add(EarningsEvent(
                    ticker          =ev["ticker"],
                    period          =ev["period"],
                    report_date     =ev.get("report_date"),
                    eps_estimate    =ev.get("eps_estimate"),
                    eps_actual      =ev.get("eps_actual"),
                    revenue_estimate=ev.get("revenue_estimate"),
                    revenue_actual  =ev.get("revenue_actual"),
                    surprise_pct    =ev.get("surprise_pct"),
                    is_upcoming     =bool(ev.get("is_upcoming")),
                ))
            else:
                # Only overwrite with non-null values so a source that omits a
                # field cannot wipe data another source already supplied.
                for field in ("report_date", "eps_estimate", "eps_actual",
                              "revenue_estimate", "revenue_actual", "surprise_pct"):
                    value = ev.get(field)
                    if value is not None:
                        setattr(existing, field, value)
                if ev.get("eps_actual") is not None:
                    existing.is_upcoming = False
                elif "is_upcoming" in ev:
                    existing.is_upcoming = bool(ev["is_upcoming"])
                existing.updated_at = _dt.utcnow()

        try:
            session.commit()
        except Exception as e:
            session.rollback()
            log.warning(f"[Engine] Earnings upsert failed: {e}")

    def _purge_stale_upcoming(self, session, ticker: str) -> None:
        """
        Drop 'upcoming' rows whose date has passed without an actual EPS.

        Without this, a date that slips leaves a permanent phantom event in the
        past that the event-driven signal keeps treating as the next report.
        """
        from sqlalchemy import select
        from .clock import market_date

        stale = session.execute(
            select(EarningsEvent).where(
                EarningsEvent.ticker == ticker,
                EarningsEvent.is_upcoming == True,   # noqa: E712 - SQL comparison
                EarningsEvent.eps_actual.is_(None),
            )
        ).scalars().all()

        today   = market_date()
        removed = 0
        for row in stale:
            rd = row.report_date.date() if hasattr(row.report_date, "date") else row.report_date
            if rd and rd < today - timedelta(days=3):
                session.delete(row)
                removed += 1
        if removed:
            session.commit()

    def _save_sentiment(self, session, ticker: str, result: dict, run_id: int):
        score = SentimentScore(
            run_id          =run_id,
            ticker          =ticker,
            score           =result["score"],
            label           =result["label"],
            article_count   =result["article_count"],
            source_breakdown=json.dumps(result["source_breakdown"]),
        )
        session.add(score)
        session.commit()
