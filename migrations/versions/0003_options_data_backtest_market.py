"""options data v1, backtest v2, options market v2

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-17
"""
from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "historical_option_quotes" not in tables:
        op.create_table(
            "historical_option_quotes",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("ticker", sa.String(10), nullable=False),
            sa.Column("trade_date", sa.Date(), nullable=False),
            sa.Column("expiration", sa.Date(), nullable=False),
            sa.Column("strike", sa.Float(), nullable=False),
            sa.Column("right", sa.String(1), nullable=False),
            sa.Column("underlying_price", sa.Float()),
            sa.Column("bid", sa.Float()), sa.Column("ask", sa.Float()),
            sa.Column("model_value", sa.Float()),
            sa.Column("volume", sa.Integer()), sa.Column("open_interest", sa.Integer()),
            sa.Column("implied_volatility", sa.Float()),
            sa.Column("delta", sa.Float()), sa.Column("gamma", sa.Float()),
            sa.Column("theta", sa.Float()), sa.Column("vega", sa.Float()),
            sa.Column("source", sa.String(30), nullable=False),
            sa.Column("created_at", sa.DateTime()),
            sa.UniqueConstraint("ticker", "trade_date", "expiration", "strike", "right", "source",
                                name="uq_hist_option_quote"),
        )
        op.create_index("ix_hist_option_ticker_date", "historical_option_quotes", ["ticker", "trade_date"])
        op.create_index("ix_hist_option_contract", "historical_option_quotes", ["ticker", "expiration", "strike", "right"])

    if "options_market_snapshots" not in tables:
        op.create_table(
            "options_market_snapshots",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("ticker", sa.String(10), nullable=False),
            sa.Column("snapshot_date", sa.Date(), nullable=False),
            sa.Column("snapped_at", sa.DateTime()),
            sa.Column("source", sa.String(30), nullable=False),
            sa.Column("spot", sa.Float()), sa.Column("atm_iv", sa.Float()),
            sa.Column("iv_rank_1y", sa.Float()), sa.Column("iv_percentile_1y", sa.Float()),
            sa.Column("iv_30d", sa.Float()), sa.Column("iv_60d", sa.Float()), sa.Column("iv_90d", sa.Float()),
            sa.Column("term_slope_30_60", sa.Float()), sa.Column("term_slope_60_90", sa.Float()),
            sa.Column("term_state", sa.String(30)),
            sa.Column("put_25d_iv", sa.Float()), sa.Column("call_25d_iv", sa.Float()),
            sa.Column("put_skew", sa.Float()), sa.Column("call_skew", sa.Float()),
            sa.Column("expected_move_pct", sa.Float()), sa.Column("expected_move_dollars", sa.Float()),
            sa.Column("confidence", sa.Float()), sa.Column("components", sa.Text()),
            sa.UniqueConstraint("ticker", "snapshot_date", name="uq_options_market_ticker_date"),
        )
        op.create_index("ix_options_market_ticker_date", "options_market_snapshots", ["ticker", "snapshot_date"])

    # Backtest v2 provenance + exact historical structure economics.
    if "backtest_observations" in tables:
        cols = {c["name"] for c in inspector.get_columns("backtest_observations")}
        additions = [
            ("option_data_source", sa.String(30)),
            ("option_expiration", sa.Date),
            ("option_long_strike", sa.Float),
            ("option_short_strike", sa.Float),
            ("option_entry_debit", sa.Float),
            ("option_exit_value", sa.Float),
        ]
        for name, type_ in additions:
            if name not in cols:
                op.add_column("backtest_observations", sa.Column(name, type_()))


def downgrade() -> None:
    op.drop_table("options_market_snapshots")
    op.drop_table("historical_option_quotes")
