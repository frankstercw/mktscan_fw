"""Options pipeline fixes: IV history, earnings units, daily predictions, dedup

Brings an existing database up to the current models. Every change here was
previously invisible on a live database, because ``Base.metadata.create_all()``
only creates whole tables — it never adds a column — and the call sites read new
columns through ``getattr(row, name, None)``, which silently returns None when
the column is absent. That is exactly how the IV-rank pipeline stayed broken.

Changes
───────
* ``iv_snapshots``   — new table. Previously declared on a *separate*
                       declarative_base in iv_rank.py, so init_db() never
                       created it and every IV write raised.
* ``price_snapshots`` — iv_52w_low / iv_52w_high / iv_rank / iv_percentile /
                       iv_history_days. The signal code already read the first
                       two; they did not exist.
* ``articles``       — headline_key, for collapsing syndicated wire copy that
                       URL-level dedup misses. Existing rows are backfilled.
* ``earnings_events`` — is_upcoming / updated_at, supporting date-keyed upserts
                       instead of the frozen literal period "Upcoming".
* ``tradeability_outcomes`` — prediction_date plus a UNIQUE (ticker,
                       prediction_date). This is what caps the 15-minute
                       scheduler at one prediction per ticker per day instead of
                       ~96 duplicates that all resolve to the same return.

Revision ID: 0001
Revises:
Create Date: 2026-08-09
"""
from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if table not in inspector.get_table_names():
        return set()
    return {c["name"] for c in inspector.get_columns(table)}


def _add(table: str, column: sa.Column) -> None:
    """Idempotent ALTER — safe on databases already patched by ensure_schema()."""
    if table in sa.inspect(op.get_bind()).get_table_names() and column.name not in _columns(table):
        op.add_column(table, column)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    # ── Fresh database: create the whole schema and stop ─────────────────────
    # Everything below this point is a *patch* to an existing database, guarded
    # by `if "<table>" in tables`. On an empty database every one of those guards
    # is False, so without this branch the migration would create almost nothing,
    # stamp itself as applied, and leave the app relying on create_all() to
    # quietly build the schema afterwards — which works by accident and hides
    # real failures. A new deployment gets the full schema here, from the models.
    core_tables = {"companies", "articles", "sentiment_scores", "price_snapshots"}
    if not (core_tables & tables):
        from mktscan.database import Base
        import mktscan.backtest_incremental  # noqa: F401  registers backtest tables

        Base.metadata.create_all(bind)
        return

    # ── iv_snapshots ─────────────────────────────────────────────────────────
    if "iv_snapshots" not in tables:
        op.create_table(
            "iv_snapshots",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("ticker", sa.String(length=10), nullable=False),
            sa.Column("snapshot_date", sa.Date(), nullable=False),
            sa.Column("iv_atm", sa.Float()),
            sa.Column("iv_proxy", sa.Float()),
            sa.Column("iv_used", sa.Float()),
            sa.Column("source", sa.String(length=10)),
            sa.Column("dte", sa.Integer()),
            sa.Column("created_at", sa.DateTime()),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("ticker", "snapshot_date", name="uq_iv_ticker_date"),
        )
        op.create_index("ix_iv_ticker_date", "iv_snapshots", ["ticker", "snapshot_date"])

    # ── price_snapshots: IV rank columns ─────────────────────────────────────
    for name in ("iv_52w_low", "iv_52w_high", "iv_rank", "iv_percentile"):
        _add("price_snapshots", sa.Column(name, sa.Float()))
    _add("price_snapshots", sa.Column("iv_history_days", sa.Integer()))

    # Columns added since first deploy that create_all() never applied.
    for name in ("target_price", "short_ratio", "short_pct_float",
                 "implied_volatility", "beta", "volume_ratio"):
        _add("price_snapshots", sa.Column(name, sa.Float()))
    _add("price_snapshots", sa.Column("avg_volume_30d", sa.Integer()))

    # ── articles: headline dedup key ─────────────────────────────────────────
    _add("articles", sa.Column("headline_key", sa.String(length=40)))
    if "articles" in tables:
        existing_indexes = {i["name"] for i in inspector.get_indexes("articles")}
        if "ix_articles_headline_key" not in existing_indexes:
            op.create_index("ix_articles_headline_key", "articles", ["headline_key"])
        if "ix_articles_ticker_scraped" not in existing_indexes:
            op.create_index("ix_articles_ticker_scraped", "articles", ["ticker", "scraped_at"])
        if "ix_articles_ticker_headline" not in existing_indexes:
            op.create_index("ix_articles_ticker_headline", "articles", ["ticker", "headline_key"])

        # URLs were stored as "" when absent, which collides under
        # UNIQUE(source, url) and aborted whole insert batches. NULLs do not
        # collide, so normalise the existing rows.
        op.execute("UPDATE articles SET url = NULL WHERE url = ''")

    # ── earnings_events ──────────────────────────────────────────────────────
    _add("earnings_events", sa.Column("is_upcoming", sa.Boolean()))
    _add("earnings_events", sa.Column("updated_at", sa.DateTime()))
    if "earnings_events" in tables:
        existing_indexes = {i["name"] for i in inspector.get_indexes("earnings_events")}
        if "ix_earnings_ticker_date" not in existing_indexes:
            op.create_index("ix_earnings_ticker_date", "earnings_events",
                            ["ticker", "report_date"])
        # Postgres has a real BOOLEAN type and rejects `= 1` with
        # "column is of type boolean but expression is of type integer",
        # which aborts the whole migration transaction. SQLite stores booleans
        # as integers and accepts either. Use the dialect's own literal.
        true_literal = "true" if bind.dialect.name != "sqlite" else "1"
        op.execute(
            f"UPDATE earnings_events SET is_upcoming = {true_literal} "
            f"WHERE period = 'Upcoming'"
        )
        # surprise_pct held yfinance's epsDifference — an absolute dollar amount
        # consumed downstream as a percentage. The stored values are not
        # convertible without the estimate, so recompute where we can and clear
        # the rest rather than leaving wrong numbers in place.
        op.execute(
            "UPDATE earnings_events "
            "SET surprise_pct = CASE "
            "  WHEN eps_estimate IS NOT NULL AND eps_actual IS NOT NULL "
            "       AND ABS(eps_estimate) >= 0.01 "
            "  THEN (eps_actual - eps_estimate) / ABS(eps_estimate) * 100 "
            "  ELSE NULL END"
        )

    # ── tradeability_outcomes: one prediction per ticker per day ─────────────
    if "tradeability_outcomes" in tables:
        if "prediction_date" not in _columns("tradeability_outcomes"):
            op.add_column("tradeability_outcomes", sa.Column("prediction_date", sa.Date()))
            op.execute("UPDATE tradeability_outcomes SET prediction_date = DATE(predicted_at)")

            # Collapse the historical pseudo-replicates: keep the last prediction
            # of each day and drop the rest, so accuracy statistics are computed
            # over independent observations.
            op.execute(
                "DELETE FROM tradeability_outcomes WHERE id NOT IN ("
                "  SELECT MAX(id) FROM tradeability_outcomes "
                "  GROUP BY ticker, prediction_date)"
            )

        existing_indexes = {i["name"] for i in inspector.get_indexes("tradeability_outcomes")}
        if "ix_outcome_ticker_date" not in existing_indexes:
            op.create_index("ix_outcome_ticker_date", "tradeability_outcomes",
                            ["ticker", "prediction_date"])
        uniques = {u["name"] for u in inspector.get_unique_constraints("tradeability_outcomes")}
        if "uq_outcome_ticker_day" not in uniques:
            with op.batch_alter_table("tradeability_outcomes") as batch:
                batch.create_unique_constraint(
                    "uq_outcome_ticker_day", ["ticker", "prediction_date"]
                )

    # ── backtest_observations: option-level results ──────────────────────────
    for name, coltype in (
        ("coverage", sa.Float()), ("strategy", sa.String(length=30)),
        ("option_pnl_pct", sa.Float()), ("realized_vol", sa.Float()),
    ):
        _add("backtest_observations", sa.Column(name, coltype))
    _add("backtest_observations", sa.Column("option_win", sa.Boolean()))

    for name in ("median_return_pct", "benchmark_avg_return_pct",
                 "benchmark_win_rate_pct", "excess_return_pct",
                 "option_avg_pnl_pct", "option_win_rate"):
        _add("backtest_summary", sa.Column(name, sa.Float()))

    # Old backtest rows were produced by a signal that no longer exists (its RSI
    # bands differed from production). Leaving them mixed with new rows would
    # pool two different models into one summary.
    if "backtest_observations" in tables:
        op.execute("DELETE FROM backtest_observations")
        op.execute("DELETE FROM backtest_summary")


def downgrade() -> None:
    # Deliberately not implemented. This migration destroys data that the new
    # schema cannot faithfully reconstruct (duplicate daily predictions, the
    # dollar-denominated surprise values, the old backtest observations), so a
    # mechanical downgrade would silently produce a database that looks valid
    # and is not. Restore from a backup instead.
    raise NotImplementedError(
        "Downgrade is not supported — restore from a backup taken before upgrade."
    )
