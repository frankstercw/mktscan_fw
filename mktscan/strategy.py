"""
mktscan/strategy.py
──────────────────────────────────────────────────────────────────────────────
The single options-strategy selector.

There used to be two, and they contradicted each other:

  • ``tradeability.suggest_options_strategy`` mapped direction × IV rank onto
    spreads, iron condors and cash-secured puts.
  • ``options._select_strategy`` ignored IV entirely and only ever produced naked
    long calls and long puts.

Both ran on every ticker. The dashboard displayed the second. For a bullish name
with a high IV rank the first said "sell a cash-secured put, premium is rich" and
the second said "buy a call" — opposite sides of the same trade, from the same
score, in the same run.

This module is now the only place a strategy is chosen. It returns a *spec*
(structure, target DTE, strike rule, sizing) and options.py turns that spec into
concrete strikes and prices from the live chain. Selection logic and pricing
logic stay separate.

Design notes
────────────
The old selector reached for a 1-week ATM long option on its strongest signals.
That is the worst available structure for a directional view: maximum theta,
maximum gamma risk, and a breakeven that needs the move to happen almost
immediately. The default here is 30–45 DTE, which is the standard window for
directional retail structures — enough time for a thesis to work, before the
steep part of the theta curve.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Direction = Literal["bullish", "bearish", "neutral"]
IVBucket  = Literal["low", "mid", "high", "unknown"]

# Score band that counts as directional. Inside ±0.20 we do not claim an edge.
DIRECTIONAL_THRESHOLD = 0.20
STRONG_THRESHOLD      = 0.50

# IV rank buckets. 30/70 are the conventional retail cut points for
# "premium is cheap" vs "premium is rich".
IV_LOW_MAX  = 30.0
IV_HIGH_MIN = 70.0

# Never hold a short-dated directional option through an earnings print: the
# post-event IV crush routinely costs more than the underlying move returns.
EARNINGS_BLACKOUT_DAYS = 3
EARNINGS_CAUTION_DAYS  = 10


@dataclass
class StrategySpec:
    """What to trade, before it is priced against a real chain."""
    name:          str
    structure:     str               # long_call | bull_call_spread | iron_condor | ...
    direction:     Direction
    legs:          list[dict[str, Any]] = field(default_factory=list)
    target_dte:    int   = 35
    min_dte:       int   = 21
    max_dte:       int   = 60
    sizing:        str   = "full"    # full | half | quarter | avoid
    rationale:     str   = ""
    iv_note:       str   = ""
    tradeable:     bool  = True
    avoid_reason:  str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "structure": self.structure,
            "direction": self.direction, "legs": self.legs,
            "target_dte": self.target_dte, "min_dte": self.min_dte,
            "max_dte": self.max_dte, "sizing": self.sizing,
            "rationale": self.rationale, "iv_note": self.iv_note,
            "tradeable": self.tradeable, "avoid_reason": self.avoid_reason,
        }


# ── Bucketing ─────────────────────────────────────────────────────────────────

def classify_direction(score: float) -> Direction:
    if score >= DIRECTIONAL_THRESHOLD:
        return "bullish"
    if score <= -DIRECTIONAL_THRESHOLD:
        return "bearish"
    return "neutral"


def classify_iv(iv_rank: float | None, iv_basis: str = "chain") -> IVBucket:
    """
    Bucket the IV rank.

    A rank computed from the realised-volatility proxy rather than true option
    IV is deliberately treated as ``unknown``: it measures a different quantity,
    and picking between buying and selling premium on that basis would be a
    guess dressed up as a signal.
    """
    if iv_rank is None or iv_basis != "chain":
        return "unknown"
    if iv_rank >= IV_HIGH_MIN:
        return "high"
    if iv_rank <= IV_LOW_MAX:
        return "low"
    return "mid"


# ── The grid ──────────────────────────────────────────────────────────────────
#
# Strike rules are expressed as target deltas, not fixed OTM percentages. A "2%
# OTM" strike means something completely different on a 15%-vol utility than on
# an 80%-vol miner; delta normalises for that automatically, and it is how the
# strike actually gets selected from the chain.

_GRID: dict[tuple[Direction, IVBucket], dict[str, Any]] = {
    ("bullish", "low"): {
        "name": "Bull Call Spread",
        "structure": "bull_call_spread",
        "legs": [
            {"action": "BUY",  "right": "C", "target_delta": 0.45},
            {"action": "SELL", "right": "C", "target_delta": 0.25},
        ],
        "rationale": (
            "Bullish signal with IV rank in the bottom third — premium is cheap. "
            "A debit spread keeps the long-vega benefit of cheap options while "
            "capping the cost, so a stalled move does not cost the full outlay."
        ),
    },
    ("bullish", "mid"): {
        "name": "Bull Call Spread",
        "structure": "bull_call_spread",
        "legs": [
            {"action": "BUY",  "right": "C", "target_delta": 0.45},
            {"action": "SELL", "right": "C", "target_delta": 0.25},
        ],
        "rationale": (
            "Bullish signal with mid-range IV. Defined-risk debit spread: the "
            "short leg finances roughly a third of the premium and cuts the "
            "breakeven closer to spot."
        ),
    },
    ("bullish", "high"): {
        "name": "Bull Put Spread",
        "structure": "bull_put_spread",
        "legs": [
            {"action": "SELL", "right": "P", "target_delta": 0.30},
            {"action": "BUY",  "right": "P", "target_delta": 0.15},
        ],
        "rationale": (
            "Bullish signal with IV rank in the top third — premium is rich, so "
            "sell it rather than buy it. A put credit spread profits from time "
            "decay and IV contraction, and still wins if the stock merely holds "
            "its level. Defined risk, unlike a naked cash-secured put."
        ),
    },
    ("bearish", "low"): {
        "name": "Bear Put Spread",
        "structure": "bear_put_spread",
        "legs": [
            {"action": "BUY",  "right": "P", "target_delta": 0.45},
            {"action": "SELL", "right": "P", "target_delta": 0.25},
        ],
        "rationale": (
            "Bearish signal with IV rank in the bottom third — puts are cheap. "
            "Debit spread caps the cost of being early."
        ),
    },
    ("bearish", "mid"): {
        "name": "Bear Put Spread",
        "structure": "bear_put_spread",
        "legs": [
            {"action": "BUY",  "right": "P", "target_delta": 0.45},
            {"action": "SELL", "right": "P", "target_delta": 0.25},
        ],
        "rationale": (
            "Bearish signal with mid-range IV. Defined-risk debit spread rather "
            "than a naked put, which would need a larger move to clear its "
            "breakeven."
        ),
    },
    ("bearish", "high"): {
        "name": "Bear Call Spread",
        "structure": "bear_call_spread",
        "legs": [
            {"action": "SELL", "right": "C", "target_delta": 0.30},
            {"action": "BUY",  "right": "C", "target_delta": 0.15},
        ],
        "rationale": (
            "Bearish signal with IV rank in the top third. Selling a call spread "
            "collects the elevated premium and profits if the stock falls, stalls "
            "or drifts up slightly — a wider win zone than a long put."
        ),
    },
    ("neutral", "high"): {
        "name": "Iron Condor",
        "structure": "iron_condor",
        "legs": [
            {"action": "SELL", "right": "P", "target_delta": 0.20},
            {"action": "BUY",  "right": "P", "target_delta": 0.10},
            {"action": "SELL", "right": "C", "target_delta": 0.20},
            {"action": "BUY",  "right": "C", "target_delta": 0.10},
        ],
        "rationale": (
            "No directional edge, but IV rank is in the top third. Selling both "
            "wings monetises the elevated premium without taking a side. Manage "
            "at roughly 50% of max profit."
        ),
    },
    ("neutral", "low"): {
        "name": "No Trade",
        "structure": "none",
        "legs": [],
        "rationale": (
            "No directional edge and IV rank is already low, so there is no "
            "premium worth selling and nothing to suggest a move worth buying. "
            "A long straddle here needs a specific catalyst this tool does not "
            "detect — waiting is the position."
        ),
    },
    ("neutral", "mid"): {
        "name": "No Trade",
        "structure": "none",
        "legs": [],
        "rationale": (
            "No directional edge and IV rank is mid-range. Nothing to buy, "
            "nothing worth selling. Wait for the score to clear ±0.20 or for IV "
            "rank to reach an extreme."
        ),
    },
}

# Without a trustworthy IV rank we cannot tell whether premium is cheap or rich,
# so we take the structure that is least sensitive to being wrong about it: a
# debit spread, at reduced size.
_UNKNOWN_IV: dict[Direction, dict[str, Any]] = {
    "bullish": {
        "name": "Bull Call Spread",
        "structure": "bull_call_spread",
        "legs": [
            {"action": "BUY",  "right": "C", "target_delta": 0.45},
            {"action": "SELL", "right": "C", "target_delta": 0.25},
        ],
        "rationale": (
            "Bullish signal, but IV rank is unavailable or proxy-based, so we "
            "cannot tell whether premium is cheap or rich. A debit spread is the "
            "least IV-sensitive way to express the view; size is reduced to "
            "reflect the missing information."
        ),
    },
    "bearish": {
        "name": "Bear Put Spread",
        "structure": "bear_put_spread",
        "legs": [
            {"action": "BUY",  "right": "P", "target_delta": 0.45},
            {"action": "SELL", "right": "P", "target_delta": 0.25},
        ],
        "rationale": (
            "Bearish signal, but IV rank is unavailable or proxy-based. A debit "
            "spread limits the damage of misjudging the volatility regime; size "
            "is reduced accordingly."
        ),
    },
    "neutral": {
        "name": "No Trade",
        "structure": "none",
        "legs": [],
        "rationale": "No directional edge and no reliable IV rank. Nothing to do.",
    },
}


def select_strategy(
    score:        float,
    iv_rank:      float | None = None,
    days_to_earn: int | None   = None,
    annual_vol:   float | None = None,
    iv_basis:     str          = "chain",
    signal_confidence: float   = 1.0,
) -> StrategySpec:
    """
    Choose a strategy spec from the composite score and the volatility regime.

    Parameters
    ----------
    score             : composite tradeability score in [-1, +1]
    iv_rank           : 0-100 IV rank, or None
    days_to_earn      : calendar days to the next earnings report, or None
    annual_vol        : annualised realised volatility, in percent
    iv_basis          : "chain" | "proxy" | "none" — see iv_rank.compute_iv_rank
    signal_confidence : 0-1 aggregate confidence behind the score
    """
    direction = classify_direction(score)
    iv_bucket = classify_iv(iv_rank, iv_basis)

    # ── Earnings blackout — overrides everything ──────────────────────────────
    if days_to_earn is not None and 0 <= days_to_earn <= EARNINGS_BLACKOUT_DAYS:
        return StrategySpec(
            name="Avoid", structure="none", direction=direction,
            sizing="avoid", tradeable=False, avoid_reason="earnings_too_close",
            rationale=(
                f"Earnings in {days_to_earn} day(s). IV is inflated into the "
                f"print and collapses immediately after, so a long option can "
                f"lose money even when the direction is right. Short premium is "
                f"exposed to the gap. No position."
            ),
        )

    template = (_GRID.get((direction, iv_bucket)) if iv_bucket != "unknown"
                else _UNKNOWN_IV[direction])

    spec = StrategySpec(
        name      = template["name"],
        structure = template["structure"],
        direction = direction,
        legs      = [dict(leg) for leg in template["legs"]],
        rationale = template["rationale"],
        tradeable = template["structure"] != "none",
    )

    if not spec.tradeable:
        spec.sizing = "avoid"
        spec.avoid_reason = "no_edge"
        return spec

    # ── Sizing ────────────────────────────────────────────────────────────────
    sizing = "full"
    reasons: list[str] = []

    if iv_bucket == "unknown":
        sizing = "half"
        reasons.append("IV rank unavailable")

    if days_to_earn is not None and days_to_earn <= EARNINGS_CAUTION_DAYS:
        sizing = "half" if sizing == "full" else "quarter"
        reasons.append(f"earnings in {days_to_earn}d")

    if direction != "neutral" and abs(score) < STRONG_THRESHOLD:
        # A moderate directional signal gets moderate size. The previous version
        # sized moderate and strong signals identically and only varied the
        # structure. Skipped for neutral structures like the condor, where a
        # score near zero is the *premise* of the trade rather than a weakness.
        sizing = "half" if sizing == "full" else sizing
        reasons.append("moderate signal strength")

    if signal_confidence < 0.4:
        sizing = "quarter"
        reasons.append(f"low signal confidence ({signal_confidence:.2f})")

    spec.sizing = sizing

    # ── Expiry ────────────────────────────────────────────────────────────────
    # 30-45 DTE by default. Push past a known earnings date rather than expiring
    # into it, so the position is not forced to carry event risk it was not
    # sized for.
    if days_to_earn is not None and EARNINGS_BLACKOUT_DAYS < days_to_earn <= 45:
        spec.target_dte = max(spec.target_dte, days_to_earn + 10)
        spec.min_dte    = max(spec.min_dte, days_to_earn + 5)
        spec.max_dte    = max(spec.max_dte, days_to_earn + 30)
        spec.rationale += (
            f" Expiry is set beyond the earnings date ({days_to_earn}d out) so "
            f"the position is not forced to hold through the print."
        )

    # ── Notes ─────────────────────────────────────────────────────────────────
    iv_note_parts: list[str] = []
    if iv_rank is not None and iv_basis == "chain":
        iv_note_parts.append(f"IV rank {iv_rank:.0f} ({iv_bucket} regime).")
    elif iv_basis == "proxy":
        iv_note_parts.append(
            "IV rank is proxy-based (realised vol, not option IV) — treated as "
            "unknown for strategy selection."
        )
    else:
        iv_note_parts.append("No IV history yet — rank unavailable.")

    if annual_vol is not None:
        iv_note_parts.append(f"Realised vol {annual_vol:.0f}% annualised.")
        if annual_vol > 60:
            iv_note_parts.append(
                "That is high — widen strikes or cut size; the stop will be hit "
                "by noise alone at normal position sizing."
            )

    if reasons:
        iv_note_parts.append(f"Size reduced to {sizing}: {', '.join(reasons)}.")

    spec.iv_note = " ".join(iv_note_parts)
    return spec
