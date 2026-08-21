"""
mktscan/config.py
Loads and validates configuration from config.yaml / config.local.yaml
"""
from __future__ import annotations
import os
import yaml
from pathlib import Path
from typing import Any


_CONFIG: dict[str, Any] | None = None


def load_config(path: str | None = None) -> dict[str, Any]:
    """
    Load config. Priority:
    1. Explicit path argument
    2. MKTSCAN_CONFIG env var
    3. config.local.yaml  (gitignored — put secrets here)
    4. config.yaml        (defaults / template)
    """
    global _CONFIG
    candidates = []
    if path:
        candidates.append(Path(path))

    env_path = os.environ.get("MKTSCAN_CONFIG")
    if env_path:
        candidates.append(Path(env_path))

    # Walk up from cwd to find config files
    cwd = Path.cwd()
    for root in [cwd, cwd.parent, Path(__file__).parent.parent]:
        candidates.append(root / "config.local.yaml")
        candidates.append(root / "config.yaml")

    cfg: dict[str, Any] = {}
    loaded = False
    for p in candidates:
        if p.exists():
            with open(p) as f:
                cfg = yaml.safe_load(f) or {}
            loaded = True
            break

    if not loaded:
        raise FileNotFoundError(
            "No config.yaml found. Copy config.yaml and fill in your API keys."
        )

    # Allow env var overrides for secrets
    _apply_env_overrides(cfg)
    _CONFIG = cfg
    return cfg


# Every secret the tool understands, and the env var that supplies it.
# Secrets belong in the environment, not in a YAML file that gets committed —
# config.yaml previously shipped with placeholder slots for an SMTP password, a
# Slack webhook and four API keys, which is an invitation to paste real ones in
# and commit them.
SECRET_ENV_MAP: dict[str, tuple[str, ...]] = {
    "MKTSCAN_AV_KEY":        ("sources", "alpha_vantage", "api_key"),
    "MKTSCAN_BENZINGA_KEY":  ("sources", "benzinga", "api_key"),
    "MKTSCAN_FINVIZ_COOKIE": ("sources", "finviz", "session_cookie"),
    "MKTSCAN_WSJ_COOKIE":    ("sources", "wsj", "session_cookie"),
    "MKTSCAN_OPENAI_KEY":    ("sentiment", "openai_api_key"),
    "FINNHUB_API_KEY":       ("sources", "finnhub", "api_key"),
    "MKTSCAN_FINNHUB_KEY":   ("sources", "finnhub", "api_key"),
    "MKTSCAN_SMTP_PASSWORD": ("alerts", "email", "password"),
    "MKTSCAN_SLACK_WEBHOOK": ("alerts", "slack_webhook"),
}

# Anything still matching one of these is a template value, not a real secret.
_PLACEHOLDER_PREFIXES = ("YOUR_", "your-", "<", "CHANGE")


def _is_placeholder(value: Any) -> bool:
    return isinstance(value, str) and value.strip().startswith(_PLACEHOLDER_PREFIXES)


def _apply_env_overrides(cfg: dict) -> None:
    """Override secrets from environment variables and scrub unset placeholders."""
    for env_var, key_path in SECRET_ENV_MAP.items():
        val = os.environ.get(env_var)
        if val:
            node = cfg
            for k in key_path[:-1]:
                node = node.setdefault(k, {})
            node[key_path[-1]] = val

    # Blank out any remaining placeholder so downstream code sees an empty
    # credential and disables the source, rather than sending "YOUR_AV_KEY" to a
    # live API and logging the failure every run.
    for key_path in set(SECRET_ENV_MAP.values()):
        node = cfg
        for k in key_path[:-1]:
            if not isinstance(node, dict):
                break
            node = node.get(k, {})
        if isinstance(node, dict) and _is_placeholder(node.get(key_path[-1])):
            node[key_path[-1]] = ""

    # ── Non-secret behavioural overrides ─────────────────────────────────────
    # These matter for container deployments, where editing config.yaml means
    # rebuilding the image. Setting them as environment variables lets the same
    # image run as either service with different behaviour.
    behaviour = {
        "MKTSCAN_SENTIMENT_MODEL": ("sentiment", "model"),
        "MKTSCAN_SCHEDULE":        ("scraper", "schedule"),
        "MKTSCAN_PRICE_SCHEDULE":  ("scraper", "price_schedule"),
        "MKTSCAN_ANALYST_SCHEDULE": ("scraper", "analyst_schedule"),
        "MKTSCAN_DELAY_SECONDS":   ("scraper", "delay_seconds"),
        "MKTSCAN_MAX_ARTICLES":    ("scraper", "max_articles_per_source"),
        "MKTSCAN_LOG_LEVEL":       ("logging", "level"),
    }
    for env_var, key_path in behaviour.items():
        raw = os.environ.get(env_var)
        if not raw:
            continue
        value: Any = raw
        if env_var == "MKTSCAN_DELAY_SECONDS":
            try:
                value = float(raw)
            except ValueError:
                continue
        elif env_var == "MKTSCAN_MAX_ARTICLES":
            try:
                value = int(raw)
            except ValueError:
                continue
        node = cfg
        for k in key_path[:-1]:
            node = node.setdefault(k, {})
        node[key_path[-1]] = value

    # ── Railway / cloud PostgreSQL ───────────────────────────────────────────
    # DATABASE_URL is injected automatically when a Postgres service is linked.
    # Postgres is effectively mandatory in a multi-service deployment: the
    # dashboard and scheduler run in separate containers with separate
    # filesystems, so a SQLite file cannot be shared between them — each would
    # silently get its own empty database.
    db_url = os.environ.get("DATABASE_URL")
    if db_url:
        # Railway hands out postgres://; SQLAlchemy 2.x removed that alias.
        db_url = db_url.replace("postgres://", "postgresql://", 1)
        cfg.setdefault("storage", {})["type"]          = "postgres"
        cfg.setdefault("storage", {})["postgres_dsn"]  = db_url

    # PORT is set by Railway for the web service
    port = os.environ.get("PORT")
    if port:
        cfg.setdefault("server", {})["port"] = int(port)


def get_config() -> dict[str, Any]:
    """Return cached config, loading if needed."""
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = load_config()
    return _CONFIG


def get(key_path: str, default: Any = None) -> Any:
    """
    Dot-notation accessor. E.g. get('sources.benzinga.api_key')
    """
    cfg = get_config()
    parts = key_path.split(".")
    node = cfg
    for p in parts:
        if not isinstance(node, dict) or p not in node:
            return default
        node = node[p]
    return node
