"""
mktscan/cli.py
Command-line interface. Entry point: `python -m mktscan` or `mktscan`
"""
from __future__ import annotations
import logging
import sys

import click
from rich.console import Console
from rich.table import Table
from rich.text import Text
from rich import print as rprint

console = Console()


def _setup_logging(verbose: bool):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        level=level,
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    # Silence noisy third-party loggers
    for noisy in ("urllib3", "transformers", "torch", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _progress(level: str, msg: str):
    colors = {"ok": "green", "warn": "yellow", "err": "red", "info": "cyan"}
    color = colors.get(level, "white")
    console.print(f"  [{color}]{level.upper():4s}[/{color}]  {msg}")


@click.group()
@click.option("--verbose", "-v", is_flag=True, help="Verbose logging")
@click.pass_context
def cli(ctx, verbose):
    """MktScan — Market Intelligence Scraper & Sentiment Engine"""
    ctx.ensure_object(dict)
    ctx.obj["verbose"] = verbose
    _setup_logging(verbose)


@cli.command()
@click.option("--mode", "-m",
              type=click.Choice(["all", "news", "earnings", "prices"]),
              default="all", show_default=True,
              help="What to scrape")
@click.option("--config", "-c", default=None, help="Path to config file")
@click.pass_context
def run(ctx, mode, config):
    """Run a scrape now."""
    from .config import load_config
    from .engine import ScrapeEngine

    cfg = load_config(config)
    console.rule("[bold cyan]MktScan Run[/bold cyan]")
    engine = ScrapeEngine(cfg=cfg, progress_cb=_progress)

    try:
        result = engine.run(mode=mode)
    except Exception as e:
        console.print(f"[red]Run failed: {e}[/red]")
        raise SystemExit(1)

    console.rule()
    rprint(f"[green]✓ Done[/green] — "
           f"{result['articles_new']} new articles, "
           f"{result['tickers_scored']} tickers scored, "
           f"{result['elapsed_seconds']:.0f}s")

    if result.get("sentiment"):
        _print_sentiment_table(result["sentiment"])


@cli.command()
@click.option("--config",   "-c", default=None, help="Path to config file")
@click.option("--interval", "-i", default=None, type=int,
              help="Run every N minutes (overrides config schedule)")
@click.pass_context
def schedule(ctx, config, interval):
    """Start the scheduler (blocks). Runs every 15 min by default."""
    from .config import load_config
    from .scheduler import run_scheduled

    cfg  = load_config(config)
    mins = interval or None
    desc = f"every {mins} minutes" if mins else cfg.get("scraper", {}).get("schedule", "*/15 * * * *")
    console.print(f"[cyan]Starting scheduler — {desc}[/cyan]")
    run_scheduled(cfg, interval_minutes=mins)


@cli.command()
@click.option("--days", "-d", default=7, show_default=True, help="Days of history")
@click.pass_context
def scores(ctx, days):
    """Show latest sentiment scores from the database."""
    from .database import init_db, get_session, get_latest_scores

    init_db()
    session = get_session()
    rows = get_latest_scores(session)
    session.close()

    if not rows:
        console.print("[yellow]No scores found. Run `mktscan run` first.[/yellow]")
        return

    table = Table(title="Latest Sentiment Scores", show_header=True, header_style="bold cyan")
    table.add_column("Ticker", style="bold")
    table.add_column("Score")
    table.add_column("Label")
    table.add_column("Articles")
    table.add_column("Scored At")

    for row in rows:
        score_text = Text(f"{row.score:+.3f}")
        score_text.stylize("green" if row.score > 0.1 else ("red" if row.score < -0.1 else "yellow"))
        table.add_row(
            row.ticker,
            score_text,
            row.lbl,
            str(row.article_count),
            str(row.scored_at)[:16],
        )

    console.print(table)


@cli.command()
@click.option("--refresh", is_flag=True, help="Fetch and store today's regime snapshot")
@click.pass_context
def regime(ctx, refresh):
    """Show or refresh the separate market-regime context layer."""
    from .database import init_db, get_session, get_basket
    from .regime import latest_market_regime, refresh_market_regime

    init_db()
    session = get_session()
    try:
        if refresh:
            tickers = [c.ticker for c in get_basket(session)]
            result = refresh_market_regime(session, tickers)
            console.print(
                f"[green]✓[/green] {result['label']}  score={result['score']:+.3f}  "
                f"confidence={result['confidence']:.0%}"
            )
        row = latest_market_regime(session)
        if row is None:
            console.print("[yellow]No regime snapshot yet. Run `mktscan regime --refresh`.[/yellow]")
            return
        table = Table(title=f"Market Regime — {row.snapshot_date}", show_header=False)
        table.add_column("Metric", style="cyan")
        table.add_column("Value")
        table.add_row("Regime", row.regime_label or "—")
        table.add_row("Score", f"{row.regime_score:+.3f}" if row.regime_score is not None else "—")
        table.add_row("Confidence", f"{row.confidence:.0%}" if row.confidence is not None else "—")
        table.add_row("SPY trend", f"{row.spy_trend_score:+.2f}" if row.spy_trend_score is not None else "—")
        table.add_row("QQQ trend", f"{row.qqq_trend_score:+.2f}" if row.qqq_trend_score is not None else "—")
        table.add_row("VIX", f"{row.vix:.2f} ({row.volatility_state})" if row.vix is not None else "—")
        table.add_row("Basket breadth >50d", f"{row.breadth_above_50d:.0f}%" if row.breadth_above_50d is not None else "—")
        table.add_row("2Y / 10Y", f"{row.two_year_yield:.2f}% / {row.ten_year_yield:.2f}%" if row.two_year_yield is not None and row.ten_year_yield is not None else "—")
        table.add_row("Next macro", row.next_macro_event or "—")
        console.print(table)
    finally:
        session.close()


@cli.command()
@click.pass_context
def doctor(ctx):
    """
    Print deployment diagnostics: database, schema, data volume, IV history.

    Runs on every container boot so the Railway logs always answer the three
    questions that matter when something looks broken — is the database
    reachable, does the schema exist, and is there any data in it. Never raises;
    a diagnostic that crashes is worse than useless.
    """
    import os
    from sqlalchemy import inspect as sa_inspect, func, select

    console.print("\n[bold cyan]── MktScan doctor ──────────────────────────[/bold cyan]")

    # ── Environment ──────────────────────────────────────────────────────────
    role = os.environ.get("MKTSCAN_ROLE", "(unset)")
    db_url = os.environ.get("DATABASE_URL", "")
    console.print(f"  role:            {role}")
    if db_url:
        # Never print credentials.
        safe = db_url.split("@")[-1] if "@" in db_url else db_url
        console.print(f"  DATABASE_URL:    [green]set[/green] (→ {safe[:50]})")
    else:
        console.print("  DATABASE_URL:    [yellow]NOT SET — using local SQLite[/yellow]")
    console.print(f"  sentiment model: {os.environ.get('MKTSCAN_SENTIMENT_MODEL', '(from config)')}")

    # ── Database ─────────────────────────────────────────────────────────────
    try:
        from .database import get_engine
        engine = get_engine()
        console.print(f"  dialect:         {engine.dialect.name}")
        with engine.connect() as conn:
            conn.exec_driver_sql("SELECT 1")
        console.print("  connection:      [green]OK[/green]")
    except Exception as e:
        console.print(f"  connection:      [red]FAILED — {e}[/red]")
        console.print("[bold cyan]────────────────────────────────────────────[/bold cyan]\n")
        return

    # ── Schema ───────────────────────────────────────────────────────────────
    expected = [
        "companies", "articles", "sentiment_scores", "price_snapshots",
        "earnings_events", "scraper_runs", "tradeability_outcomes",
        "iv_snapshots", "backtest_observations",
        "macro_events", "market_regime_snapshots",
    ]
    try:
        present = set(sa_inspect(engine).get_table_names())
        missing = [t for t in expected if t not in present]
        if missing:
            console.print(f"  tables:          [red]missing {', '.join(missing)}[/red]")
            console.print("                   → run `alembic upgrade head`")
        else:
            console.print(f"  tables:          [green]all {len(expected)} present[/green]")
    except Exception as e:
        console.print(f"  tables:          [red]inspect failed — {e}[/red]")
        return

    # ── Row counts ───────────────────────────────────────────────────────────
    from .database import (
        get_session, Company, Article, PriceSnapshot, SentimentScore,
        IVSnapshot, EarningsEvent,
    )
    session = get_session()
    try:
        for label, model in (
            ("companies", Company), ("articles", Article),
            ("prices", PriceSnapshot), ("scores", SentimentScore),
            ("earnings", EarningsEvent), ("iv snapshots", IVSnapshot),
        ):
            try:
                pk = list(model.__table__.primary_key.columns)[0]
                n = session.execute(select(func.count(pk))).scalar() or 0
                colour = "green" if n else "yellow"
                console.print(f"  {label:15s}  [{colour}]{n:,}[/{colour}]")
            except Exception as e:
                session.rollback()
                console.print(f"  {label:15s}  [red]error — {e}[/red]")

        # ── IV rank readiness ────────────────────────────────────────────────
        try:
            from .iv_rank import compute_iv_rank
            from .database import get_basket

            tickers = [c.ticker for c in get_basket(session)]
            if tickers:
                bases = {}
                for tk in tickers[:5]:
                    bases[tk] = compute_iv_rank(session, tk)["basis"]
                if all(b == "none" for b in bases.values()):
                    console.print(
                        "  IV rank:         [yellow]unavailable — "
                        "run `mktscan iv --backfill`[/yellow]"
                    )
                    console.print(
                        "                   strategy selection will use its "
                        "fallback branch until seeded"
                    )
                else:
                    summary = ", ".join(f"{k}={v}" for k, v in bases.items())
                    console.print(f"  IV rank:         [green]{summary}[/green]")
        except Exception as e:
            session.rollback()
            console.print(f"  IV rank:         [red]check failed — {e}[/red]")
    finally:
        session.close()

    console.print("[bold cyan]────────────────────────────────────────────[/bold cyan]\n")


@cli.command()
@click.option("--backfill", is_flag=True, help="Seed IV history from realised vol (run once)")
@click.option("--update",   is_flag=True, help="Record today's ATM IV from the option chain")
@click.option("--check",    is_flag=True, help="Inspect and repair the database schema")
@click.option("--ticker",   default=None, help="Show IV rank for one ticker")
@click.pass_context
def iv(ctx, backfill, update, check, ticker):
    """
    Manage implied-volatility history — the input to IV rank.

    IV rank drives strategy selection, so without history the tool falls back to
    its least-informed branch. Typical first-time setup:

        python -m mktscan iv --check
        python -m mktscan iv --backfill
        python -m mktscan iv --update
    """
    from .database import init_db, get_session, get_basket
    from .iv_rank import (
        backfill_iv_history, check_and_migrate, compute_iv_rank, update_iv_snapshot,
    )

    init_db()

    if check:
        for key, value in check_and_migrate().items():
            console.print(f"  [dim]{key}:[/dim] {value}")
        return

    session = get_session()
    try:
        tickers = [c.ticker for c in get_basket(session)]

        if backfill:
            console.print(f"[cyan]Seeding IV history for {len(tickers)} tickers...[/cyan]")
            result = backfill_iv_history(session, tickers, days=365)
            console.print(f"[green]✓ {result['rows']} rows across {result['tickers']} tickers[/green]")
            if result["failed"]:
                console.print(f"[yellow]  failed: {', '.join(result['failed'])}[/yellow]")

        if update:
            console.print("[cyan]Fetching today's ATM IV from option chains...[/cyan]")
            count = update_iv_snapshot(session, tickers)
            console.print(f"[green]✓ updated {count}/{len(tickers)}[/green]")

        if ticker or (not backfill and not update):
            table = Table(title="IV Rank", show_header=True, header_style="bold cyan")
            for col in ("Ticker", "IV", "Rank", "Pctile", "Basis", "Days", "Conf"):
                table.add_column(col)
            for tk in ([ticker.upper()] if ticker else tickers):
                r = compute_iv_rank(session, tk)
                if r["iv_rank"] is None:
                    table.add_row(tk, "—", "—", "—", r["basis"], str(r["data_days"]), "0.0")
                    continue
                rank_text = Text(f"{r['iv_rank']:.0f}")
                rank_text.stylize("red" if r["iv_rank"] >= 70 else
                                  "green" if r["iv_rank"] <= 30 else "yellow")
                table.add_row(
                    tk, f"{r['iv_current']*100:.1f}%", rank_text, f"{r['iv_pct']:.0f}",
                    r["basis"], str(r["data_days"]), f"{r['confidence']:.2f}",
                )
            console.print(table)
            console.print(
                "[dim]basis=proxy means the rank is built from realised volatility, "
                "not option IV — treated as unavailable for strategy selection.[/dim]"
            )
    finally:
        session.close()


@cli.command()
@click.option("--ticker", default=None, help="Limit to one ticker")
@click.pass_context
def setups(ctx, ticker):
    """Show priced options trade setups for the basket."""
    from .database import init_db, get_session
    from .options import generate_basket_setups, generate_trade_setup
    from .tradeability import compute_basket_tradeability

    init_db()
    session = get_session()
    try:
        console.print("[cyan]Scoring basket...[/cyan]")
        scores = compute_basket_tradeability(session)
        if ticker:
            tk = ticker.upper()
            if tk not in scores:
                console.print(f"[red]{tk} is not in the basket.[/red]")
                return
            setups_map = {tk: generate_trade_setup(tk, scores[tk])}
        else:
            console.print("[cyan]Pricing option chains...[/cyan]")
            setups_map = generate_basket_setups(scores)
    finally:
        session.close()

    table = Table(title="Options Trade Setups", show_header=True, header_style="bold cyan")
    for col in ("Ticker", "Score", "Strategy", "Expiry", "Net", "Max Loss", "Max Gain",
                "Breakeven", "PoP", "R/R", "Size", "Conf"):
        table.add_column(col)

    for tk, s in sorted(setups_map.items(), key=lambda kv: -abs(kv[1].get("tradeability", 0))):
        if not s.get("tradeable"):
            table.add_row(tk, f"{s.get('tradeability', 0):+.2f}",
                          s.get("strategy", "—"), "—", "—", "—", "—", "—", "—", "—",
                          "—", s.get("reason", "—"))
            continue
        score_text = Text(f"{s['tradeability']:+.2f}")
        score_text.stylize("green" if s["tradeability"] > 0 else "red")
        table.add_row(
            tk, score_text, s["strategy"], f"{s['expiry']} ({s['dte']}d)",
            f"${s['net_debit']:+.2f}",
            f"${s['max_loss_per_contract']:.0f}" if s.get("max_loss_per_contract") else "—",
            f"${s['max_profit_per_contract']:.0f}" if s.get("max_profit_per_contract") else "—",
            f"${s['breakeven']:.2f}" if s.get("breakeven") else "—",
            f"{s['probability_of_profit']:.0f}%" if s.get("probability_of_profit") else "—",
            f"{s['rr_ratio']:.2f}", s["sizing"], s["confidence_tier"],
        )

    console.print(table)
    for tk, s in setups_map.items():
        for warning in s.get("warnings", []):
            console.print(f"[yellow]  ⚠ {tk}: {warning}[/yellow]")
    console.print(f"\n[dim]{__import__('mktscan.options', fromlist=['DISCLAIMER']).DISCLAIMER}[/dim]")


@cli.command()
@click.pass_context
def dashboard(ctx):
    """Launch the Streamlit dashboard."""
    import subprocess, sys, os
    dash = os.path.join(os.path.dirname(__file__), "..", "dashboard", "app.py")
    console.print("[cyan]Launching Streamlit dashboard...[/cyan]")
    subprocess.run([sys.executable, "-m", "streamlit", "run", dash])


@cli.command()
@click.argument("ticker")
@click.argument("name")
@click.option("--sector", default="", help="Sector")
@click.option("--keywords", "-k", default="", help="Comma-separated search keywords")
@click.pass_context
def add(ctx, ticker, name, sector, keywords):
    """Add a company to the basket."""
    from .database import init_db, get_session, upsert_company

    init_db()
    session = get_session()
    kw = keywords or ticker
    upsert_company(session, ticker.upper(), name, sector, kw)
    session.close()
    console.print(f"[green]✓ Added {ticker.upper()} — {name}[/green]")


@cli.command()
@click.pass_context
def basket(ctx):
    """List companies in the watch basket."""
    from .database import init_db, get_session, get_basket

    init_db()
    session = get_session()
    companies = get_basket(session)
    session.close()

    if not companies:
        console.print("[yellow]Basket is empty. Use `mktscan add` to add companies.[/yellow]")
        return

    table = Table(title="Watch Basket", header_style="bold cyan")
    table.add_column("Ticker", style="bold green")
    table.add_column("Name")
    table.add_column("Sector")
    table.add_column("Keywords")

    for c in companies:
        table.add_row(c.ticker, c.name, c.sector or "—", (c.keywords or "")[:50])

    console.print(table)


def _print_sentiment_table(sentiment: dict):
    table = Table(title="Sentiment Results", header_style="bold cyan")
    table.add_column("Ticker", style="bold")
    table.add_column("Score")
    table.add_column("Signal")
    table.add_column("Articles")
    table.add_column("Sources")

    for ticker, result in sorted(sentiment.items(), key=lambda x: -x[1]["score"]):
        s = result["score"]
        score_text = Text(f"{s:+.3f}")
        score_text.stylize("green" if s > 0.1 else ("red" if s < -0.1 else "yellow"))
        sources = ", ".join(f"{k}:{v}" for k, v in result.get("source_breakdown", {}).items())
        table.add_row(ticker, score_text, result["label"], str(result["article_count"]), sources)

    console.print(table)


def main():
    cli(obj={})


if __name__ == "__main__":
    main()
