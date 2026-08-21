"""analyst ratings and journal analyst snapshot

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-20
"""
from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def _column_names(inspector, table):
    return {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "analyst_rating_events" not in tables:
        op.create_table(
            "analyst_rating_events",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("external_id", sa.String(100), nullable=False, unique=True),
            sa.Column("ticker", sa.String(15), nullable=False),
            sa.Column("published_at", sa.DateTime(), nullable=False),
            sa.Column("firm", sa.String(200)),
            sa.Column("analyst_name", sa.String(200)),
            sa.Column("action_company", sa.String(50)),
            sa.Column("action_pt", sa.String(50)),
            sa.Column("rating_prior", sa.String(100)),
            sa.Column("rating_current", sa.String(100)),
            sa.Column("pt_prior", sa.Float()),
            sa.Column("pt_current", sa.Float()),
            sa.Column("importance", sa.Integer()),
            sa.Column("url", sa.Text()),
            sa.Column("source", sa.String(30), nullable=False),
            sa.Column("raw_json", sa.Text()),
            sa.Column("created_at", sa.DateTime()),
            sa.Column("updated_at", sa.DateTime()),
        )
        op.create_index(
            "ix_analyst_rating_ticker_published",
            "analyst_rating_events",
            ["ticker", "published_at"],
        )
        op.create_index(
            "ix_analyst_rating_published",
            "analyst_rating_events",
            ["published_at"],
        )

    inspector = sa.inspect(bind)
    if "trade_journal_entries" in set(inspector.get_table_names()):
        cols = _column_names(inspector, "trade_journal_entries")
        additions = [
            ("analyst_snapshot_at", sa.DateTime()),
            ("analyst_momentum_score", sa.Float()),
            ("analyst_momentum_state", sa.String(30)),
            ("analyst_events_30d", sa.Integer()),
            ("analyst_upgrades_30d", sa.Integer()),
            ("analyst_downgrades_30d", sa.Integer()),
            ("analyst_pt_raises_30d", sa.Integer()),
            ("analyst_pt_cuts_30d", sa.Integer()),
        ]
        for name, typ in additions:
            if name not in cols:
                op.add_column("trade_journal_entries", sa.Column(name, typ))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "trade_journal_entries" in tables:
        cols = _column_names(inspector, "trade_journal_entries")
        for name in [
            "analyst_pt_cuts_30d",
            "analyst_pt_raises_30d",
            "analyst_downgrades_30d",
            "analyst_upgrades_30d",
            "analyst_events_30d",
            "analyst_momentum_state",
            "analyst_momentum_score",
            "analyst_snapshot_at",
        ]:
            if name in cols:
                op.drop_column("trade_journal_entries", name)
    if "analyst_rating_events" in tables:
        op.drop_table("analyst_rating_events")
