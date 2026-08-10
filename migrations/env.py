"""
Alembic environment.

Resolves the target database from the application's own engine factory so that
migrations, the scheduler and the dashboard can never disagree about which
database they are talking to — and so no DSN or password is stored in
alembic.ini.
"""
from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context

# Make the package importable when alembic runs from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mktscan.database import Base, get_engine   # noqa: E402
import mktscan.backtest_incremental             # noqa: E402,F401  (registers tables)

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    engine = get_engine()
    context.configure(
        url=str(engine.url),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # SQLite cannot ALTER most things in place; batch mode rewrites the table.
        render_as_batch=engine.dialect.name == "sqlite",
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = get_engine()
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=engine.dialect.name == "sqlite",
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
