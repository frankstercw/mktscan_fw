"""trade journal v1

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-17
"""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "trade_journal_entries" in set(inspector.get_table_names()):
        return
    op.create_table(
        "trade_journal_entries",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("ticker", sa.String(10), nullable=False),
        sa.Column("instrument_type", sa.String(10), nullable=False),
        sa.Column("direction", sa.String(10), nullable=False),
        sa.Column("strategy", sa.String(60), nullable=False),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("opened_at", sa.DateTime(), nullable=False),
        sa.Column("closed_at", sa.DateTime()),
        sa.Column("thesis", sa.Text()), sa.Column("tags", sa.Text()), sa.Column("notes", sa.Text()),
        sa.Column("underlying_entry", sa.Float()), sa.Column("underlying_exit", sa.Float()),
        sa.Column("expiration", sa.Date()),
        sa.Column("long_option_type", sa.String(4)), sa.Column("long_strike", sa.Float()),
        sa.Column("short_option_type", sa.String(4)), sa.Column("short_strike", sa.Float()),
        sa.Column("quantity", sa.Float(), nullable=False), sa.Column("multiplier", sa.Integer(), nullable=False),
        sa.Column("entry_type", sa.String(10), nullable=False),
        sa.Column("entry_value", sa.Float(), nullable=False), sa.Column("current_value", sa.Float()), sa.Column("exit_value", sa.Float()),
        sa.Column("entry_fees", sa.Float()), sa.Column("exit_fees", sa.Float()), sa.Column("marked_at", sa.DateTime()),
        sa.Column("planned_max_loss", sa.Float()), sa.Column("stop_condition", sa.Text()),
        sa.Column("profit_target", sa.Text()), sa.Column("planned_exit_date", sa.Date()), sa.Column("exit_reason", sa.String(50)),
        sa.Column("realized_pnl", sa.Float()), sa.Column("return_on_risk_pct", sa.Float()), sa.Column("holding_days", sa.Float()),
        sa.Column("tradeability_prediction_id", sa.Integer()), sa.Column("tradeability_score", sa.Float()), sa.Column("tradeability_label", sa.String(20)),
        sa.Column("regime_snapshot_id", sa.Integer()), sa.Column("regime_score", sa.Float()), sa.Column("regime_label", sa.String(40)),
        sa.Column("options_snapshot_id", sa.Integer()), sa.Column("options_source", sa.String(30)),
        sa.Column("atm_iv", sa.Float()), sa.Column("iv_rank", sa.Float()), sa.Column("iv_percentile", sa.Float()),
        sa.Column("iv_30d", sa.Float()), sa.Column("iv_60d", sa.Float()), sa.Column("iv_90d", sa.Float()),
        sa.Column("put_skew", sa.Float()), sa.Column("call_skew", sa.Float()), sa.Column("expected_move_pct", sa.Float()),
        sa.Column("created_at", sa.DateTime()), sa.Column("updated_at", sa.DateTime()),
    )
    op.create_index("ix_trade_journal_status_opened", "trade_journal_entries", ["status", "opened_at"])
    op.create_index("ix_trade_journal_ticker_opened", "trade_journal_entries", ["ticker", "opened_at"])


def downgrade() -> None:
    op.drop_table("trade_journal_entries")
