"""
mktscan/cross_section.py
──────────────────────────────────────────────────────────────────────────────
Cross-sectional normalisation of raw signal features.

The problem this solves
───────────────────────
Every threshold in the original scoring layer was absolute, and several of the
inputs are near-constant positives for any large-cap in an uptrend:

  • ``breakout_proximity`` = (pct_from_52w_high + 0.20) / 0.25. At the 52-week
    high that returns +0.8, and you have to be a full 20% below the high before
    it even reaches zero. In a rising market it is positive for essentially
    every name in the basket.
  • ``analyst_rating``: the sell side is at "buy" or better on most of the S&P,
    so this contributed ≈ +0.6 to nearly everything.
  • ``analyst_mean_score``: typically ~2.0, i.e. ≈ +0.5 for nearly everything.
  • ``52w_position``: positive for any stock in an uptrend.

A signal that says the same thing about every name carries no information. It
shifts the whole basket's score up or down together and cannot rank anything,
which is exactly what a scanner needs to do. Meanwhile the absolute thresholds
were implicitly calibrated to one market regime — a P/E of 35 meant something
different in 2021 than it does now.

The fix
───────
Score each feature by where it sits *relative to the rest of the basket today*,
not against a hard-coded constant. A stock at the 90th percentile of 52-week
position within the basket scores +0.8 whether the whole market is at highs or
in a drawdown. The regime cancels out; the relative information survives.

Method: rank-based normalisation (percentile → [-1, +1]), with average ranks for
ties. Ranks are used rather than z-scores because these features are not
normally distributed — P/E and short interest in particular have long right
tails that would let one extreme name dominate a z-score.

Falls back to the original absolute mapping when the basket is too small for a
percentile to mean anything (``MIN_BASKET``).
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Iterable

log = logging.getLogger(__name__)

# Below this many observations a percentile is mostly noise: with 4 names the
# possible scores are only -1, -0.33, +0.33, +1 regardless of the actual spread.
MIN_BASKET = 6

# Features normalised cross-sectionally, and whether a higher raw value is
# bullish. Everything else keeps its absolute mapping — some thresholds are
# genuinely meaningful in absolute terms (RSI extremes, days to earnings), and
# ranking them would destroy real information.
XS_FEATURES: dict[str, bool] = {
    # feature name              higher is bullish
    "52w_position":             True,
    "breakout_proximity":       True,
    "analyst_rating_value":     True,
    "analyst_mean_inverted":    True,   # already flipped so higher = more bullish
    "target_upside_pct":        True,
    "pe_ratio":                 False,  # cheaper is better, all else equal
    "trend_slope":              True,
    "accel":                    True,
    "raw_sentiment":            True,
    "recency_sentiment":        True,
    "volume_ratio_signed":      True,
    "short_squeeze_pressure":   True,
}


def _average_ranks(values: list[float]) -> list[float]:
    """Ranks in [0, 1], ties sharing the average rank."""
    n = len(values)
    if n == 0:
        return []
    if n == 1:
        return [0.5]

    order = sorted(range(n), key=lambda i: values[i])
    ranks = [0.0] * n

    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg_rank = (i + j) / 2.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg_rank
        i = j + 1

    return [r / (n - 1) for r in ranks]


def percentile_scores(
    feature_by_ticker: dict[str, float | None],
    higher_is_bullish: bool = True,
) -> dict[str, float]:
    """
    Map one feature across the basket onto [-1, +1] by percentile rank.

    Tickers with a missing value are omitted from the result entirely rather
    than being imputed to the median — an absent value is not evidence of an
    average value, and pretending otherwise silently invents signal.
    """
    present = {t: v for t, v in feature_by_ticker.items() if v is not None}
    if len(present) < MIN_BASKET:
        return {}

    tickers = list(present.keys())
    values  = [float(present[t]) for t in tickers]

    # A feature with no dispersion carries no cross-sectional information.
    if max(values) == min(values):
        return {t: 0.0 for t in tickers}

    ranks = _average_ranks(values)
    out: dict[str, float] = {}
    for ticker, rank in zip(tickers, ranks):
        score = rank * 2.0 - 1.0            # [0,1] → [-1,+1]
        out[ticker] = round(score if higher_is_bullish else -score, 4)
    return out


def build_cross_sectional_scores(
    features_by_ticker: dict[str, dict[str, float | None]],
) -> dict[str, dict[str, float]]:
    """
    Turn raw per-ticker features into cross-sectionally ranked scores.

    Input:  {"AAPL": {"52w_position": 0.87, "pe_ratio": 31.2, ...}, ...}
    Output: {"AAPL": {"52w_position": +0.44, "pe_ratio": -0.11, ...}, ...}

    Only features listed in ``XS_FEATURES`` are transformed.
    """
    if len(features_by_ticker) < MIN_BASKET:
        log.debug(
            f"[XS] Basket of {len(features_by_ticker)} is below the {MIN_BASKET} "
            f"minimum — falling back to absolute thresholds"
        )
        return {t: {} for t in features_by_ticker}

    result: dict[str, dict[str, float]] = {t: {} for t in features_by_ticker}

    for feature, higher_is_bullish in XS_FEATURES.items():
        column = {t: feats.get(feature) for t, feats in features_by_ticker.items()}
        scored = percentile_scores(column, higher_is_bullish)
        for ticker, score in scored.items():
            result[ticker][feature] = score

    return result


def blend(absolute: float, cross_sectional: float | None, weight: float = 0.65) -> float:
    """
    Blend an absolute mapping with its cross-sectional counterpart.

    Neither alone is right. Pure cross-sectional loses genuine absolute
    information — if every name in the basket is overbought, the most overbought
    one still should not read as a buy just because it tops the ranking. Pure
    absolute is regime-dependent and, as shown above, often degenerate.

    Default leans toward the cross-sectional view (0.65) because that is where
    the discriminating power is, while retaining a third of the absolute signal
    as a level anchor.
    """
    if cross_sectional is None:
        return absolute
    blended = weight * cross_sectional + (1.0 - weight) * absolute
    return round(max(-1.0, min(1.0, blended)), 4)


def summarise_dispersion(features_by_ticker: dict[str, dict[str, Any]]) -> dict[str, dict]:
    """
    Report spread per feature — a diagnostic for whether a feature is doing
    anything at all. A feature whose basket-wide standard deviation is near zero
    is a constant in disguise and should be dropped or re-specified.
    """
    out: dict[str, dict] = {}
    for feature in XS_FEATURES:
        values = [
            float(f[feature]) for f in features_by_ticker.values()
            if f.get(feature) is not None
        ]
        if len(values) < 2:
            out[feature] = {"n": len(values), "stdev": None, "degenerate": True}
            continue
        mean  = sum(values) / len(values)
        var   = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
        stdev = var ** 0.5
        spread = (max(values) - min(values)) or 0.0
        out[feature] = {
            "n": len(values),
            "mean": round(mean, 4),
            "stdev": round(stdev, 4),
            "min": round(min(values), 4),
            "max": round(max(values), 4),
            # Flag features that barely vary across the basket.
            "degenerate": bool(abs(mean) > 1e-9 and stdev / abs(mean) < 0.05) or spread == 0.0,
        }
    return out
