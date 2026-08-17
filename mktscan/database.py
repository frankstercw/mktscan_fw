"""
mktscan/database.py
SQLAlchemy models + helpers for SQLite (default) or PostgreSQL.
"""
from __future__ import annotations
import hashlib
import json
import logging
import os
import re
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

log = logging.getLogger(__name__)

from sqlalchemy import (
    create_engine, Column, String, Float, Integer, Date,
    DateTime, Text, Boolean, UniqueConstraint, Index,
    select, desc, func, and_, event
)
from sqlalchemy.orm import Session, sessionmaker

# Support both SQLAlchemy 2.x (DeclarativeBase) and 1.4.x (declarative_base)
try:
    from sqlalchemy.orm import DeclarativeBase
    class Base(DeclarativeBase):
        pass
except ImportError:
    from sqlalchemy.orm import declarative_base
    Base = declarative_base()

from .config import get


# ── Models ────────────────────────────────────────────────────────────────────

class Company(Base):
    """Companies in the watch basket."""
    __tablename__ = "companies"

    ticker   = Column(String(10), primary_key=True)
    name     = Column(String(200), nullable=False)
    sector   = Column(String(100))
    keywords = Column(Text)
    active   = Column(Boolean, default=True)
    added_at = Column(DateTime, default=datetime.utcnow)

    def keyword_list(self) -> list[str]:
        if not self.keywords:
            return [self.ticker]
        return [k.strip() for k in self.keywords.split(",") if k.strip()]


class Article(Base):
    """Scraped articles / headlines."""
    __tablename__ = "articles"

    id           = Column(Integer, primary_key=True, autoincrement=True)
    source       = Column(String(50), nullable=False)
    ticker       = Column(String(10), nullable=False)
    headline     = Column(Text, nullable=False)
    body_snippet = Column(Text)
    # NULL (not "") when the source gave us no URL. NULLs do not collide under a
    # UNIQUE constraint in either SQLite or Postgres, so several URL-less articles
    # from the same source in one run no longer abort the whole batch insert.
    url          = Column(Text)
    # SHA1 of the normalised headline — used to collapse syndicated wire copy that
    # appears on several outlets under different URLs.
    headline_key = Column(String(40), index=True)
    published_at = Column(DateTime)
    scraped_at   = Column(DateTime, default=datetime.utcnow)
    sentiment    = Column(Float)
    used_in_run  = Column(Integer)

    __table_args__ = (
        UniqueConstraint("source", "url", name="uq_source_url"),
        Index("ix_articles_ticker", "ticker"),
        Index("ix_articles_scraped_at", "scraped_at"),
        # Hot query: WHERE ticker = ? ORDER BY scraped_at DESC LIMIT n
        Index("ix_articles_ticker_scraped", "ticker", "scraped_at"),
        # Dedup lookup: WHERE ticker = ? AND headline_key = ?
        Index("ix_articles_ticker_headline", "ticker", "headline_key"),
    )


class SentimentScore(Base):
    """Aggregated sentiment score per ticker per run."""
    __tablename__ = "sentiment_scores"

    id               = Column(Integer, primary_key=True, autoincrement=True)
    run_id           = Column(Integer, nullable=False)
    ticker           = Column(String(10), nullable=False)
    score            = Column(Float, nullable=False)
    label            = Column(String(20))
    article_count    = Column(Integer, default=0)
    source_breakdown = Column(Text)
    scored_at        = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_sentiment_ticker", "ticker"),
        Index("ix_sentiment_run", "run_id"),
        # Hot query: latest score per ticker
        Index("ix_sentiment_ticker_scored", "ticker", "scored_at"),
    )

    def source_dict(self) -> dict:
        if self.source_breakdown:
            return json.loads(self.source_breakdown)
        return {}


class PriceSnapshot(Base):
    """End-of-day price + basic fundamentals."""
    __tablename__ = "price_snapshots"

    id                  = Column(Integer, primary_key=True, autoincrement=True)
    ticker              = Column(String(10), nullable=False)
    price               = Column(Float)
    change_pct          = Column(Float)
    volume              = Column(Integer)
    avg_volume_30d      = Column(Integer)
    volume_ratio        = Column(Float)       # today / 30d avg — >1.5 = unusual
    market_cap          = Column(Float)
    pe_ratio            = Column(Float)
    week_52_high        = Column(Float)
    week_52_low         = Column(Float)
    analyst_rating      = Column(String(30))
    analyst_mean_score  = Column(Float)       # 1=strong buy, 5=sell
    target_price        = Column(Float)       # consensus analyst target
    short_ratio         = Column(Float)       # days to cover short
    short_pct_float     = Column(Float)       # % of float shorted
    implied_volatility  = Column(Float)       # ATM IV from the live option chain, annualised
    iv_52w_low          = Column(Float)       # lowest IV in the trailing window
    iv_52w_high         = Column(Float)       # highest IV in the trailing window
    iv_rank             = Column(Float)       # 0-100, where current IV sits in that range
    iv_percentile       = Column(Float)       # 0-100, % of days with lower IV
    iv_history_days     = Column(Integer)     # how many observations backed the rank
    beta                = Column(Float)       # market beta
    snapped_at          = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_price_ticker", "ticker"),
        # Hot query: WHERE ticker = ? ORDER BY snapped_at DESC LIMIT 1
        Index("ix_price_ticker_snapped", "ticker", "snapped_at"),
    )


class EarningsEvent(Base):
    """Upcoming earnings dates and results."""
    __tablename__ = "earnings_events"

    id               = Column(Integer, primary_key=True, autoincrement=True)
    ticker           = Column(String(10), nullable=False)
    report_date      = Column(DateTime)
    period           = Column(String(20))
    eps_estimate     = Column(Float)
    eps_actual       = Column(Float)
    revenue_estimate = Column(Float)
    revenue_actual   = Column(Float)
    surprise_pct     = Column(Float)   # (actual - estimate) / |estimate| * 100
    is_upcoming      = Column(Boolean, default=False)  # scheduled, not yet reported
    scraped_at       = Column(DateTime, default=datetime.utcnow)
    updated_at       = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("ticker", "period", name="uq_earnings_period"),
        Index("ix_earnings_ticker", "ticker"),
        Index("ix_earnings_ticker_date", "ticker", "report_date"),
    )


class ScraperRun(Base):
    """Audit log of each scraper execution."""
    __tablename__ = "scraper_runs"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    started_at      = Column(DateTime, default=datetime.utcnow)
    finished_at     = Column(DateTime)
    status          = Column(String(20), default="running")
    articles_new    = Column(Integer, default=0)
    tickers_scored  = Column(Integer, default=0)
    error_msg       = Column(Text)
    config_snapshot = Column(Text)


class TradeabilityOutcome(Base):
    """
    Records each tradeability score alongside the actual price outcome
    observed N days later. Used to calibrate future scores via feedback.

    Populated automatically by the engine on each run:
      - On run N:   record score_at_prediction = today's tradeability score
      - On run N+1: look back at yesterday's record, fill in actual_return_pct

    This builds a ground-truth dataset: "when I scored AAPL +0.098,
    the stock actually moved +2.1% the next day."
    """
    __tablename__ = "tradeability_outcomes"

    id                   = Column(Integer, primary_key=True, autoincrement=True)
    ticker               = Column(String(10), nullable=False)
    score_at_prediction  = Column(Float, nullable=False)   # tradeability score when predicted
    label_at_prediction  = Column(String(20))              # BULLISH / NEUTRAL / BEARISH etc
    predicted_at         = Column(DateTime, nullable=False) # when the score was recorded
    # Calendar date of the prediction, in US market time. The unique constraint on
    # (ticker, prediction_date) is what stops a 15-minute scheduler from writing 96
    # near-identical predictions a day that all resolve to the same forward return.
    prediction_date      = Column(Date, nullable=False)
    outcome_date         = Column(DateTime)                 # date the price move was observed
    actual_return_pct    = Column(Float)                    # actual % price change over horizon
    horizon_days         = Column(Integer, default=5)       # trading days between prediction and outcome
    direction_correct    = Column(Boolean)                  # did sign of score match sign of return?
    magnitude_error      = Column(Float)                    # abs(expected_move - actual_move)
    run_id               = Column(Integer)
    # Market context captured at prediction time. Context only: these fields do
    # not alter the score; they make regime-conditioned validation possible.
    regime_score_at_prediction      = Column(Float)
    regime_label_at_prediction      = Column(String(40))
    regime_confidence_at_prediction = Column(Float)

    __table_args__ = (
        UniqueConstraint("ticker", "prediction_date", name="uq_outcome_ticker_day"),
        Index("ix_outcome_ticker", "ticker"),
        Index("ix_outcome_predicted_at", "predicted_at"),
        Index("ix_outcome_ticker_date", "ticker", "prediction_date"),
    )


class MacroEvent(Base):
    """Persisted macro calendar event used by the market-regime risk layer."""
    __tablename__ = "macro_events"

    id         = Column(Integer, primary_key=True, autoincrement=True)
    source     = Column(String(30), nullable=False, default="marketwatch")
    name       = Column(String(200), nullable=False)
    category   = Column(String(50))
    importance = Column(String(20))
    event_at   = Column(DateTime, nullable=False)
    period     = Column(String(50))
    consensus  = Column(String(100))
    prior      = Column(String(100))
    actual     = Column(String(100))
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("source", "name", "event_at", name="uq_macro_source_name_time"),
        Index("ix_macro_event_at", "event_at"),
        Index("ix_macro_importance_at", "importance", "event_at"),
    )


class MarketRegimeSnapshot(Base):
    """One updatable market-context snapshot per US market date."""
    __tablename__ = "market_regime_snapshots"

    id            = Column(Integer, primary_key=True, autoincrement=True)
    snapshot_date = Column(Date, nullable=False, unique=True)
    snapped_at    = Column(DateTime, default=datetime.utcnow)

    regime_score  = Column(Float)
    regime_label  = Column(String(40))
    confidence    = Column(Float)
    coverage      = Column(Float)
    trend_score   = Column(Float)

    spy_price       = Column(Float)
    spy_return_20d  = Column(Float)
    spy_return_60d  = Column(Float)
    spy_trend_score = Column(Float)
    qqq_price       = Column(Float)
    qqq_return_20d  = Column(Float)
    qqq_return_60d  = Column(Float)
    qqq_trend_score = Column(Float)

    vix                  = Column(Float)
    vix_change_5d_pct    = Column(Float)
    vix_percentile_20d   = Column(Float)
    vix_percentile_1y    = Column(Float)
    volatility_state     = Column(String(40))
    volatility_score     = Column(Float)

    breadth_above_20d      = Column(Float)
    breadth_above_50d      = Column(Float)
    breadth_above_200d     = Column(Float)
    breadth_positive_5d    = Column(Float)
    breadth_positive_20d   = Column(Float)
    breadth_score          = Column(Float)
    breadth_universe_size  = Column(Integer)

    two_year_yield            = Column(Float)
    ten_year_yield            = Column(Float)
    curve_10y_2y              = Column(Float)
    ten_year_5d_change_bps    = Column(Float)
    ten_year_20d_change_bps   = Column(Float)
    rates_score               = Column(Float)

    next_macro_event      = Column(String(200))
    next_macro_at         = Column(DateTime)
    next_macro_importance = Column(String(20))
    hours_to_macro        = Column(Float)
    macro_risk_score      = Column(Float)

    components = Column(Text)

    __table_args__ = (
        Index("ix_regime_snapshot_date", "snapshot_date"),
        Index("ix_regime_snapped_at", "snapped_at"),
    )


class IVSnapshot(Base):
    """
    Daily ATM implied volatility per ticker — the history behind IV rank.

    Declared on the shared ``Base`` so ``init_db()`` actually creates it. It
    previously lived on its own ``declarative_base()`` in iv_rank.py, which meant
    the table was never created and IV rank was permanently unavailable.

    ``source`` distinguishes true option-chain IV from the realised-volatility
    proxy used to seed history. Ranking real IV against an RV-built range is
    apples-to-oranges (IV sits structurally above RV), so ``compute_iv_rank``
    ranks within a single source class.
    """
    __tablename__ = "iv_snapshots"

    id            = Column(Integer, primary_key=True, autoincrement=True)
    ticker        = Column(String(10), nullable=False)
    snapshot_date = Column(Date,       nullable=False)
    iv_atm        = Column(Float)      # ATM IV from the option chain
    iv_proxy      = Column(Float)      # 30d realised vol, annualised — seeding proxy
    iv_used       = Column(Float)      # whichever value the rank should use
    source        = Column(String(10), default="chain")   # "chain" | "proxy"
    dte           = Column(Integer)    # days to expiry of the chain we sampled
    created_at    = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("ticker", "snapshot_date", name="uq_iv_ticker_date"),
        Index("ix_iv_ticker_date", "ticker", "snapshot_date"),
    )

    def __repr__(self):
        return f"<IVSnapshot {self.ticker} {self.snapshot_date} iv={self.iv_used}>"

# ── Engine & session factory ──────────────────────────────────────────────────

_engine = None
_Session: sessionmaker | None = None


def get_engine():
    global _engine
    if _engine is None:
        storage = get("storage") or {}
        stype = storage.get("type", "sqlite")

        # An explicit DATABASE_URL always wins — that is how Railway/Heroku inject
        # Postgres credentials, and it keeps the DSN out of the config file.
        dsn_env = os.environ.get("DATABASE_URL", "").strip()
        if dsn_env:
            if dsn_env.startswith("postgres://"):      # SQLAlchemy 2.x dropped this alias
                dsn_env = dsn_env.replace("postgres://", "postgresql://", 1)
            _engine = create_engine(dsn_env, pool_pre_ping=True)
        elif stype == "postgres":
            dsn = storage.get("postgres_dsn", "postgresql://localhost/mktscan")
            _engine = create_engine(dsn, pool_pre_ping=True)
        else:
            path = storage.get("sqlite_path", "./data/mktscan.db")
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            _engine = create_engine(
                f"sqlite:///{path}",
                connect_args={"check_same_thread": False, "timeout": 30},
            )

            # The dashboard process and the scheduler process share this file.
            # Without WAL, any dashboard read blocks a scheduler write and you get
            # "database is locked". WAL lets readers and one writer coexist.
            @event.listens_for(_engine, "connect")
            def _set_sqlite_pragmas(dbapi_conn, _record):
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA synchronous=NORMAL")
                cur.execute("PRAGMA busy_timeout=30000")
                cur.execute("PRAGMA foreign_keys=ON")
                cur.close()

    return _engine


def get_session() -> Session:
    global _Session
    if _Session is None:
        _Session = sessionmaker(bind=get_engine(), autoflush=True)
    return _Session()


@contextmanager
def session_scope() -> Iterator[Session]:
    """
    Transactional scope around a series of operations.

    Preferred over bare ``get_session()`` — it guarantees rollback on error and
    close on exit, which the callers that leaked sessions were relying on the GC
    for. Use as::

        with session_scope() as session:
            ...
    """
    session = get_session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db() -> None:
    """
    Create any missing tables.

    This only ever *adds* tables — it cannot add a column to a table that already
    exists. Schema changes go through Alembic (``alembic upgrade head``); see
    migrations/. ``ensure_schema()`` below is the safety net for databases created
    before Alembic was introduced.
    """
    engine = get_engine()
    Base.metadata.create_all(engine)


def ensure_schema() -> list[str]:
    """
    Add any columns that exist on the models but not in the live database.

    Every column added since the original deploy (target_price, short_ratio,
    implied_volatility, beta, iv_rank, headline_key, prediction_date, ...) was
    previously invisible on existing databases: ``create_all`` does not alter
    tables, and the call sites read them through ``getattr(row, name, None)``,
    so a missing column silently returned None instead of raising. That is
    precisely how the IV-rank pipeline stayed broken.

    Returns the list of DDL statements applied.
    """
    from sqlalchemy import inspect as sa_inspect, text

    engine    = get_engine()
    inspector = sa_inspect(engine)
    existing  = set(inspector.get_table_names())
    applied: list[str] = []

    type_sql = {
        "FLOAT": "FLOAT", "INTEGER": "INTEGER", "BOOLEAN": "BOOLEAN",
        "DATETIME": "TIMESTAMP", "DATE": "DATE", "TEXT": "TEXT",
    }

    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing:
                continue
            live_cols = {c["name"] for c in inspector.get_columns(table.name)}
            for col in table.columns:
                if col.name in live_cols:
                    continue
                compiled = col.type.compile(engine.dialect)
                sql_type = type_sql.get(str(col.type).upper().split("(")[0], compiled)
                ddl = f'ALTER TABLE {table.name} ADD COLUMN {col.name} {sql_type}'
                try:
                    conn.execute(text(ddl))
                    applied.append(ddl)
                except Exception as e:          # column may race in, or type unsupported
                    log.warning(f"[schema] could not apply `{ddl}`: {e}")

    if applied:
        log.info(f"[schema] applied {len(applied)} column addition(s)")
    return applied


# ── Query helpers ─────────────────────────────────────────────────────────────

def get_basket(session: Session) -> list[Company]:
    return list(session.execute(
        select(Company).where(Company.active == True)
    ).scalars())


def get_latest_scores(session: Session) -> list:
    """
    Return the most recent score for each ticker.

    Previously this selected the *entire* sentiment_scores table and deduplicated
    in Python, which grows without bound (a 15-minute scheduler writes ~96 rows
    per ticker per day) and relied on ORDER BY surviving a subquery — something
    Postgres explicitly does not guarantee. Now the database does the work via a
    MAX(scored_at) group-by join, which the (ticker, scored_at) index serves
    directly.
    """
    latest = (
        select(
            SentimentScore.ticker.label("ticker"),
            func.max(SentimentScore.scored_at).label("max_scored_at"),
        )
        .group_by(SentimentScore.ticker)
        .subquery()
    )

    return list(session.execute(
        select(
            SentimentScore.ticker,
            SentimentScore.score.label("score"),
            SentimentScore.label.label("lbl"),
            SentimentScore.article_count,
            SentimentScore.source_breakdown,
            SentimentScore.scored_at,
        ).join(
            latest,
            and_(
                SentimentScore.ticker == latest.c.ticker,
                SentimentScore.scored_at == latest.c.max_scored_at,
            ),
        )
    ).all())


# ── Headline normalisation / dedup key ────────────────────────────────────────

_PUNCT_RE = re.compile(r"[^a-z0-9 ]+")
_WS_RE    = re.compile(r"\s+")


def headline_key(headline: str) -> str:
    """
    Stable key for detecting the same story republished under different URLs.

    Wire copy from Reuters/AP shows up on Yahoo, MarketWatch and half a dozen
    aggregators with distinct URLs. URL-level dedup misses all of it, so a single
    story was being counted N times *and* inflating the source-diversity bonus
    that is supposed to reward genuinely independent confirmation.

    Normalisation: lowercase, strip punctuation, collapse whitespace, drop a
    leading outlet prefix ("Reuters - ", "UPDATE 2-", ...).
    """
    if not headline:
        return ""
    text = headline.strip().lower()
    text = re.sub(r"^(update|exclusive|breaking|correct(ed)?)\s*\d*\s*[-:]\s*", "", text)
    text = re.sub(r"^[a-z .]{2,20}\s+[-–—]\s+", "", text)
    text = _PUNCT_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text).strip()
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def get_score_history(session: Session, ticker: str, days: int = 30) -> list:
    from datetime import timedelta
    cutoff = datetime.utcnow() - timedelta(days=days)
    return list(session.execute(
        select(SentimentScore)
        .where(SentimentScore.ticker == ticker)
        .where(SentimentScore.scored_at >= cutoff)
        .order_by(SentimentScore.scored_at)
    ).scalars())


def get_recent_articles(session: Session, ticker: str, limit: int = 20) -> list[Article]:
    return list(session.execute(
        select(Article)
        .where(Article.ticker == ticker)
        .order_by(desc(Article.scraped_at))
        .limit(limit)
    ).scalars())


def upsert_company(session: Session, ticker: str, name: str,
                   sector: str = "", keywords: str = "") -> Company:
    c = session.get(Company, ticker)
    if c is None:
        c = Company(ticker=ticker, name=name, sector=sector, keywords=keywords)
        session.add(c)
    else:
        c.name = name
        if sector:
            c.sector = sector
        if keywords:
            c.keywords = keywords
    session.commit()
    return c


# ── Default basket ────────────────────────────────────────────────────────────

BASKET_DEFAULTS = [
    # ── Original basket ───────────────────────────────────────────────────────
    ("AAPL", "Apple Inc.",             "Technology",     "Apple, iPhone, App Store, Mac, Tim Cook"),
    ("NVDA", "NVIDIA Corp.",           "Semiconductors", "NVIDIA, GeForce, H100, Blackwell, Jensen Huang, GPU"),
    ("MSFT", "Microsoft Corp.",        "Technology",     "Microsoft, Azure, Copilot, Windows, Satya Nadella"),
    ("AVGO", "Broadcom Inc.",          "Semiconductors", "Broadcom, AVGO, VMware, Hock Tan, networking chips"),
    ("TSM",  "Taiwan Semiconductor",   "Semiconductors", "TSMC, Taiwan Semiconductor, foundry, C.C. Wei"),
    ("AMD",  "Advanced Micro Devices", "Semiconductors", "AMD, Ryzen, EPYC, Radeon, Lisa Su, MI300"),
    ("GOOG", "Alphabet Inc.",          "Technology",     "Google, Alphabet, Gemini, YouTube, Waymo, Sundar Pichai"),
    ("AMZN", "Amazon.com Inc.",        "Technology",     "Amazon, AWS, Prime, Alexa, Andy Jassy, cloud"),
    ("META", "Meta Platforms",         "Social Media",   "Meta, Facebook, Instagram, WhatsApp, Llama, Mark Zuckerberg"),
    # ── New additions ─────────────────────────────────────────────────────────
    ("ADBE", "Adobe Inc.",             "Technology",     "Adobe, Photoshop, Creative Cloud, Firefly, Figma"),
    ("Z",    "Zillow Group",           "Real Estate Tech","Zillow, Zestimate, rental, mortgage, housing market"),
    ("SNOW", "Snowflake Inc.",         "Cloud / Data",   "Snowflake, data cloud, data warehouse, SQL, AI data"),
    ("RIOT", "Riot Platforms",         "Crypto Mining",  "Riot, Bitcoin mining, cryptocurrency, BTC, mining rig"),
    ("MARA", "Marathon Digital",       "Crypto Mining",  "Marathon, MARA, Bitcoin mining, cryptocurrency, BTC"),
    ("PANW", "Palo Alto Networks",     "Cybersecurity",  "Palo Alto, PANW, cybersecurity, firewall, Nikesh Arora"),
    ("PLTR", "Palantir Technologies",  "AI / Defence",   "Palantir, PLTR, AIP, government AI, defence, Alex Karp"),
    ("SOFI", "SoFi Technologies",      "Fintech",        "SoFi, neobank, student loans, personal finance, fintech"),
    ("HOOD", "Robinhood Markets",      "Fintech",        "Robinhood, HOOD, retail trading, crypto, commission-free"),
    ("CELH", "Celsius Holdings",       "Consumer / Bev", "Celsius, energy drink, CELH, beverage, fitness"),
]


def seed_default_basket(session: Session) -> None:
    """Populate the basket if the companies table is empty."""
    existing = get_basket(session)
    if existing:
        return
    for ticker, name, sector, kw in BASKET_DEFAULTS:
        session.add(Company(ticker=ticker, name=name, sector=sector, keywords=kw))
    session.commit()


def reset_basket(session: Session) -> None:
    """Deactivate all current companies and load the default basket."""
    for c in get_basket(session):
        c.active = False
    session.commit()
    for ticker, name, sector, kw in BASKET_DEFAULTS:
        upsert_company(session, ticker, name, sector, kw)
        c = session.get(Company, ticker)
        if c:
            c.active = True
    session.commit()
