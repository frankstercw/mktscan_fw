#!/usr/bin/env bash
#
# Dashboard entrypoint.
#
# Deliberately does NOT run migrations — the scheduler owns the schema. If both
# services migrate at boot they contend for the same DDL lock. The dashboard
# waits for the expected tables instead, so a first deploy where the dashboard
# wins the startup race shows a clear message rather than a stack trace.
set -euo pipefail

echo "── MktScan dashboard starting ──────────────────────────────"

if [[ -z "${DATABASE_URL:-}" ]]; then
  echo "WARNING: DATABASE_URL is not set — this container will use its own"
  echo "  empty SQLite file and show no data from the scheduler."
fi

# Wait briefly for the scheduler's migration to land on a cold start.
python - <<'PY' || true
import time
from sqlalchemy import inspect
from mktscan.database import get_engine

REQUIRED = {"companies", "articles", "price_snapshots", "iv_snapshots"}

for attempt in range(12):                       # ~60s
    try:
        tables = set(inspect(get_engine()).get_table_names())
        missing = REQUIRED - tables
        if not missing:
            print("   schema ready")
            break
        print(f"   waiting for schema, missing: {', '.join(sorted(missing))}")
    except Exception as e:
        print(f"   database not reachable yet ({e})")
    time.sleep(5)
else:
    print("   proceeding anyway — the dashboard will show an empty-state message")
PY

echo "→ Starting Streamlit on port ${PORT:-8501}"
exec streamlit run dashboard/app.py \
  --server.port "${PORT:-8501}" \
  --server.address 0.0.0.0 \
  --server.headless true \
  --server.enableCORS false \
  --server.enableXsrfProtection false \
  --browser.gatherUsageStats false
