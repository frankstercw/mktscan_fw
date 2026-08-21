"""
mktscan/scheduler.py
APScheduler-based job scheduler.
Supports both cron expressions and simple interval scheduling.
Default: every 15 minutes (configurable in config.yaml or via CLI --interval flag).
"""
from __future__ import annotations
import logging
import re
from typing import Any

log = logging.getLogger(__name__)


def _trigger_from_expr(expr: str, *, default: str):
    """Build an APScheduler trigger from five-part cron or */N shorthand."""
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    cron_expr = (expr or default).strip()
    interval_match = re.match(r"^\*/(\d+)\s+\*\s+\*\s+\*\s+\*$", cron_expr)
    if interval_match:
        mins = int(interval_match.group(1))
        return IntervalTrigger(minutes=mins), f"every {mins} minute{'s' if mins != 1 else ''}"

    parts = cron_expr.split()
    if len(parts) != 5:
        raise ValueError(
            f"Invalid cron expression: '{cron_expr}'. "
            "Use 5-part cron (min hour day month dow) or '*/N * * * *'."
        )
    minute, hour, day, month, day_of_week = parts
    return (
        CronTrigger(
            minute=minute, hour=hour, day=day, month=month,
            day_of_week=day_of_week, timezone="UTC",
        ),
        f"cron: {cron_expr}",
    )


def run_scheduled(cfg: dict[str, Any] | None = None, interval_minutes: int | None = None):
    """
    Start the scheduler. Blocks until interrupted (Ctrl+C).

    Parameters
    ----------
    cfg               : loaded config dict (loads from file if None)
    interval_minutes  : override — run every N minutes regardless of config schedule.
                        If None, reads from config.yaml scraper.schedule.
    """
    try:
        from apscheduler.schedulers.blocking import BlockingScheduler
        from apscheduler.triggers.cron     import CronTrigger
        from apscheduler.triggers.interval import IntervalTrigger
    except ImportError:
        raise RuntimeError("apscheduler not installed. Run: pip install apscheduler")

    from .config  import get_config
    from .engine  import ScrapeEngine
    from . import alerts as alert_module
    from rich.console import Console

    console = Console()
    cfg     = cfg or get_config()

    # ── Determine triggers ───────────────────────────────────────────────────
    if interval_minutes is not None:
        from apscheduler.triggers.interval import IntervalTrigger
        trigger = IntervalTrigger(minutes=interval_minutes)
        trigger_desc = f"every {interval_minutes} minute{'s' if interval_minutes != 1 else ''}"
    else:
        trigger, trigger_desc = _trigger_from_expr(
            cfg.get("scraper", {}).get("schedule", "*/30 13-21 * * 1-5"),
            default="*/30 13-21 * * 1-5",
        )

    price_trigger, price_trigger_desc = _trigger_from_expr(
        cfg.get("scraper", {}).get("price_schedule", "*/10 13-21 * * 1-5"),
        default="*/10 13-21 * * 1-5",
    )

    # ── Job ───────────────────────────────────────────────────────────────────
    run_count = [0]

    def job():
        run_count[0] += 1
        console.print(f"[cyan]▶ Scheduled run #{run_count[0]} starting...[/cyan]")
        alert_module.reset_session_cache()
        engine = ScrapeEngine(cfg)
        try:
            result = engine.run(mode="all")
            console.print(
                f"[green]✓ Run #{run_count[0]} complete[/green] — "
                f"{result['articles_new']} new articles, "
                f"{result['tickers_scored']} scored, "
                f"{result['elapsed_seconds']:.0f}s"
            )
            if result.get("errors"):
                for err in result["errors"][:3]:
                    console.print(f"  [yellow]⚠ {err}[/yellow]")

        except Exception as e:
            console.print(f"[red]✗ Run #{run_count[0]} failed: {e}[/red]")
            log.exception("Scheduled run failed")

    def price_job():
        """Lightweight price + market-regime refresh between full scrapes."""
        console.print("[cyan]▶ Price/regime refresh...[/cyan]")
        try:
            result = ScrapeEngine(cfg).run(mode="prices")
            console.print(
                f"[green]✓ Price refresh complete[/green] — "
                f"{result.get('elapsed_seconds', 0):.0f}s"
            )
            if result.get("errors"):
                for err in result["errors"][:3]:
                    console.print(f"  [yellow]⚠ {err}[/yellow]")
        except Exception as exc:
            console.print(f"[red]✗ Price refresh failed: {exc}[/red]")
            log.exception("Scheduled price refresh failed")

    def iv_seed_job():
        """Seed IV history asynchronously so it can never block the scheduler boot."""
        try:
            from .database import get_session, get_basket
            from .iv_rank import compute_iv_rank, backfill_iv_history

            session = get_session()
            try:
                tickers = [c.ticker for c in get_basket(session)]
                if tickers and compute_iv_rank(session, tickers[0])["basis"] == "none":
                    console.print("[cyan]▶ No IV history found — background seed starting...[/cyan]")
                    backfill_iv_history(session, tickers, days=365)
                    iv_snapshot_job()
                    console.print("[green]✓ IV history seed complete[/green]")
            finally:
                session.close()
        except Exception as exc:
            console.print(f"[yellow]⚠ IV history seed skipped: {exc}[/yellow]")
            log.warning("IV history seed skipped: %s", exc)

    def iv_snapshot_job():
        """
        Record today's ATM implied volatility for the basket.

        This is a genuinely daily job now. It used to sit inside ``job()``, which
        the comment described as "Daily IV snapshot update" while it actually ran
        on the 15-minute scrape trigger — pulling a full option chain per ticker
        96 times a day. That is both wasteful and a reliable way to get rate
        limited by Yahoo, and IV rank only needs one observation per day anyway.

        Scheduled for 21:15 UTC (≈16:15 ET), just after the US close, so the
        chain reflects settled end-of-day quotes.
        """
        console.print("[cyan]▶ Daily IV snapshot...[/cyan]")
        try:
            from .iv_rank import update_iv_snapshot
            from .database import get_session, get_basket, init_db

            init_db()
            session = get_session()
            try:
                tickers = [c.ticker for c in get_basket(session)]
                updated = update_iv_snapshot(session, tickers)
                console.print(
                    f"[green]✓ IV snapshots updated for {updated}/{len(tickers)} tickers[/green]"
                )
            finally:
                session.close()
        except Exception as iv_err:
            console.print(f"[yellow]⚠ IV snapshot update failed: {iv_err}[/yellow]")
            log.warning(f"IV snapshot update failed: {iv_err}")

    def analyst_ratings_job(force: bool = False):
        """Refresh Benzinga analyst actions for basket + open journal positions.

        The APScheduler trigger runs every configured quarter-hour between
        09:00 and 16:59 New York time; this guard narrows execution to the
        actual regular session (09:30–16:00) and handles DST naturally.
        """
        from datetime import datetime as _datetime, time as _time
        from zoneinfo import ZoneInfo

        now_et = _datetime.now(ZoneInfo("America/New_York"))
        if not force and not (_time(9, 30) <= now_et.time().replace(tzinfo=None) <= _time(16, 0)):
            return

        console.print("[cyan]▶ Analyst ratings refresh...[/cyan]")
        try:
            from .analyst_ratings import analyst_watch_tickers, refresh_analyst_ratings
            from .database import get_session, init_db

            init_db()
            session = get_session()
            try:
                tickers = analyst_watch_tickers(session)
                result = refresh_analyst_ratings(session, tickers)
            finally:
                session.close()

            if not result.get("enabled"):
                console.print(f"[dim]Analyst ratings disabled — {result.get('reason', 'no Benzinga key')}[/dim]")
                return

            console.print(
                f"[green]✓ Analyst ratings refreshed[/green] — "
                f"{result.get('tickers', 0)} tickers, "
                f"{result.get('events', 0)} events, "
                f"{result.get('inserted', 0)} new, "
                f"provider={result.get('provider', 'unknown')}"
            )
            for err in result.get("errors", [])[:3]:
                console.print(f"  [yellow]⚠ {err}[/yellow]")
        except Exception as exc:
            console.print(f"[red]✗ Analyst ratings refresh failed: {exc}[/red]")
            log.exception("Analyst ratings refresh failed")

    def backtest_job():
        """Weekly incremental backtest — runs every Sunday at 02:00 UTC."""
        console.print("[cyan]▶ Weekly backtest starting...[/cyan]")
        try:
            from .backtest_incremental import run_incremental_backtest
            from .database import get_session, get_basket

            session = get_session()
            try:
                tickers = [c.ticker for c in get_basket(session)]

                def _cb(level: str, msg: str):
                    console.print(f"  [dim]{msg}[/dim]")

                result = run_incremental_backtest(
                    session=session,
                    tickers=tickers,
                    progress_cb=_cb,
                )
                console.print(
                    f"[green]✓ Backtest complete[/green] — "
                    f"{result['new_observations']} new observations, "
                    f"{result['tickers_processed']} tickers processed"
                )
            finally:
                session.close()
        except Exception as e:
            console.print(f"[red]✗ Backtest failed: {e}[/red]")
            log.exception("Backtest job failed")

    # ── Start ─────────────────────────────────────────────────────────────────
    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(
        job,
        trigger=trigger,
        id="mktscan_scrape",
        name="MktScan full scrape",
        misfire_grace_time=300,
        coalesce=True,
        max_instances=1,
    )
    scheduler.add_job(
        price_job,
        trigger=price_trigger,
        id="mktscan_prices",
        name="MktScan price/regime refresh",
        misfire_grace_time=180,
        coalesce=True,
        max_instances=1,
    )

    from apscheduler.triggers.cron import CronTrigger as _CronTrigger

    analyst_minute = str(cfg.get("scraper", {}).get("analyst_schedule", "*/15")).strip()
    # Accept either a simple minute expression (recommended) or a five-part
    # cron; the job still applies the New York regular-session guard.
    if " " in analyst_minute:
        analyst_parts = analyst_minute.split()
        analyst_minute = analyst_parts[0] if len(analyst_parts) == 5 else "*/15"
    scheduler.add_job(
        analyst_ratings_job,
        trigger=_CronTrigger(
            day_of_week="mon-fri",
            hour="9-16",
            minute=analyst_minute,
            timezone="America/New_York",
        ),
        id="mktscan_analyst_ratings",
        name="MktScan Benzinga analyst ratings",
        misfire_grace_time=300,
        coalesce=True,
        max_instances=1,
    )

    # ── Daily IV snapshot — weekdays at 21:15 UTC (≈16:15 ET, after the close) ─
    scheduler.add_job(
        iv_snapshot_job,
        trigger=_CronTrigger(day_of_week="mon-fri", hour=21, minute=15),
        id="mktscan_iv_snapshot",
        name="MktScan daily IV snapshot",
        misfire_grace_time=3600,
        coalesce=True,
    )

    # ── Weekly backtest job — every Sunday at 02:00 UTC ───────────────────────
    scheduler.add_job(
        backtest_job,
        trigger=_CronTrigger(day_of_week="sun", hour=2, minute=0),
        id="mktscan_backtest",
        name="MktScan weekly backtest",
        misfire_grace_time=3600,
        coalesce=True,
    )

    # Seed IV history shortly after the scheduler starts. A historical Yahoo
    # backfill can take many minutes or get rate-limited, so it must never sit
    # on the critical path before recurring jobs are registered.
    from datetime import datetime as _dt, timedelta as _td
    scheduler.add_job(
        iv_seed_job,
        trigger="date",
        run_date=_dt.utcnow() + _td(seconds=20),
        id="mktscan_iv_seed",
        name="MktScan one-time IV seed check",
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        analyst_ratings_job,
        trigger="date",
        run_date=_dt.utcnow() + _td(seconds=35),
        args=[True],
        id="mktscan_analyst_startup",
        name="MktScan analyst ratings startup seed",
        misfire_grace_time=600,
    )

    # ── Health check HTTP server (required by Railway) ───────────────────────
    import os as _os, threading as _threading
    from http.server import HTTPServer, BaseHTTPRequestHandler

    class _HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok")
        def log_message(self, *args):
            pass

    _health_port = int(_os.environ.get("PORT", 8080))
    try:
        _health_server = HTTPServer(("0.0.0.0", _health_port), _HealthHandler)
        _threading.Thread(target=_health_server.serve_forever, daemon=True).start()
        console.print(f"[green]✓ Health server on port {_health_port}[/green]")
    except Exception as _he:
        console.print(f"[yellow]⚠ Health server error: {_he}[/yellow]")

    console.print(f"[bold cyan]MktScan Scheduler[/bold cyan] — full refresh {trigger_desc} (UTC)")
    console.print(f"[dim]  + price/regime refresh {price_trigger_desc} (UTC)[/dim]")
    console.print("[dim]  + analyst ratings every 15m during 09:30–16:00 America/New_York[/dim]")
    console.print("[dim]  + daily IV snapshot at 21:15 UTC (weekdays)[/dim]")
    console.print("[dim]  + weekly backtest Sunday 02:00 UTC[/dim]")
    console.print("[dim]Press Ctrl+C to stop[/dim]")

    # One initial full run gives the dashboard fresh data immediately. The
    # potentially slow IV backfill now runs asynchronously after the scheduler
    # has started, so it cannot prevent recurring jobs from being registered.
    console.print("[cyan]Running initial scrape on startup...[/cyan]")
    job()

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        console.print("\n[yellow]Scheduler stopped.[/yellow]")
        scheduler.shutdown(wait=False)


