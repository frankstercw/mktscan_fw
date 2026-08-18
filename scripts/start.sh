#!/usr/bin/env bash
#
# Single entrypoint for both services.
#
# Which role this container plays is decided by the MKTSCAN_ROLE environment
# variable, not by a different Dockerfile or start command:
#
#     MKTSCAN_ROLE=scheduler   → background worker (owns the schema)
#     MKTSCAN_ROLE=dashboard   → Streamlit web UI  (default)
#
# Why: the previous setup needed each Railway service to be pointed at a
# railway.toml inside services/<name>/. Railway only auto-detects that file at
# the repo root and the config path does not follow the Root Directory setting,
# so if the absolute path is not entered exactly, Railway silently falls back to
# the root Dockerfile and start command — and you get two dashboards, no
# scheduler, and therefore no migrations and an empty database. One env var has
# far fewer ways to go wrong.
set -uo pipefail

ROLE="${MKTSCAN_ROLE:-dashboard}"

echo "══════════════════════════════════════════════════════════"
echo " MktScan — role: ${ROLE}"
echo "══════════════════════════════════════════════════════════"

if [[ -z "${DATABASE_URL:-}" ]]; then
  echo "⚠ DATABASE_URL is not set."
  echo "  This container will use its own SQLite file, which is NOT shared with"
  echo "  the other service and is lost on redeploy. Link a Postgres database and"
  echo "  set DATABASE_URL=\${{Postgres.DATABASE_URL}} on both services."
else
  echo "✓ DATABASE_URL is set"
fi

# ── Schema ────────────────────────────────────────────────────────────────────
# Only the scheduler migrates. Two containers running `alembic upgrade head`
# against the same Postgres at boot contend for the DDL lock, and the loser can
# leave a partially applied migration.
if [[ "${ROLE}" == "scheduler" ]]; then
  echo "→ Applying database migrations..."
  if alembic upgrade head; then
    echo "✓ Migrations applied"
  else
    # Deliberately not fatal. A crash-looping container gives you no logs to read
    # and no shell to debug from. Fall back to building the schema directly from
    # the models, and say loudly what happened.
    echo "⚠ alembic failed — falling back to create_all() + ensure_schema()"
    python - <<'PY'
from mktscan.database import init_db, ensure_schema
init_db()
applied = ensure_schema()
print(f"  fallback schema ready ({len(applied)} column(s) added)")
PY
  fi
fi

# ── Diagnostics ───────────────────────────────────────────────────────────────
# Printed on every boot so the Railway logs always answer "is the database
# reachable, does the schema exist, is there any data".
python -m mktscan doctor || echo "⚠ doctor check failed (continuing)"

# ── Role dispatch ─────────────────────────────────────────────────────────────
if [[ "${ROLE}" == "scheduler" ]]; then

  echo "→ Seeding basket if empty..."
  python -m mktscan basket >/dev/null 2>&1 || true

  echo "→ IV history seed will run in background after scheduler startup"

  echo "→ Starting scheduler"
  exec python -m mktscan schedule

else

  echo "→ Starting Streamlit on port ${PORT:-8501}"
  exec streamlit run dashboard/app.py \
    --server.port "${PORT:-8501}" \
    --server.address 0.0.0.0 \
    --server.headless true \
    --server.enableCORS false \
    --server.enableXsrfProtection false \
    --browser.gatherUsageStats false

fi
