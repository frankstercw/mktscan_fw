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

    # ── Determine trigger ─────────────────────────────────────────────────────
    if interval_minutes is not None:
        # Explicit interval override
        trigger      = IntervalTrigger(minutes=interval_minutes)
        trigger_desc = f"every {interval_minutes} minute{'s' if interval_minutes != 1 else ''}"
    else:
        cron_expr = cfg.get("scraper", {}).get("schedule", "*/15 * * * *")

        # Detect simple interval patterns like "*/15 * * * *" or "*/5 * * * *"
        interval_match = re.match(r"^\*/(\d+)\s+\*\s+\*\s+\*\s+\*$", cron_expr.strip())
        if interval_match:
            mins    = int(interval_match.group(1))
            trigger = IntervalTrigger(minutes=mins)
            trigger_desc = f"every {mins} minute{'s' if mins != 1 else ''}"
        else:
            # Full cron expression
            parts = cron_expr.strip().split()
            if len(parts) != 5:
                raise ValueError(
                    f"Invalid cron expression: '{cron_expr}'. "
                    "Use 5-part cron (min hour day month dow) or '*/N * * * *' for interval."
                )
            minute, hour, day, month, day_of_week = parts
            trigger = CronTrigger(
                minute=minute, hour=hour,
                day=day, month=month, day_of_week=day_of_week,
            )
            trigger_desc = f"cron: {cron_expr}"

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
        misfire_grace_time=120,   # if a run is missed by <2min, still execute it
        coalesce=True,            # if multiple missed runs pile up, only run once
    )

    from apscheduler.triggers.cron import CronTrigger as _CronTrigger

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

    console.print(f"[bold cyan]MktScan Scheduler[/bold cyan] — {trigger_desc} (UTC)")
    console.print("[dim]  + daily IV snapshot at 21:15 UTC (weekdays)[/dim]")
    console.print("[dim]  + weekly backtest Sunday 02:00 UTC[/dim]")
    console.print("[dim]Press Ctrl+C to stop[/dim]")

    # Run once immediately on startup so you don't wait for the first interval.
    console.print("[cyan]Running initial scrape on startup...[/cyan]")
    job()

    # Seed IV history on first boot. Without at least one snapshot the IV rank —
    # and therefore the whole strategy selector — has nothing to work with, and
    # waiting for 21:15 UTC would leave the tool degraded until then.
    try:
        from .database import get_session, get_basket
        from .iv_rank import compute_iv_rank

        session = get_session()
        try:
            tickers = [c.ticker for c in get_basket(session)]
            if tickers and compute_iv_rank(session, tickers[0])["basis"] == "none":
                console.print("[cyan]No IV history found — seeding...[/cyan]")
                from .iv_rank import backfill_iv_history
                backfill_iv_history(session, tickers, days=365)
                iv_snapshot_job()
        finally:
            session.close()
    except Exception as e:
        console.print(f"[yellow]⚠ IV history seed skipped: {e}[/yellow]")

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        console.print("\n[yellow]Scheduler stopped.[/yellow]")
        scheduler.shutdown(wait=False)


