"""market regime context layer

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-17
"""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "macro_events" not in tables:
        op.create_table(
            "macro_events",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("source", sa.String(30), nullable=False),
            sa.Column("name", sa.String(200), nullable=False),
            sa.Column("category", sa.String(50)),
            sa.Column("importance", sa.String(20)),
            sa.Column("event_at", sa.DateTime(), nullable=False),
            sa.Column("period", sa.String(50)),
            sa.Column("consensus", sa.String(100)),
            sa.Column("prior", sa.String(100)),
            sa.Column("actual", sa.String(100)),
            sa.Column("updated_at", sa.DateTime()),
            sa.UniqueConstraint("source", "name", "event_at", name="uq_macro_source_name_time"),
        )
        op.create_index("ix_macro_event_at", "macro_events", ["event_at"])
        op.create_index("ix_macro_importance_at", "macro_events", ["importance", "event_at"])

    if "market_regime_snapshots" not in tables:
        op.create_table(
            "market_regime_snapshots",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("snapshot_date", sa.Date(), nullable=False),
            sa.Column("snapped_at", sa.DateTime()),
            sa.Column("regime_score", sa.Float()), sa.Column("regime_label", sa.String(40)),
            sa.Column("confidence", sa.Float()), sa.Column("coverage", sa.Float()), sa.Column("trend_score", sa.Float()),
            sa.Column("spy_price", sa.Float()), sa.Column("spy_return_20d", sa.Float()), sa.Column("spy_return_60d", sa.Float()), sa.Column("spy_trend_score", sa.Float()),
            sa.Column("qqq_price", sa.Float()), sa.Column("qqq_return_20d", sa.Float()), sa.Column("qqq_return_60d", sa.Float()), sa.Column("qqq_trend_score", sa.Float()),
            sa.Column("vix", sa.Float()), sa.Column("vix_change_5d_pct", sa.Float()), sa.Column("vix_percentile_20d", sa.Float()), sa.Column("vix_percentile_1y", sa.Float()), sa.Column("volatility_state", sa.String(40)), sa.Column("volatility_score", sa.Float()),
            sa.Column("breadth_above_20d", sa.Float()), sa.Column("breadth_above_50d", sa.Float()), sa.Column("breadth_above_200d", sa.Float()), sa.Column("breadth_positive_5d", sa.Float()), sa.Column("breadth_positive_20d", sa.Float()), sa.Column("breadth_score", sa.Float()), sa.Column("breadth_universe_size", sa.Integer()),
            sa.Column("two_year_yield", sa.Float()), sa.Column("ten_year_yield", sa.Float()), sa.Column("curve_10y_2y", sa.Float()), sa.Column("ten_year_5d_change_bps", sa.Float()), sa.Column("ten_year_20d_change_bps", sa.Float()), sa.Column("rates_score", sa.Float()),
            sa.Column("next_macro_event", sa.String(200)), sa.Column("next_macro_at", sa.DateTime()), sa.Column("next_macro_importance", sa.String(20)), sa.Column("hours_to_macro", sa.Float()), sa.Column("macro_risk_score", sa.Float()),
            sa.Column("components", sa.Text()),
            sa.UniqueConstraint("snapshot_date", name="uq_market_regime_snapshot_date"),
        )
        op.create_index("ix_regime_snapshot_date", "market_regime_snapshots", ["snapshot_date"])
        op.create_index("ix_regime_snapped_at", "market_regime_snapshots", ["snapped_at"])

    # Attach immutable regime context to each daily prediction for later
    # regime-conditioned validation. Existing rows remain NULL.
    inspector = sa.inspect(bind)
    if "tradeability_outcomes" in inspector.get_table_names():
        cols = {c["name"] for c in inspector.get_columns("tradeability_outcomes")}
        for name, typ in (
            ("regime_score_at_prediction", sa.Float()),
            ("regime_label_at_prediction", sa.String(40)),
            ("regime_confidence_at_prediction", sa.Float()),
        ):
            if name not in cols:
                op.add_column("tradeability_outcomes", sa.Column(name, typ))


def downgrade() -> None:
    op.drop_table("market_regime_snapshots")
    op.drop_table("macro_events")
