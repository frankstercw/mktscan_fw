"""
mktscan/alerts.py
Fires email and/or Slack alerts when sentiment crosses configured thresholds.
"""
from __future__ import annotations
import logging
import smtplib
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Any

import requests

log = logging.getLogger(__name__)

# Cache to avoid duplicate alerts within same run
_fired_this_session: set[str] = set()


def check_and_fire(
    cfg: dict[str, Any],
    ticker: str,
    company_name: str,
    score: float,
    label: str,
) -> None:
    """
    Check if score crosses a threshold and fire configured alerts.
    """
    if not cfg.get("enabled"):
        return

    bull_thresh = float(cfg.get("sentiment_threshold_bull", 0.6))
    bear_thresh = float(cfg.get("sentiment_threshold_bear", -0.3))

    fire = False
    direction = ""
    if score >= bull_thresh:
        direction = "BULLISH"
        fire = True
    elif score <= bear_thresh:
        direction = "BEARISH"
        fire = True

    if not fire:
        return

    key = f"{ticker}:{direction}"
    if key in _fired_this_session:
        return  # Don't re-alert for same ticker/direction in same session

    _fired_this_session.add(key)

    subject = f"MktScan Alert: {ticker} is {direction} ({score:+.2f})"
    body = (
        f"Ticker: {ticker} — {company_name}\n"
        f"Sentiment Score: {score:+.4f}\n"
        f"Signal: {label}\n"
        f"Threshold: {'bull' if direction == 'BULLISH' else 'bear'} = {bull_thresh if direction == 'BULLISH' else bear_thresh}\n"
        f"Timestamp: {datetime.utcnow().isoformat()}Z\n"
    )

    # Email
    email_cfg = cfg.get("email", {})
    if email_cfg.get("smtp_host"):
        _send_email(email_cfg, subject, body)

    # Slack
    slack_url = cfg.get("slack_webhook", "")
    if slack_url and not slack_url.startswith("YOUR_"):
        _send_slack(slack_url, ticker, company_name, score, label, direction)


def _send_email(cfg: dict, subject: str, body: str) -> None:
    try:
        msg = MIMEMultipart()
        msg["From"]    = cfg["from_addr"]
        msg["To"]      = cfg["to_addr"]
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))

        with smtplib.SMTP(cfg["smtp_host"], int(cfg.get("smtp_port", 587))) as smtp:
            smtp.starttls()
            smtp.login(cfg["from_addr"], cfg["password"])
            smtp.sendmail(cfg["from_addr"], cfg["to_addr"], msg.as_string())

        log.info(f"[Alerts] Email sent: {subject}")
    except Exception as e:
        log.error(f"[Alerts] Email failed: {e}")


def _send_slack(
    webhook_url: str,
    ticker: str,
    name: str,
    score: float,
    label: str,
    direction: str,
) -> None:
    emoji   = "🟢" if direction == "BULLISH" else "🔴"
    color   = "#22d3a0" if direction == "BULLISH" else "#f87171"
    payload = {
        "attachments": [{
            "color": color,
            "title": f"{emoji} {ticker} — {label}",
            "text":  f"*{name}*\nSentiment Score: `{score:+.4f}`",
            "footer":"MktScan",
            "ts":    int(datetime.utcnow().timestamp()),
        }]
    }
    try:
        r = requests.post(webhook_url, json=payload, timeout=10)
        r.raise_for_status()
        log.info(f"[Alerts] Slack fired for {ticker}")
    except Exception as e:
        log.error(f"[Alerts] Slack failed: {e}")


def reset_session_cache() -> None:
    """Call at the start of each new scheduled run."""
    _fired_this_session.clear()
