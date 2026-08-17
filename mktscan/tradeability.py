"""
mktscan/tradeability.py
─────────────────────────────────────────────────────────────────────────────
Composite tradeability score.

Nine signal categories, each producing a sub-score in [-1, +1], combined into a
confidence-weighted average.

  1. Sentiment       — news sentiment, recency-decayed, with momentum + diversity
  2. Technical       — 52w range position, breakout proximity, analyst consensus
  3. Price momentum  — RSI, trend, volatility regime, streaks, acceleration
  4. Fundamental     — P/E band, earnings surprise history, beat streak
  5. Event-driven    — earnings proximity and last result
  6. Volume          — volume anomaly relative to the 30-day average
  7. Short interest  — squeeze vs distribution
  8. Options IV      — IV rank regime (drives strategy selection)
  9. Analyst         — consensus score and price-target upside

What changed and why
────────────────────
* **Cross-sectional ranking.** Sub-signals that were near-constant across the
  basket (52w position, breakout proximity, analyst ratings, P/E) are now scored
  by percentile within the basket and blended with their absolute mapping. See
  cross_section.py for the full rationale.

* **Zero-confidence categories no longer vote.** The old weighting was
  ``cat_weight * (0.3 + 0.7 * confidence)``, so a category with *no data*
  returned score 0.0 and still kept 30% of its weight — anchoring the composite
  toward zero. With options_iv permanently dead, plus frequently-missing short
  interest and fundamentals, roughly 28% of total weight was a constant zero
  pulling every score toward NEUTRAL. Categories below a confidence floor are
  now dropped from the denominator entirely.

* **RSI is computed properly.** It was seeded from a single observation with
  only 14 data points and ``alpha = 1/len(returns)``. Wilder's RSI needs ~100+
  bars to converge; the readings did not match any charting package, contrary to
  the comment claiming they did. It now seeds from a 14-period SMA over 120 bars.

* **The RSI mapping is monotonic.** It used to score RSI < 25 at +0.8
  (mean reversion) *and* RSI 60-70 at +0.4 (momentum), making both tails bullish
  with only 40-60 neutral. That is two incompatible theses running through one
  variable, and the composite could not tell them apart. Momentum is now
  monotonic, with overbought/oversold exposed separately as a mean-reversion
  flag that the strategy layer can use.

* **The earnings contradiction is resolved.** ``calc_event_driven_signal`` scored
  earnings-within-7-days as **+0.5** (bullish) while ``calc_earnings_proximity_signal``
  scored the identical condition as **-0.5** (risk) — and the latter was never
  called. For a long option, imminent earnings is a risk, not a bullish signal:
  IV inflates into the print and collapses after it. There is now one earnings
  treatment, and it is negative.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta
from typing import Any

from .clock import as_date, market_date, utc_now
from .cross_section import blend, build_cross_sectional_scores

log = logging.getLogger(__name__)

# ── Weights ───────────────────────────────────────────────────────────────────
# Deliberately close to uniform. These are not fitted — there is no procedure in
# this codebase that could fit 9 weights on 19 tickers without overfitting — so
# they encode a prior about relative reliability, nothing more. Differential
# weighting should only be introduced once the backtest shows out-of-sample
# evidence that it helps.
DEFAULT_WEIGHTS: dict[str, float] = {
    "sentiment":           0.12,   # noisy over options timeframes
    "technical":           0.13,
    "price_momentum":      0.18,   # the most reliable short-horizon signal
    "fundamental":         0.08,   # slow-moving; weak over 2-6 weeks
    "event_driven":        0.14,   # earnings dominate option P&L
    "volume":              0.08,
    "short_interest":      0.06,
    "options_iv":          0.14,   # regime selector for the strategy layer
    "analyst":             0.07,
}

CATEGORY_KEYS = list(DEFAULT_WEIGHTS.keys())

# A category below this confidence contributes nothing. Previously such a
# category still carried 30% of its weight at a score of 0.0, which is an
# assertion of neutrality that the absence of data does not support.
MIN_CATEGORY_CONFIDENCE = 0.15


# ── Labels ────────────────────────────────────────────────────────────────────

def tradeability_label(score: float) -> str:
    if score >  0.50: return "STRONG BUY"
    if score >  0.20: return "BULLISH"
    if score > -0.20: return "NEUTRAL"
    if score > -0.50: return "BEARISH"
    return "STRONG SELL"


def tradeability_color(score: float) -> str:
    if score >  0.50: return "#22d3a0"
    if score >  0.20: return "#86efac"
    if score > -0.20: return "#fbbf24"
    if score > -0.50: return "#fca5a5"
    return "#f87171"


def _clamp(value: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def fmt_component(value: Any) -> str:
    """
    Format a component value for a human-readable detail string.

    Component dicts hold mixed types on purpose: signed float sub-scores, plus
    context like `iv_basis` (str), `mean_reversion_flag` (bool) and
    `earnings_days_away` (int), any of which may be None when data is missing.
    Applying a float format code to all of them raises ValueError, so every
    detail string must go through this.
    """
    if value is None:
        return "n/a"
    if isinstance(value, bool):          # before int — bool subclasses int
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:+.2f}"
    return str(value)


def _empty(detail: str) -> dict[str, Any]:
    return {"score": 0.0, "confidence": 0.0, "detail": detail, "components": {}}


# ═══════════════════════════════════════════════════════════════════════════════
# FEATURE EXTRACTION — raw values for cross-sectional ranking
# ═══════════════════════════════════════════════════════════════════════════════

def extract_features(
    price_data:      dict | None,
    daily_returns:   list[float] | None,
    sentiment_score: float | None,
    articles:        list[dict] | None = None,
) -> dict[str, float | None]:
    """
    Pull the raw values that cross_section.py ranks across the basket.

    Raw, untransformed values only — the whole point is to rank them against
    each other rather than against a hard-coded threshold.
    """
    pd_ = price_data or {}
    features: dict[str, float | None] = {}

    price = pd_.get("price")
    hi_52 = pd_.get("week_52_high")
    lo_52 = pd_.get("week_52_low")

    features["52w_position"] = (
        (price - lo_52) / (hi_52 - lo_52)
        if price and hi_52 and lo_52 and hi_52 > lo_52 else None
    )
    features["breakout_proximity"] = (
        (price - hi_52) / hi_52 if price and hi_52 and hi_52 > 0 else None
    )

    rating_map = {
        "strongbuy": 1.0, "buy": 0.6, "outperform": 0.5, "overweight": 0.5,
        "hold": 0.0, "neutral": 0.0, "marketperform": 0.0,
        "underperform": -0.5, "underweight": -0.5,
        "sell": -0.8, "strongsell": -1.0,
    }
    rating = (pd_.get("analyst_rating") or "").lower().replace(" ", "").replace("_", "")
    features["analyst_rating_value"] = rating_map.get(rating)

    mean_score = pd_.get("analyst_mean_score")
    # yfinance uses 1 = strong buy .. 5 = sell, so invert for "higher is bullish".
    features["analyst_mean_inverted"] = (3.0 - mean_score) if mean_score else None

    target, current = pd_.get("target_price"), pd_.get("price")
    features["target_upside_pct"] = (
        (target - current) / current * 100 if target and current and current > 0 else None
    )

    pe = pd_.get("pe_ratio")
    features["pe_ratio"] = pe if pe and pe > 0 else None

    vol_ratio  = pd_.get("volume_ratio")
    change_pct = pd_.get("change_pct")
    features["volume_ratio_signed"] = (
        vol_ratio * (1 if (change_pct or 0) >= 0 else -1) if vol_ratio else None
    )

    short_ratio = pd_.get("short_ratio")
    features["short_squeeze_pressure"] = (
        short_ratio * (1 if (change_pct or 0) > 0 else -1) if short_ratio else None
    )

    returns = daily_returns or []
    if len(returns) >= 5:
        window = returns[-14:]
        n      = len(window)
        xs     = list(range(n))
        x_bar  = sum(xs) / n
        y_bar  = sum(window) / n
        num    = sum((x - x_bar) * (y - y_bar) for x, y in zip(xs, window))
        den    = sum((x - x_bar) ** 2 for x in xs)
        features["trend_slope"] = num / den if den else 0.0
        features["accel"] = (sum(window[-5:]) / 5) - y_bar
    else:
        features["trend_slope"] = None
        features["accel"] = None

    features["raw_sentiment"] = sentiment_score

    fresh = _fresh_articles(articles or [])
    features["recency_sentiment"] = _recency_weighted_sentiment(fresh)

    return features


def _fresh_articles(articles: list[dict]) -> list[dict]:
    """Drop articles older than 7 days or without a usable timestamp."""
    cutoff = utc_now().replace(tzinfo=None) - timedelta(days=7)
    fresh: list[dict] = []
    for a in articles:
        pub = a.get("published_at")
        if pub is None:
            continue
        if isinstance(pub, str):
            try:
                pub = datetime.fromisoformat(pub)
            except ValueError:
                continue
        if pub.tzinfo is not None:
            pub = pub.replace(tzinfo=None)
        if pub >= cutoff:
            fresh.append(a)
    return fresh


def _recency_weighted_sentiment(articles: list[dict]) -> float | None:
    """Exponentially decayed mean sentiment, 48-hour half-life."""
    now = utc_now().replace(tzinfo=None)
    ws = wt = 0.0
    for a in articles:
        if a.get("sentiment") is None:
            continue
        pub = a.get("published_at") or now
        if isinstance(pub, str):
            try:
                pub = datetime.fromisoformat(pub)
            except ValueError:
                pub = now
        if pub.tzinfo is not None:
            pub = pub.replace(tzinfo=None)
        age_h = max(0.0, (now - pub).total_seconds() / 3600)
        w     = math.exp(-age_h / 48.0)
        ws   += a["sentiment"] * w
        wt   += w
    return _clamp(ws / wt) if wt > 0 else None


# ═══════════════════════════════════════════════════════════════════════════════
# SIGNAL CALCULATORS
# ═══════════════════════════════════════════════════════════════════════════════

def calc_sentiment_signal(
    sentiment_score:   float | None,
    article_count:     int = 0,
    articles:          list[dict] | None = None,
    sentiment_history: list[Any] | None  = None,
    xs:                dict[str, float] | None = None,
) -> dict[str, Any]:
    """Category 1 — news sentiment, confidence-discounted and recency-weighted."""
    xs = xs or {}
    components: dict[str, Any] = {}
    signals: list[tuple[float, float]] = []
    confidence = 0.0

    if sentiment_score is not None:
        # Discount toward zero when few articles back the reading.
        confidence = min(1.0, article_count / 15.0) if article_count else 0.0
        adjusted   = sentiment_score * (0.4 + 0.6 * confidence)
        adjusted   = blend(adjusted, xs.get("raw_sentiment"))
        components["raw_sentiment"] = round(sentiment_score, 4)
        components["article_count"] = article_count
        components["conf_adjusted"] = round(adjusted, 4)
        signals.append((adjusted, 1.5))

    articles = _fresh_articles(articles or [])

    if sentiment_history and len(sentiment_history) >= 2:
        recent    = [h.score for h in sentiment_history[-3:]]
        momentum  = recent[-1] - recent[0]
        mom_score = _clamp(momentum * 2.5)
        components["sentiment_momentum"] = round(mom_score, 3)
        signals.append((mom_score, 1.0))

    if articles:
        # Distinct outlets, after headline-level dedup upstream. This used to be
        # computed over duplicate wire copy, so one syndicated story looked like
        # four independent sources agreeing.
        sources   = {a.get("source", "unknown") for a in articles}
        diversity = min(1.0, (len(sources) - 1) / 3.0)
        components["source_diversity"] = round(diversity, 3)
        # Diversity is a confidence modifier, not a directional signal — it was
        # previously added as a standalone *positive* score, which meant a widely
        # covered stock scored bullish regardless of what the coverage said.
        signals.append((diversity * 0.5 * (1 if (sentiment_score or 0) >= 0 else -1), 0.4))

        rec_score = _recency_weighted_sentiment(articles)
        if rec_score is not None:
            rec_score = blend(rec_score, xs.get("recency_sentiment"))
            components["recency_weighted"] = round(rec_score, 3)
            signals.append((rec_score, 1.2))

    if not signals:
        return _empty("No sentiment data")

    total_w = sum(w for _, w in signals)
    final   = _clamp(sum(s * w for s, w in signals) / total_w)
    avg_conf = round(min(1.0, (confidence + min(1.0, len(signals) / 4.0)) / 2.0), 3)

    parts = [f"raw {sentiment_score:+.3f}" if sentiment_score is not None else "no raw score"]
    for key, fmt in (("sentiment_momentum", "momentum {:+.3f}"),
                     ("recency_weighted", "recency {:+.3f}"),
                     ("source_diversity", "diversity {:.2f}")):
        if key in components:
            parts.append(fmt.format(components[key]))

    return {"score": round(final, 4), "confidence": avg_conf,
            "detail": " | ".join(parts), "components": components}


def calc_technical_signal(
    price_data: dict | None,
    xs: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Category 2 — point-in-time price snapshot signals."""
    if not price_data:
        return _empty("No price data")

    xs = xs or {}
    components: dict[str, float] = {}
    signals: list[tuple[float, float]] = []

    price = price_data.get("price")
    hi_52 = price_data.get("week_52_high")
    lo_52 = price_data.get("week_52_low")

    if price and hi_52 and lo_52 and hi_52 > lo_52:
        pct = (price - lo_52) / (hi_52 - lo_52)
        s   = blend((pct - 0.5) * 2, xs.get("52w_position"))
        components["52w_position"] = round(s, 3)
        signals.append((s, 1.2))

    chg = price_data.get("change_pct")
    if chg is not None:
        s = _clamp(chg / 5.0)
        components["day_momentum"] = round(s, 3)
        signals.append((s, 0.6))

    rating_map = {
        "strongbuy": 1.0, "buy": 0.6, "outperform": 0.5, "overweight": 0.5,
        "hold": 0.0, "neutral": 0.0, "marketperform": 0.0,
        "underperform": -0.5, "underweight": -0.5,
        "sell": -0.8, "strongsell": -1.0,
    }
    rating = (price_data.get("analyst_rating") or "").lower().replace(" ", "").replace("_", "")
    if rating in rating_map:
        # Absolute analyst ratings are ~+0.6 for most of the S&P and therefore
        # carry almost no cross-sectional information. Rank dominates here.
        s = blend(rating_map[rating], xs.get("analyst_rating_value"), weight=0.8)
        components["analyst_rating"] = round(s, 3)
        signals.append((s, 1.0))

    if price and hi_52 and hi_52 > 0:
        pct_from_high = (price - hi_52) / hi_52
        # Old mapping: (pct + 0.20) / 0.25 → +0.8 at the 52w high, and you had to
        # be 20% below the high before it reached zero. Positive for almost
        # everything. Recentred so "at the high" is +0.5 and -10% is ≈ 0.
        absolute = _clamp((pct_from_high + 0.10) / 0.20)
        s = blend(absolute, xs.get("breakout_proximity"))
        components["breakout_proximity"] = round(s, 3)
        signals.append((s, 0.8))

    if not signals:
        return _empty("Insufficient price data")

    total_w = sum(w for _, w in signals)
    score   = _clamp(sum(s * w for s, w in signals) / total_w)
    return {
        "score": round(score, 4),
        "confidence": round(min(1.0, len(signals) / 4.0), 3),
        "detail": " | ".join(f"{k}: {fmt_component(v)}" for k, v in components.items()),
        "components": components,
    }


def wilder_rsi(prices_or_returns: list[float], period: int = 14, from_returns: bool = True) -> float | None:
    """
    Wilder's RSI, seeded correctly.

    The previous implementation used ``alpha = 1 / len(returns)`` and seeded
    ``avg_gain`` from a *single* observation, over a 14-element series. Wilder's
    smoothing needs roughly 100+ bars to shed the influence of its seed, so those
    readings were essentially arbitrary — and did not match TradingView or
    Bloomberg despite the comment claiming they did.

    This seeds from a simple average of the first ``period`` values, then applies
    ``alpha = 1/period``, which is the textbook definition. Feed it 120 bars.
    """
    values = list(prices_or_returns or [])
    if not from_returns:
        values = [
            (values[i] - values[i - 1]) / values[i - 1] * 100
            for i in range(1, len(values)) if values[i - 1]
        ]
    if len(values) < period + 1:
        return None

    gains  = [max(0.0, r) for r in values]
    losses = [abs(min(0.0, r)) for r in values]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    alpha = 1.0 / period
    for g, l in zip(gains[period:], losses[period:]):
        avg_gain = avg_gain * (1 - alpha) + g * alpha
        avg_loss = avg_loss * (1 - alpha) + l * alpha

    if avg_loss == 0:
        return 100.0
    if avg_gain == 0:
        return 0.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def calc_price_momentum_signal(
    daily_returns: list[float],
    xs: dict[str, float] | None = None,
) -> dict[str, Any]:
    """
    Category 3 — price momentum.

    Expects a long series (120 bars) so RSI converges; trend, streak and
    acceleration are computed over the trailing 14 sessions.
    """
    if not daily_returns or len(daily_returns) < 5:
        return _empty("Insufficient price history (need 5+ trading days)")

    xs = xs or {}
    full   = daily_returns
    window = daily_returns[-14:]
    n      = len(window)
    components: dict[str, Any] = {}
    signals: list[tuple[float, float]] = []

    # ── RSI — monotonic momentum mapping ──────────────────────────────────────
    rsi = wilder_rsi(full, period=14)
    if rsi is not None:
        # Monotonic: higher RSI = more momentum, tapering at the extremes where
        # continuation becomes less reliable. Mean reversion is reported
        # separately rather than folded into the same number with the opposite
        # sign, which is what made the old mapping non-monotonic.
        if   rsi >= 80: rsi_score = 0.1     # extended; momentum real but fragile
        elif rsi >= 70: rsi_score = 0.5
        elif rsi >= 60: rsi_score = 0.4
        elif rsi >= 50: rsi_score = 0.15
        elif rsi >= 40: rsi_score = -0.15
        elif rsi >= 30: rsi_score = -0.4
        elif rsi >= 20: rsi_score = -0.5
        else:           rsi_score = -0.1    # washed out; momentum fading
        components["rsi"] = round(rsi, 1)
        components["rsi_score"] = round(rsi_score, 3)
        components["mean_reversion_flag"] = bool(rsi >= 80 or rsi <= 20)
        signals.append((rsi_score, 1.5))

    # ── Trend ─────────────────────────────────────────────────────────────────
    xs_i  = list(range(n))
    x_bar = sum(xs_i) / n
    y_bar = sum(window) / n
    num   = sum((x - x_bar) * (y - y_bar) for x, y in zip(xs_i, window))
    den   = sum((x - x_bar) ** 2 for x in xs_i)
    slope = num / den if den else 0.0

    trend_score = blend(_clamp(slope / 0.5), xs.get("trend_slope"))
    components["trend_slope_pct_per_day"] = round(slope, 3)
    components["trend_score"] = round(trend_score, 3)
    signals.append((trend_score, 1.2))

    # ── Volatility regime ─────────────────────────────────────────────────────
    if len(full) >= 10:
        vol_window = full[-30:] if len(full) >= 30 else full
        mean_r     = sum(vol_window) / len(vol_window)
        variance   = sum((r - mean_r) ** 2 for r in vol_window) / (len(vol_window) - 1)
        annual_vol = math.sqrt(variance) * math.sqrt(252)

        if   annual_vol < 20: vol_score = 0.3
        elif annual_vol < 35: vol_score = 0.1
        elif annual_vol < 55: vol_score = -0.1
        else:                 vol_score = -0.4

        components["annual_volatility_pct"] = round(annual_vol, 1)
        components["vol_score"] = round(vol_score, 3)
        signals.append((vol_score, 0.8))
    else:
        annual_vol = None

    # ── Consecutive-day streak ────────────────────────────────────────────────
    direction = 1 if window[-1] >= 0 else -1
    streak = 0
    for r in reversed(window):
        if (r >= 0 and direction == 1) or (r < 0 and direction == -1):
            streak += 1
        else:
            break

    streak_signed = streak * direction
    streak_score  = {0: 0.0, 1: 0.0, 2: 0.2, 3: 0.4, 4: 0.6}.get(abs(streak_signed), 0.7) * direction
    components["consecutive_day_streak"] = streak_signed
    components["streak_score"] = round(streak_score, 3)
    signals.append((streak_score, 0.9))

    # ── Short-term acceleration ───────────────────────────────────────────────
    if n >= 5:
        accel       = (sum(window[-5:]) / 5) - y_bar
        accel_score = blend(_clamp(accel / 0.5), xs.get("accel"))
        components["5d_vs_14d_acceleration"] = round(accel, 3)
        components["accel_score"] = round(accel_score, 3)
        signals.append((accel_score, 1.0))

    total_return = sum(window)
    components["14d_total_return_pct"] = round(total_return, 2)

    total_w = sum(w for _, w in signals)
    score   = _clamp(sum(s * w for s, w in signals) / total_w)
    # Confidence reflects how much history backs the RSI, which needs the most.
    confidence = min(1.0, len(full) / 60.0)

    detail = (
        f"RSI {rsi:.0f} | " if rsi is not None else "RSI n/a | "
    ) + (
        f"trend {slope:+.3f}%/day | "
        f"vol {annual_vol:.0f}% ann | " if annual_vol is not None else "vol n/a | "
    ) + f"streak {streak_signed:+d}d | 14d return {total_return:+.1f}%"

    return {
        "score": round(score, 4),
        "confidence": round(confidence, 3),
        "detail": detail,
        "components": components,
        "rsi": round(rsi, 1) if rsi is not None else None,
        "annual_vol": round(annual_vol, 1) if annual_vol is not None else None,
        "streak": streak_signed,
        "total_return_14d": round(total_return, 2),
        "mean_reversion_flag": components.get("mean_reversion_flag", False),
    }


def calc_fundamental_signal(
    price_data:       dict | None,
    earnings_history: list[dict] | None,
    xs:               dict[str, float] | None = None,
) -> dict[str, Any]:
    """Category 4 — valuation and earnings-surprise history."""
    xs = xs or {}
    signals: list[tuple[float, float]] = []
    components: dict[str, float] = {}

    pe = (price_data or {}).get("pe_ratio")
    if pe and pe > 0:
        if   pe < 15: pe_abs = 0.6
        elif pe < 25: pe_abs = 0.2
        elif pe < 35: pe_abs = 0.0
        elif pe < 50: pe_abs = -0.3
        elif pe < 70: pe_abs = -0.6
        else:         pe_abs = -0.9
        # An absolute P/E band is regime- and sector-dependent; ranking within
        # the basket is the more meaningful comparison.
        pe_score = blend(pe_abs, xs.get("pe_ratio"))
        components["pe_ratio"] = round(pe, 1)
        components["pe_score"] = round(pe_score, 3)
        signals.append((pe_score, 1.0))

    if earnings_history:
        # surprise_pct is now a genuine percentage — it used to hold yfinance's
        # epsDifference, an absolute dollar amount, so a $0.05 beat read as 0.5%
        # and this whole category sat near zero for every ticker.
        recent = [e for e in earnings_history if e.get("surprise_pct") is not None][:4]
        if recent:
            avg_surp   = sum(e["surprise_pct"] for e in recent) / len(recent)
            surp_score = _clamp(avg_surp / 10.0)
            components["avg_eps_surprise_pct"] = round(avg_surp, 2)
            components["surprise_score"] = round(surp_score, 3)
            signals.append((surp_score, 1.2))

        if len(recent) >= 2:
            beats        = sum(1 for e in recent if e["surprise_pct"] > 0)
            streak_score = (beats / len(recent) - 0.5) * 2
            components["beat_streak_of_4"] = beats
            components["streak_score"] = round(streak_score, 3)
            signals.append((streak_score, 0.8))

    if not signals:
        return _empty("No fundamental data")

    total_w = sum(w for _, w in signals)
    score   = _clamp(sum(s * w for s, w in signals) / total_w)
    return {
        "score": round(score, 4),
        "confidence": round(min(1.0, len(signals) / 3.0), 3),
        "detail": " | ".join(f"{k}: {fmt_component(v)}" for k, v in components.items()),
        "components": components,
    }


def calc_event_driven_signal(
    ticker:          str,
    earnings_events: list[dict] | None,
    price_data:      dict | None,
) -> dict[str, Any]:
    """
    Category 5 — event-driven signals.

    This is where the two contradictory earnings treatments used to live: this
    function scored earnings-within-7-days at **+0.5**, while the never-called
    ``calc_earnings_proximity_signal`` scored it at **-0.5**. For an options
    position the second was right — implied volatility inflates into a print and
    collapses immediately after, so a long option frequently loses money even
    when the direction is correct, and a short option is exposed to the gap.

    There is now one treatment, and approaching earnings reduces the score.
    """
    signals: list[tuple[float, float]] = []
    components: dict[str, Any] = {}
    today = market_date()

    days_away: int | None = None

    if earnings_events:
        future = []
        for e in earnings_events:
            rd = as_date(e.get("report_date"))
            if rd and rd >= today:
                future.append((rd, e))

        if future:
            report_date, _ = min(future, key=lambda x: x[0])
            days_away = (report_date - today).days
            if   days_away == 0: prox = -0.6   # print today — maximum uncertainty
            elif days_away == 1: prox = -0.5
            elif days_away == 2: prox = -0.4
            elif days_away == 3: prox = -0.3
            elif days_away <= 7: prox = -0.15
            elif days_away <= 21: prox = 0.05  # mild anticipation, IV still building
            else: prox = 0.0
            components["earnings_days_away"] = days_away
            components["earnings_proximity"] = round(prox, 3)
            signals.append((prox, 1.2))

        past = [
            e for e in earnings_events
            if e.get("eps_actual") is not None and as_date(e.get("report_date"))
        ]
        if past:
            most_recent = max(past, key=lambda e: as_date(e["report_date"]))
            surp = most_recent.get("surprise_pct")
            if surp is not None:
                result_score = _clamp(surp / 15.0)
                components["last_eps_surprise_pct"] = round(surp, 2)
                components["last_result_score"] = round(result_score, 3)
                signals.append((result_score, 1.0))

    if price_data:
        price = price_data.get("price")
        hi_52 = price_data.get("week_52_high")
        if price and hi_52 and hi_52 > 0:
            pct = (price - hi_52) / hi_52
            s   = 0.7 if pct >= -0.02 else 0.3 if pct >= -0.10 else 0.0
            components["near_52w_high"] = round(s, 3)
            signals.append((s, 0.6))

    if not signals:
        return _empty("No event data")

    total_w = sum(w for _, w in signals)
    score   = _clamp(sum(s * w for s, w in signals) / total_w)
    return {
        "score": round(score, 4),
        "confidence": round(min(1.0, len(signals) / 3.0), 3),
        "detail": " | ".join(f"{k}: {fmt_component(v)}" for k, v in components.items()),
        "components": components,
        "days_to_earnings": days_away,
    }


def calc_volume_signal(
    price_data: dict | None,
    xs: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Category 6 — volume anomaly, signed by the day's price direction."""
    if not price_data:
        return _empty("No price data")

    xs = xs or {}
    volume_ratio = price_data.get("volume_ratio")
    change_pct   = price_data.get("change_pct") or 0.0

    if volume_ratio is None:
        return _empty("No volume data")

    components = {"volume_ratio": round(volume_ratio, 2), "change_pct": round(change_pct, 3)}
    direction  = 1 if change_pct >= 0 else -1

    if   volume_ratio >= 3.0: intensity = 1.0
    elif volume_ratio >= 2.0: intensity = 0.7
    elif volume_ratio >= 1.5: intensity = 0.4
    elif volume_ratio >= 0.8: intensity = 0.0
    elif volume_ratio >= 0.5: intensity = -0.2
    else:                     intensity = -0.4

    score = blend(_clamp(intensity * direction), xs.get("volume_ratio_signed"), weight=0.4)
    components["intensity"] = round(intensity, 2)
    components["direction"] = direction

    return {
        "score": round(score, 4),
        "confidence": round(min(1.0, abs(volume_ratio - 1.0)), 3),
        "detail": f"vol ratio {volume_ratio:.2f}x | change {change_pct:+.2f}% | intensity {intensity:+.2f}",
        "components": components,
    }


def calc_short_interest_signal(
    price_data: dict | None,
    xs: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Category 7 — short squeeze vs distribution."""
    if not price_data:
        return _empty("No price data")

    xs = xs or {}
    short_ratio = price_data.get("short_ratio")
    short_pct   = price_data.get("short_pct_float")
    change_pct  = price_data.get("change_pct") or 0.0

    if short_ratio is None and short_pct is None:
        return _empty("No short data")

    components: dict[str, Any] = {}
    signals: list[tuple[float, float]] = []

    if short_ratio is not None:
        components["short_ratio_days"] = round(short_ratio, 1)
        if   short_ratio >= 10: si = 0.6 if change_pct > 0 else -0.5
        elif short_ratio >= 5:  si = 0.3 if change_pct > 0 else -0.2
        elif short_ratio >= 2:  si = 0.0
        else:                   si = 0.1
        si = blend(si, xs.get("short_squeeze_pressure"), weight=0.4)
        components["short_ratio_score"] = round(si, 3)
        signals.append((si, 1.2))

    if short_pct is not None:
        pct = short_pct * 100 if short_pct < 1 else short_pct
        components["short_pct_float"] = round(pct, 1)
        if   pct >= 20: ps = 0.5 if change_pct > 0 else -0.4
        elif pct >= 10: ps = 0.2 if change_pct > 0 else -0.2
        else:           ps = 0.0
        components["short_pct_score"] = round(ps, 3)
        signals.append((ps, 0.8))

    total_w = sum(w for _, w in signals)
    score   = _clamp(sum(s * w for s, w in signals) / total_w)
    return {
        "score": round(score, 4),
        "confidence": round(min(1.0, len(signals) / 2.0), 3),
        "detail": " | ".join(f"{k}: {fmt_component(v)}" for k, v in components.items()),
        "components": components,
    }


def calc_options_iv_signal(
    price_data: dict | None,
    iv_rank_data: dict | None = None,
) -> dict[str, Any]:
    """
    Category 8 — implied volatility regime.

    ``iv_rank_data`` comes from ``iv_rank.compute_iv_rank`` and is the fix for
    the central defect in the original: this signal read ``iv_52w_low`` /
    ``iv_52w_high`` off PriceSnapshot through ``getattr(..., None)``, but those
    columns did not exist, so the rank was always None. The raw-IV fallback read
    ``info["impliedVolatility"]``, which yfinance essentially never populates for
    equities. The category therefore returned 0.0 at confidence 0.0 on every
    ticker — while carrying the joint-highest weight in the model and being
    described as "the primary strategy selector".
    """
    iv_rank_data = iv_rank_data or {}
    price_data   = price_data or {}

    iv_rank = iv_rank_data.get("iv_rank")
    basis   = iv_rank_data.get("basis", "none")
    iv_now  = iv_rank_data.get("iv_current") or price_data.get("implied_volatility")

    if iv_rank is None and iv_now is None:
        return _empty("No IV data — run `python -m mktscan iv --backfill` to seed history")

    components: dict[str, Any] = {"iv_basis": basis}
    if iv_now is not None:
        iv_pct = iv_now * 100 if iv_now <= 3.0 else iv_now
        components["iv_pct"] = round(iv_pct, 1)
    else:
        iv_pct = None

    change_pct = price_data.get("change_pct") or 0.0
    beta       = price_data.get("beta")

    if iv_rank is not None:
        components["iv_rank"] = round(iv_rank, 1)
        components["iv_percentile"] = iv_rank_data.get("iv_pct")
        components["iv_history_days"] = iv_rank_data.get("data_days")
        # High rank = premium expensive = worse for buying, better for selling.
        if   iv_rank >= 80: regime = -0.5
        elif iv_rank >= 60: regime = -0.2
        elif iv_rank >= 40: regime = 0.0
        elif iv_rank >= 20: regime = 0.2
        else:               regime = 0.4
    elif iv_pct is not None:
        if   iv_pct < 20: regime = 0.3
        elif iv_pct < 30: regime = 0.1
        elif iv_pct < 45: regime = -0.1
        elif iv_pct < 65: regime = -0.3
        else:             regime = -0.5
    else:
        return _empty("No IV data")

    if beta and beta > 0:
        # High-beta names carry structurally higher IV; normalise so they are not
        # permanently penalised for it.
        beta_adj = _clamp((beta - 1.0) * -0.1, -0.2, 0.2)
        regime   = _clamp(regime + beta_adj)
        components["beta"] = round(beta, 2)
        components["beta_adj"] = round(beta_adj, 3)

    components["iv_regime_score"] = round(regime, 3)

    direction_signal = 0.0
    if iv_pct is not None:
        if iv_pct < 30 and change_pct > 0:
            direction_signal = 0.2
        elif iv_pct > 50 and change_pct < -2:
            direction_signal = -0.3

    score = _clamp(regime + direction_signal)

    # Confidence tracks the quality of the history behind the rank. A
    # proxy-based rank is capped low by compute_iv_rank because it is measuring
    # realised, not implied, volatility.
    confidence = iv_rank_data.get("confidence", 0.0) if iv_rank is not None else 0.25

    rank_str = f"IV rank {iv_rank:.0f} ({basis})" if iv_rank is not None else "IV rank unavailable"
    iv_str   = f"IV {iv_pct:.1f}%" if iv_pct is not None else "IV n/a"

    return {
        "score": round(score, 4),
        "confidence": round(confidence, 3),
        "detail": f"{iv_str} | {rank_str} | regime {regime:+.2f} | dir {direction_signal:+.2f}",
        "components": components,
        "iv_rank": iv_rank,
        "iv_basis": basis,
    }


def calc_analyst_signal(
    price_data: dict | None,
    xs: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Category 9 — analyst consensus and price-target upside."""
    if not price_data:
        return _empty("No price data")

    xs = xs or {}
    mean_score    = price_data.get("analyst_mean_score")
    target_price  = price_data.get("target_price")
    current_price = price_data.get("price")

    components: dict[str, Any] = {}
    signals: list[tuple[float, float]] = []

    if mean_score is not None:
        # Sell-side consensus clusters around 2.0 for most large caps, so the
        # absolute reading is nearly constant. Rank carries the information.
        ms_score = blend(_clamp((3.0 - mean_score) / 2.0),
                         xs.get("analyst_mean_inverted"), weight=0.8)
        components["analyst_mean_score"] = round(mean_score, 2)
        components["mean_score_signal"] = round(ms_score, 3)
        signals.append((ms_score, 1.5))

    if target_price and current_price and current_price > 0:
        upside_pct   = (target_price - current_price) / current_price * 100
        upside_score = blend(_clamp(upside_pct / 20.0), xs.get("target_upside_pct"))
        components["target_price"] = round(target_price, 2)
        components["upside_pct"] = round(upside_pct, 1)
        components["upside_score"] = round(upside_score, 3)
        signals.append((upside_score, 1.2))

    if not signals:
        return _empty("No analyst data")

    total_w = sum(w for _, w in signals)
    score   = _clamp(sum(s * w for s, w in signals) / total_w)
    return {
        "score": round(score, 4),
        "confidence": round(min(1.0, len(signals) / 2.0), 3),
        "detail": " | ".join(f"{k}: {fmt_component(v)}" for k, v in components.items()),
        "components": components,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# COMPOSITE
# ═══════════════════════════════════════════════════════════════════════════════

def compute_tradeability(
    ticker:            str,
    sentiment_score:   float | None,
    article_count:     int,
    articles:          list[dict],
    sentiment_history: list,
    price_data:        dict | None,
    earnings_events:   list[dict] | None,
    daily_returns:     list[float] | None = None,
    weights:           dict[str, float] | None = None,
    xs:                dict[str, float] | None = None,
    iv_rank_data:      dict | None = None,
) -> dict[str, Any]:
    """
    Compute the composite tradeability score for one ticker.

    ``xs`` holds this ticker's cross-sectionally ranked features (from
    cross_section.build_cross_sectional_scores); pass None to use absolute
    thresholds only. ``iv_rank_data`` comes from iv_rank.compute_iv_rank.
    """
    w  = _normalise_weights(weights or DEFAULT_WEIGHTS)
    xs = xs or {}

    categories = {
        "sentiment":      calc_sentiment_signal(sentiment_score, article_count,
                                                articles, sentiment_history, xs),
        "technical":      calc_technical_signal(price_data, xs),
        "price_momentum": calc_price_momentum_signal(daily_returns or [], xs),
        "fundamental":    calc_fundamental_signal(price_data, earnings_events, xs),
        "event_driven":   calc_event_driven_signal(ticker, earnings_events, price_data),
        "volume":         calc_volume_signal(price_data, xs),
        "short_interest": calc_short_interest_signal(price_data, xs),
        "options_iv":     calc_options_iv_signal(price_data, iv_rank_data),
        "analyst":        calc_analyst_signal(price_data, xs),
    }

    # ── Weighted combination ──────────────────────────────────────────────────
    # Categories below the confidence floor are excluded from *both* numerator
    # and denominator. The old formula kept 30% of a dead category's weight at a
    # score of 0.0, which is an active claim of neutrality that missing data does
    # not support — and with ~28% of total weight routinely dead, it compressed
    # every score toward NEUTRAL.
    total_weight = 0.0
    weighted_sum = 0.0
    skipped: list[str] = []

    for key, result in categories.items():
        confidence = result.get("confidence", 0.0)
        if confidence < MIN_CATEGORY_CONFIDENCE:
            skipped.append(key)
            continue
        effective_w   = w.get(key, 0.0) * confidence
        weighted_sum += result["score"] * effective_w
        total_weight += effective_w

    final = _clamp(round(weighted_sum / total_weight, 4)) if total_weight > 0 else 0.0

    # Coverage: how much of the intended weight actually had data behind it.
    # A score built on 30% coverage should not be presented like one built on 90%.
    intended = sum(w.get(k, 0.0) for k in CATEGORY_KEYS)
    covered  = sum(w.get(k, 0.0) for k in CATEGORY_KEYS if k not in skipped)
    coverage = round(covered / intended, 3) if intended else 0.0

    iv_signal    = categories["options_iv"]
    event_signal = categories["event_driven"]
    mom_signal   = categories["price_momentum"]

    days_to_earn = event_signal.get("days_to_earnings")
    annual_vol   = mom_signal.get("annual_vol")

    from .strategy import select_strategy
    spec = select_strategy(
        score=final,
        iv_rank=iv_signal.get("iv_rank"),
        days_to_earn=days_to_earn,
        annual_vol=annual_vol,
        iv_basis=iv_signal.get("iv_basis", "none"),
        signal_confidence=coverage,
    )

    return {
        "ticker":         ticker,
        "score":          final,
        "label":          tradeability_label(final),
        "color":          tradeability_color(final),
        "strategy":       spec.as_dict(),
        "strategy_spec":  spec,
        "weights_used":   w,
        "categories":     categories,
        "coverage":       coverage,
        "skipped_categories": skipped,
        "days_to_earnings":   days_to_earn,
        "annual_vol":     annual_vol,
        "rsi":            mom_signal.get("rsi"),
        "iv_rank":        iv_signal.get("iv_rank"),
        "iv_basis":       iv_signal.get("iv_basis", "none"),
        "computed_at":    utc_now().isoformat(),
    }


def _normalise_weights(weights: dict[str, float]) -> dict[str, float]:
    total = sum(weights.get(k, 0.0) for k in CATEGORY_KEYS)
    if total <= 0:
        return {k: round(1 / len(CATEGORY_KEYS), 4) for k in CATEGORY_KEYS}
    return {k: round(weights.get(k, 0.0) / total, 4) for k in CATEGORY_KEYS}


# ── Price history ─────────────────────────────────────────────────────────────

def fetch_daily_returns(ticker: str, bars: int = 120) -> list[float]:
    """
    Daily percentage returns, oldest first.

    Returns 120 bars by default rather than 14. Wilder's RSI needs roughly 100+
    observations to converge away from its seed value; feeding it 14 produced
    readings that were effectively arbitrary. Callers that only want the recent
    window can slice the tail.
    """
    try:
        import yfinance as yf

        end   = market_date()
        start = end - timedelta(days=int(bars * 1.6) + 10)   # allow for weekends/holidays
        raw   = yf.download(
            ticker, start=str(start), end=str(end + timedelta(days=1)),
            progress=False, auto_adjust=True,
        )
        if raw is None or raw.empty:
            return []

        if hasattr(raw.columns, "get_level_values"):
            try:
                closes = raw["Close"]
                if hasattr(closes, "squeeze"):
                    closes = closes.squeeze()
            except KeyError:
                closes = raw.iloc[:, 0]
        else:
            closes = raw["Close"] if "Close" in raw.columns else raw.iloc[:, 0]

        closes = closes.dropna().tail(bars + 1)
        pct    = closes.pct_change().dropna() * 100
        return [round(float(v), 4) for v in pct.values[-bars:]]
    except Exception as e:
        log.debug(f"[Tradeability] fetch_daily_returns({ticker}) failed: {e}")
        return []


# ── Basket computation ────────────────────────────────────────────────────────

def compute_basket_tradeability(
    session,
    weights: dict[str, float] | None = None,
    max_workers: int = 8,
) -> dict[str, dict]:
    """
    Compute tradeability for every active ticker.

    Two passes, because cross-sectional ranking needs the whole basket before any
    individual score can be finalised:

      Pass 1 — gather per-ticker inputs (DB rows + price history), in parallel.
      Pass 2 — rank features across the basket, then score each ticker.
    """
    from concurrent.futures import ThreadPoolExecutor
    from sqlalchemy import select, desc

    from .database import (
        get_basket, get_latest_scores, get_score_history,
        get_recent_articles, PriceSnapshot, EarningsEvent,
    )
    from .iv_rank import compute_iv_rank

    companies     = get_basket(session)
    latest_scores = {r.ticker: r for r in get_latest_scores(session)}

    # ── Pass 1a: database reads (single-threaded — one session) ───────────────
    inputs: dict[str, dict] = {}
    for company in companies:
        ticker   = company.ticker
        sent_row = latest_scores.get(ticker)

        price_row = session.execute(
            select(PriceSnapshot)
            .where(PriceSnapshot.ticker == ticker)
            .order_by(desc(PriceSnapshot.snapped_at))
            .limit(1)
        ).scalar_one_or_none()

        price_data = None
        if price_row:
            price_data = {
                "price":              price_row.price,
                "change_pct":         price_row.change_pct,
                "pe_ratio":           price_row.pe_ratio,
                "week_52_high":       price_row.week_52_high,
                "week_52_low":        price_row.week_52_low,
                "analyst_rating":     price_row.analyst_rating,
                "analyst_mean_score": price_row.analyst_mean_score,
                "target_price":       price_row.target_price,
                "market_cap":         price_row.market_cap,
                "volume_ratio":       price_row.volume_ratio,
                "short_ratio":        price_row.short_ratio,
                "short_pct_float":    price_row.short_pct_float,
                "implied_volatility": price_row.implied_volatility,
                "beta":               price_row.beta,
            }

        earn_rows = session.execute(
            select(EarningsEvent)
            .where(EarningsEvent.ticker == ticker)
            .order_by(desc(EarningsEvent.report_date))
            .limit(8)
        ).scalars().all()

        inputs[ticker] = {
            "sentiment_score":   sent_row.score if sent_row else None,
            "article_count":     sent_row.article_count if sent_row else 0,
            "articles": [
                {"source": a.source, "sentiment": a.sentiment,
                 "published_at": a.published_at, "headline": a.headline}
                for a in get_recent_articles(session, ticker, 50)
            ],
            "sentiment_history": get_score_history(session, ticker, days=14),
            "price_data":        price_data,
            "earnings_events": [
                {"ticker": e.ticker, "period": e.period, "report_date": e.report_date,
                 "eps_estimate": e.eps_estimate, "eps_actual": e.eps_actual,
                 "surprise_pct": e.surprise_pct, "is_upcoming": e.is_upcoming}
                for e in earn_rows
            ],
            "iv_rank_data":      compute_iv_rank(session, ticker),
        }

    # ── Pass 1b: price history (parallel — pure network I/O) ─────────────────
    tickers = list(inputs.keys())
    if tickers:
        with ThreadPoolExecutor(max_workers=min(max_workers, len(tickers))) as pool:
            for ticker, returns in zip(tickers, pool.map(fetch_daily_returns, tickers)):
                inputs[ticker]["daily_returns"] = returns

    # ── Pass 2a: cross-sectional feature ranking ─────────────────────────────
    features = {
        ticker: extract_features(
            data.get("price_data"),
            data.get("daily_returns"),
            data.get("sentiment_score"),
            data.get("articles"),
        )
        for ticker, data in inputs.items()
    }
    xs_scores = build_cross_sectional_scores(features)

    # ── Pass 2b: score each ticker ───────────────────────────────────────────
    results: dict[str, dict] = {}
    for ticker, data in inputs.items():
        result = compute_tradeability(
            ticker=ticker,
            sentiment_score=data.get("sentiment_score"),
            article_count=data.get("article_count", 0),
            articles=data.get("articles", []),
            sentiment_history=data.get("sentiment_history", []),
            price_data=data.get("price_data"),
            earnings_events=data.get("earnings_events"),
            daily_returns=data.get("daily_returns"),
            weights=weights,
            xs=xs_scores.get(ticker, {}),
            iv_rank_data=data.get("iv_rank_data"),
        )

        # Feedback calibration is now display-only by default; see feedback.py.
        try:
            from .feedback import get_accuracy_stats, apply_feedback_adjustment
            stats = get_accuracy_stats(session, ticker)
            adjusted, note = apply_feedback_adjustment(result["score"], stats)
            result["score_raw"]     = result["score"]
            result["score"]         = adjusted
            result["label"]         = tradeability_label(adjusted)
            result["color"]         = tradeability_color(adjusted)
            result["feedback_stats"] = stats
            result["feedback_note"]  = note
        except Exception as e:
            log.debug(f"[Tradeability] Feedback lookup failed for {ticker}: {e}")
            result["score_raw"]      = result["score"]
            result["feedback_stats"] = {}
            result["feedback_note"]  = "Feedback unavailable"

        results[ticker] = result

    return results
