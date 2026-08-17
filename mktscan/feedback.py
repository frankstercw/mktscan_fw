"""
mktscan/feedback.py
──────────────────────────────────────────────────────────────────────────────
Prediction tracking and score calibration.

What was broken
───────────────
1. **Pseudo-replication.** The scheduler runs every 15 minutes, and every run
   recorded a prediction per ticker. That is ~96 predictions per ticker per day,
   *all of which resolve against the same next-day return*. ``n_observations``
   grew 96× faster than independent information arrived, and since ``confidence``
   scaled to 1.0 at 30 observations, it saturated within hours — on a sample of
   effectively one day. The model was then "calibrated" against that.

2. **Resolution against the past.** ``resolve_pending_outcomes`` fell back to
   ``dated[-1][1]`` — the most recent return in the window — when no strictly
   later trading day existed. That could resolve a prediction against a return
   from *before* it was made, which is guaranteed spurious accuracy.

3. **Horizon mismatch.** A 1-day horizon on a signal built from 7 days of news
   and 14 days of momentum, used to pick 30-45 DTE options. The thing being
   measured was not the thing being traded.

4. **An adjustment too small to matter, and applied in the wrong place.** The
   multiplier was ±15% × confidence, clamped — on a score of +0.35 that is ±0.05,
   which almost never crosses a label boundary. It added a feedback path and a
   failure mode in exchange for an effect below the noise floor, and it silently
   altered the number shown to the user.

What it does now
────────────────
One prediction per ticker per calendar day, enforced by a unique constraint.
Five-trading-day horizon, matching the holding period the strategies actually
target. Resolution only ever against returns strictly after the prediction date.
30 independent observations required before any adjustment. And the adjustment
is **off by default** (``FEEDBACK_ADJUSTMENT_ENABLED``) — the statistics are
recorded and displayed, but the score you see is the score the model produced.
Turn it on only once the accuracy panel shows a stable, meaningful edge.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from .clock import as_date, as_market_date, market_date

log = logging.getLogger(__name__)

# Lookback window for accuracy statistics.
ACCURACY_WINDOW_DAYS = 180

# Minimum *independent* observations before statistics are reported at all.
MIN_OBSERVATIONS = 10

# Minimum before any adjustment is applied. Direction accuracy on 10 samples has
# a standard error of ~16 percentage points — indistinguishable from a coin flip.
MIN_OBSERVATIONS_FOR_ADJUSTMENT = 30

# Holding horizon in trading days. Matches the 21-60 DTE options the strategy
# layer selects, evaluated near the halfway point.
DEFAULT_HORIZON_DAYS = 5

# Weight of the feedback adjustment when it is enabled.
FEEDBACK_WEIGHT = 0.15

# Off by default. The feedback signal is not currently strong enough to justify
# silently altering a displayed score; record and show, do not act.
FEEDBACK_ADJUSTMENT_ENABLED = False

# Rough mapping from score to expected move, used only for the magnitude-error
# diagnostic. A score of 1.0 implies a 3% move over the horizon.
IMPLIED_MOVE_PER_POINT = 3.0


# ═══════════════════════════════════════════════════════════════════════════════
# RECORD
# ═══════════════════════════════════════════════════════════════════════════════

def record_prediction(
    session,
    ticker:       str,
    score:        float,
    label:        str,
    run_id:       int,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
    regime_score: float | None = None,
    regime_label: str | None = None,
    regime_confidence: float | None = None,
) -> bool:
    """
    Record today's score as a pending outcome — at most once per ticker per day.

    Returns True if a new record was created, False if today's was already
    present (in which case it is updated in place with the latest score, so the
    stored prediction reflects the most recent information of the day rather
    than whichever run happened to fire first).
    """
    from sqlalchemy import select
    from .database import TradeabilityOutcome

    today = market_date()

    existing = session.execute(
        select(TradeabilityOutcome).where(
            TradeabilityOutcome.ticker == ticker,
            TradeabilityOutcome.prediction_date == today,
        )
    ).scalar_one_or_none()

    if existing is not None:
        # Only refresh while still unresolved — never rewrite history that has
        # already been scored against an actual outcome.
        if existing.actual_return_pct is None:
            existing.score_at_prediction = round(score, 6)
            existing.label_at_prediction = label
            existing.predicted_at        = datetime.utcnow()
            existing.run_id              = run_id
            existing.regime_score_at_prediction      = regime_score
            existing.regime_label_at_prediction      = regime_label
            existing.regime_confidence_at_prediction = regime_confidence
        return False

    session.add(TradeabilityOutcome(
        ticker              = ticker,
        score_at_prediction = round(score, 6),
        label_at_prediction = label,
        predicted_at        = datetime.utcnow(),
        prediction_date     = today,
        horizon_days        = horizon_days,
        run_id              = run_id,
        regime_score_at_prediction      = regime_score,
        regime_label_at_prediction      = regime_label,
        regime_confidence_at_prediction = regime_confidence,
    ))
    return True


# ═══════════════════════════════════════════════════════════════════════════════
# RESOLVE
# ═══════════════════════════════════════════════════════════════════════════════

def resolve_pending_outcomes(
    session,
    price_history: dict[str, list[tuple[str, float]]],
) -> int:
    """
    Fill in actual forward returns for predictions whose horizon has elapsed.

    ``price_history`` maps ticker → [(date_str, close_price), ...] in ascending
    date order.

    The forward return is compounded across the ``horizon_days`` trading days
    *strictly after* the prediction date. If that many sessions have not yet
    elapsed the record is left pending — there is no fallback to "the most recent
    return", which previously let a prediction be resolved against a move that
    happened before it was made.
    """
    from sqlalchemy import select
    from .database import TradeabilityOutcome

    pending = session.execute(
        select(TradeabilityOutcome).where(TradeabilityOutcome.actual_return_pct.is_(None))
    ).scalars().all()

    resolved = 0

    for record in pending:
        series = price_history.get(record.ticker) or []
        if len(series) < 2:
            continue

        prediction_date = record.prediction_date or as_market_date(record.predicted_at)
        if prediction_date is None:
            continue

        horizon = record.horizon_days or DEFAULT_HORIZON_DAYS

        # Sessions strictly after the prediction date, in order.
        forward = []
        base_close = None
        for date_str, close in series:
            row_date = as_date(date_str)
            if row_date is None:
                continue
            if row_date <= prediction_date:
                base_close = close          # last close at or before prediction
            else:
                forward.append((row_date, close))

        if base_close is None or base_close <= 0:
            continue
        if len(forward) < horizon:
            continue                        # horizon has not elapsed yet

        outcome_date, outcome_close = forward[horizon - 1]
        actual_return = (outcome_close / base_close - 1.0) * 100.0

        record.actual_return_pct = round(actual_return, 4)
        record.outcome_date      = datetime.combine(outcome_date, datetime.min.time())
        record.direction_correct = (
            (record.score_at_prediction > 0 and actual_return > 0) or
            (record.score_at_prediction < 0 and actual_return < 0)
        )
        implied_move = record.score_at_prediction * IMPLIED_MOVE_PER_POINT
        record.magnitude_error = round(abs(implied_move - actual_return), 4)
        resolved += 1

    if resolved:
        session.commit()
        log.info(f"[Feedback] Resolved {resolved} outcome records")

    return resolved


# ═══════════════════════════════════════════════════════════════════════════════
# ACCURACY
# ═══════════════════════════════════════════════════════════════════════════════

def get_accuracy_stats(session, ticker: str, days: int = ACCURACY_WINDOW_DAYS) -> dict[str, Any]:
    """
    Direction accuracy and calibration statistics for one ticker.

    Every record here is one independent trading day, so ``n_observations`` is a
    real sample size rather than a count of how often the scheduler happened to
    fire. A binomial standard error is reported alongside the point estimate,
    because a 62% hit rate on 21 observations is not distinguishable from chance
    and should not be presented as if it were.
    """
    from sqlalchemy import select
    from .database import TradeabilityOutcome

    cutoff = market_date() - timedelta(days=days)
    rows = session.execute(
        select(TradeabilityOutcome)
        .where(
            TradeabilityOutcome.ticker == ticker,
            TradeabilityOutcome.actual_return_pct.isnot(None),
            TradeabilityOutcome.prediction_date >= cutoff,
        )
        .order_by(TradeabilityOutcome.prediction_date)
    ).scalars().all()

    if not rows:
        return _empty_stats(ticker)

    n            = len(rows)
    correct      = sum(1 for r in rows if r.direction_correct)
    dir_accuracy = correct / n

    # Binomial standard error, and whether the result clears ~2 SE from 0.5.
    std_err      = (0.25 / n) ** 0.5
    significant  = abs(dir_accuracy - 0.5) > 2 * std_err

    bull = [r for r in rows if r.score_at_prediction > 0.05]
    bear = [r for r in rows if r.score_at_prediction < -0.05]

    avg_bull = sum(r.actual_return_pct for r in bull) / len(bull) if bull else None
    avg_bear = sum(r.actual_return_pct for r in bear) / len(bear) if bear else None

    mag_errors  = [r.magnitude_error for r in rows if r.magnitude_error is not None]
    avg_mag_err = sum(mag_errors) / len(mag_errors) if mag_errors else None

    implied     = [r.score_at_prediction * IMPLIED_MOVE_PER_POINT for r in rows]
    actual      = [r.actual_return_pct for r in rows]
    calib_bias  = round(sum(a - i for a, i in zip(actual, implied)) / n, 4)

    # Spread between what bullish and bearish calls actually delivered. This is
    # the number that matters: a model can be 60% "directionally accurate" in a
    # rising market purely by being long-biased. A positive spread means the
    # score genuinely separates winners from losers.
    edge = (avg_bull - avg_bear) if (avg_bull is not None and avg_bear is not None) else None

    feedback_multiplier = round((dir_accuracy - 0.5) * 2.0, 4)
    confidence = round(min(1.0, max(0.0, (n - MIN_OBSERVATIONS) / 40.0)), 3)

    return {
        "ticker":              ticker,
        "n_observations":      n,
        "horizon_days":        rows[0].horizon_days or DEFAULT_HORIZON_DAYS,
        "direction_accuracy":  round(dir_accuracy, 4),
        "pct_correct":         round(dir_accuracy * 100, 1),
        "std_error_pct":       round(std_err * 100, 1),
        "statistically_significant": bool(significant),
        "avg_return_on_bull":  round(avg_bull, 3) if avg_bull is not None else None,
        "avg_return_on_bear":  round(avg_bear, 3) if avg_bear is not None else None,
        "directional_edge_pct": round(edge, 3) if edge is not None else None,
        "avg_magnitude_error": round(avg_mag_err, 3) if avg_mag_err is not None else None,
        "calibration_bias":    calib_bias,
        "feedback_multiplier": feedback_multiplier,
        "confidence":          confidence,
        "adjustment_enabled":  FEEDBACK_ADJUSTMENT_ENABLED,
        "history": [
            {
                "date":    (r.prediction_date or as_market_date(r.predicted_at)).isoformat(),
                "score":   r.score_at_prediction,
                "actual":  r.actual_return_pct,
                "correct": r.direction_correct,
            }
            for r in rows[-60:]
        ],
    }


def _empty_stats(ticker: str = "") -> dict[str, Any]:
    return {
        "ticker": ticker, "n_observations": 0,
        "horizon_days": DEFAULT_HORIZON_DAYS,
        "direction_accuracy": None, "pct_correct": None,
        "std_error_pct": None, "statistically_significant": False,
        "avg_return_on_bull": None, "avg_return_on_bear": None,
        "directional_edge_pct": None, "avg_magnitude_error": None,
        "calibration_bias": None, "feedback_multiplier": 0.0,
        "confidence": 0.0, "adjustment_enabled": FEEDBACK_ADJUSTMENT_ENABLED,
        "history": [],
    }


# ═══════════════════════════════════════════════════════════════════════════════
# ADJUSTMENT
# ═══════════════════════════════════════════════════════════════════════════════

def apply_feedback_adjustment(
    raw_score: float,
    stats:     dict[str, Any],
    weight:    float = FEEDBACK_WEIGHT,
) -> tuple[float, str]:
    """
    Optionally scale the raw score by measured direction accuracy.

    Disabled by default. When a model silently rewrites its own output based on
    30 observations of a noisy statistic, the displayed number stops being
    explainable — and the effect size (±15% × confidence) is smaller than the
    uncertainty in the statistic driving it.

    Returns ``(score, explanation)``. When disabled the score is returned
    unchanged and the explanation reports what *would* have happened, so the
    dashboard can still show whether the signal is tracking.
    """
    n          = stats.get("n_observations", 0)
    confidence = stats.get("confidence", 0.0)
    multiplier = stats.get("feedback_multiplier", 0.0)
    accuracy   = stats.get("direction_accuracy")
    significant = stats.get("statistically_significant", False)

    if n < MIN_OBSERVATIONS_FOR_ADJUSTMENT:
        return round(raw_score, 4), (
            f"No adjustment — {n}/{MIN_OBSERVATIONS_FOR_ADJUSTMENT} independent "
            f"observations. Tracking only."
        )

    blend_factor = max(0.5, min(1.5, 1.0 + weight * confidence * multiplier))
    would_be     = round(max(-1.0, min(1.0, raw_score * blend_factor)), 4)

    if not FEEDBACK_ADJUSTMENT_ENABLED:
        return round(raw_score, 4), (
            f"Tracking: {accuracy*100:.0f}% direction accuracy over {n} obs"
            f"{'' if significant else ' (not statistically significant)'}. "
            f"Adjustment disabled — score shown is unmodified "
            f"(would be {would_be:+.4f} if enabled)."
        )

    if not significant:
        return round(raw_score, 4), (
            f"No adjustment — {accuracy*100:.0f}% accuracy over {n} obs is within "
            f"2 standard errors of chance."
        )

    direction = "boosted" if blend_factor > 1.0 else "dampened"
    return would_be, (
        f"Feedback: {accuracy*100:.0f}% accuracy over {n} obs → "
        f"factor {blend_factor:.3f} ({direction}) → "
        f"raw {raw_score:+.4f} → adjusted {would_be:+.4f}"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# PRICE HISTORY
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_close_history(ticker: str, days: int = 30) -> list[tuple[str, float]]:
    """
    Recent (date, close) pairs, ascending.

    Closes rather than daily returns: the horizon return is now compounded from
    the prediction-date close to the close ``horizon_days`` sessions later, which
    cannot be reconstructed from a list of single-day percentages without
    reintroducing rounding drift.
    """
    try:
        import yfinance as yf

        end   = market_date()
        start = end - timedelta(days=days + 20)
        raw   = yf.download(ticker, start=str(start), end=str(end + timedelta(days=1)),
                            progress=False, auto_adjust=True)
        if raw is None or raw.empty:
            return []

        closes = raw["Close"]
        if hasattr(closes, "squeeze"):
            closes = closes.squeeze()
        closes = closes.dropna()

        return [(str(idx.date()), round(float(val), 6)) for idx, val in closes.items()]
    except Exception as e:
        log.debug(f"[Feedback] fetch_close_history({ticker}) failed: {e}")
        return []


def get_basket_accuracy_stats(session, tickers: list[str]) -> dict[str, dict]:
    """Accuracy statistics for every ticker."""
    return {t: get_accuracy_stats(session, t) for t in tickers}


def get_aggregate_stats(session, tickers: list[str]) -> dict[str, Any]:
    """
    Pool observations across the basket.

    Per-ticker samples are small; the pooled figure is the only one likely to
    reach a usable sample size in the first year of operation. Reported
    separately so it is clear which is which.
    """
    from sqlalchemy import select
    from .database import TradeabilityOutcome

    cutoff = market_date() - timedelta(days=ACCURACY_WINDOW_DAYS)
    rows = session.execute(
        select(TradeabilityOutcome).where(
            TradeabilityOutcome.actual_return_pct.isnot(None),
            TradeabilityOutcome.prediction_date >= cutoff,
        )
    ).scalars().all()

    if not rows:
        return {"n_observations": 0, "direction_accuracy": None,
                "directional_edge_pct": None, "statistically_significant": False}

    n        = len(rows)
    correct  = sum(1 for r in rows if r.direction_correct)
    accuracy = correct / n
    std_err  = (0.25 / n) ** 0.5

    bull = [r.actual_return_pct for r in rows if r.score_at_prediction > 0.05]
    bear = [r.actual_return_pct for r in rows if r.score_at_prediction < -0.05]
    edge = ((sum(bull) / len(bull)) - (sum(bear) / len(bear))) if bull and bear else None

    return {
        "n_observations": n,
        "n_tickers": len({r.ticker for r in rows}),
        "direction_accuracy": round(accuracy, 4),
        "pct_correct": round(accuracy * 100, 1),
        "std_error_pct": round(std_err * 100, 1),
        "statistically_significant": bool(abs(accuracy - 0.5) > 2 * std_err),
        "avg_return_on_bull": round(sum(bull) / len(bull), 3) if bull else None,
        "avg_return_on_bear": round(sum(bear) / len(bear), 3) if bear else None,
        "directional_edge_pct": round(edge, 3) if edge is not None else None,
    }
