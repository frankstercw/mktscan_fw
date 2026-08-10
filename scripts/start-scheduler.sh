#!/usr/bin/env bash
#
# Scheduler entrypoint.
#
# The scheduler owns the schema. It is the only service that runs migrations, so
# the dashboard cannot race it: two containers calling `alembic upgrade head`
# (or `create_all`) against the same Postgres at boot can deadlock on the DDL
# lock, and on the losing side you get a half-applied migration.
set -euo pipefail

echo "── MktScan scheduler starting ──────────────────────────────"

if [[ -z "${DATABASE_URL:-}" ]]; then
  echo "WARNING: DATABASE_URL is not set."
  echo "  Falling back to SQLite inside this container. That file is NOT shared"
  echo "  with the dashboard service and is lost on redeploy. Link a Postgres"
  echo "  database before relying on any output."
fi

echo "→ Applying database migrations..."
alembic upgrade head

echo "→ Seeding basket if empty..."
python -m mktscan basket >/dev/null 2>&1 || true

# Seed IV history on first boot only. Without it IV rank is unavailable and the
# strategy selector runs on its least-informed fallback branch. This is a slow,
# one-off operation (one option chain fetch per ticker); it is skipped
# automatically once history exists.
echo "→ Checking implied-volatility history..."
python - <<'PY'
import sys
from mktscan.database import get_session, get_basket, init_db
from mktscan.iv_rank import backfill_iv_history, compute_iv_rank, update_iv_snapshot

init_db()
session = get_session()
try:
    tickers = [c.ticker for c in get_basket(session)]
    if not tickers:
        print("   no basket yet — skipping")
        sys.exit(0)
    if compute_iv_rank(session, tickers[0])["basis"] == "none":
        print(f"   no IV history — seeding {len(tickers)} tickers (this takes a few minutes)")
        backfill_iv_history(session, tickers, days=365)
        update_iv_snapshot(session, tickers)
    else:
        print("   IV history present")
except Exception as e:
    print(f"   IV seed skipped: {e}")
finally:
    session.close()
PY

echo "→ Starting scheduler"
exec python -m mktscan schedule
