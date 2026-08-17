"""Human-readable interpretation of Options Market v2 snapshots.

This module is deliberately advisory/contextual. It does not modify tradeability,
strategy selection, or position sizing. Thresholds are transparent heuristics so
we can validate them before allowing them to affect production decisions.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class OptionsInterpretation:
    iv_state: str
    term_view: str
    skew_view: str
    move_view: str
    structure_bias: str
    thesis: str
    cautions: tuple[str, ...]


def _iv_state(rank: float | None, pct: float | None) -> tuple[str, str]:
    x = pct if pct is not None else rank
    if x is None:
        return "UNKNOWN", "Not enough true historical chain-IV data for a reliable relative-volatility reading."
    if x < 20:
        return "VERY LOW", f"IV is near the bottom of its historical distribution ({x:.0f}). Long-premium structures face a relatively low volatility hurdle."
    if x < 40:
        return "LOW", f"IV is relatively inexpensive versus its own history ({x:.0f}). Debit structures are comparatively more attractive, all else equal."
    if x < 60:
        return "NORMAL", f"IV is near the middle of its historical distribution ({x:.0f}); volatility level alone does not strongly favor debit or credit structures."
    if x < 80:
        return "HIGH", f"IV is relatively rich versus its own history ({x:.0f}). Premium-selling structures become more interesting, subject to event and tail risk."
    return "VERY HIGH", f"IV is near the top of its historical distribution ({x:.0f}). Long premium has a high volatility hurdle and short-premium risk is also elevated."


def _term_view(state: str | None, iv30: float | None, iv60: float | None) -> str:
    if state == "BACKWARDATION":
        gap = (iv30 - iv60) * 100 if iv30 is not None and iv60 is not None else None
        suffix = f" Front IV is about {gap:.1f} vol points above 60D." if gap is not None else ""
        return "Near-term volatility is elevated relative to later expirations; check earnings, macro events, or acute market stress." + suffix
    if state == "CONTANGO":
        return "The volatility curve slopes upward with maturity, a more normal structure without an obvious front-end volatility premium."
    if state == "FLAT":
        return "The volatility curve is fairly flat; expiration choice should be driven more by thesis horizon, theta, and liquidity."
    return "Term structure is unavailable or incomplete."


def _skew_view(put_skew: float | None, call_skew: float | None) -> str:
    # stored as decimal IV differences; 0.05 == five vol points
    if put_skew is None and call_skew is None:
        return "Skew is unavailable."
    p = put_skew * 100 if put_skew is not None else None
    c = call_skew * 100 if call_skew is not None else None
    if p is not None and p >= 4 and (c is None or p - c >= 3):
        return f"Downside protection is expensive: 25Δ put IV is about {p:.1f} vol points above ATM. Put-selling spreads may collect richer skew, but downside tail risk matters."
    if c is not None and c >= 4 and (p is None or c - p >= 3):
        return f"Upside calls are carrying rich skew: 25Δ call IV is about {c:.1f} vol points above ATM. Chasing outright calls may be expensive; a call spread can offset some wing premium."
    if p is not None and c is not None:
        return f"Skew is relatively balanced (put {p:+.1f}, call {c:+.1f} vol points vs ATM). There is no large wing-pricing distortion."
    x, label = (p, "put") if p is not None else (c, "call")
    return f"Only {label} skew is available ({x:+.1f} vol points vs ATM), so the surface read is incomplete."


def _move_view(move_pct: float | None) -> str:
    if move_pct is None:
        return "Expected move is unavailable."
    return f"Options imply roughly a ±{move_pct:.1f}% move over the selected near-term horizon. Compare this with your own directional forecast before paying for optionality."


def interpret_options_market(snapshot: Any, direction_score: float | None = None,
                             direction_label: str | None = None) -> OptionsInterpretation:
    """Interpret one OptionsMarketSnapshot-like object.

    direction_score/label are optional and are used only to explain possible
    expression. They never alter the stored tradeability score.
    """
    rank = getattr(snapshot, "iv_rank_1y", None)
    pct = getattr(snapshot, "iv_percentile_1y", None)
    iv_state, iv_text = _iv_state(rank, pct)
    term = getattr(snapshot, "term_state", None)
    put_skew = getattr(snapshot, "put_skew", None)
    call_skew = getattr(snapshot, "call_skew", None)
    move = getattr(snapshot, "expected_move_pct", None)

    bullish = (direction_score is not None and direction_score >= 0.20) or str(direction_label or "").upper().startswith("BULL")
    bearish = (direction_score is not None and direction_score <= -0.20) or str(direction_label or "").upper().startswith("BEAR")
    high_iv = iv_state in {"HIGH", "VERY HIGH"}
    low_iv = iv_state in {"LOW", "VERY LOW"}

    cautions: list[str] = []
    if term == "BACKWARDATION":
        cautions.append("Front-end IV is elevated; verify whether earnings or another event sits inside the intended holding period.")
    if pct is None and rank is None:
        cautions.append("IV Rank/Percentile is unknown; avoid treating the volatility regime as confirmed.")
    confidence = getattr(snapshot, "confidence", None)
    if confidence is not None and confidence < 0.70:
        cautions.append(f"Options-market data coverage is only {confidence:.0%}; interpretation is lower confidence.")

    if bullish:
        if low_iv:
            bias = "Bullish debit structure"
            thesis = "The underlying signal is bullish while IV is relatively inexpensive. A bull call spread is a natural first structure to evaluate; compare its breakeven with the implied move."
        elif high_iv:
            bias = "Bullish credit structure"
            thesis = "The underlying signal is bullish while IV is rich. A defined-risk bull put spread may better exploit premium, provided downside skew and event risk are acceptable."
        else:
            bias = "Bullish — structure neutral"
            thesis = "The directional signal is bullish, but volatility level does not strongly favor debit versus credit. Let liquidity, skew, expected move, and event timing decide the structure."
    elif bearish:
        if low_iv:
            bias = "Bearish debit structure"
            thesis = "The underlying signal is bearish while IV is relatively inexpensive. A bear put spread is a natural first structure to evaluate."
        elif high_iv:
            bias = "Bearish credit structure"
            thesis = "The underlying signal is bearish while IV is rich. A defined-risk bear call spread may better exploit premium, subject to squeeze/event risk."
        else:
            bias = "Bearish — structure neutral"
            thesis = "The directional signal is bearish, but volatility level does not strongly favor debit versus credit. Use skew, liquidity, expected move, and event timing to choose expression."
    else:
        if high_iv:
            bias = "Neutral / premium-selling candidate"
            thesis = "There is no strong directional signal and IV is rich. Defined-risk neutral premium structures can be researched, but only when event risk and expected move support the range thesis."
        elif low_iv:
            bias = "No clear options edge"
            thesis = "There is no strong directional signal and IV is inexpensive. Cheap volatility alone is not a reason to trade; wait for a directional or volatility catalyst thesis."
        else:
            bias = "No strong structure preference"
            thesis = "Neither direction nor relative IV provides a strong structure edge. Treat this as context rather than a trade candidate."

    return OptionsInterpretation(
        iv_state=iv_state,
        term_view=_term_view(term, getattr(snapshot, "iv_30d", None), getattr(snapshot, "iv_60d", None)),
        skew_view=_skew_view(put_skew, call_skew),
        move_view=_move_view(move),
        structure_bias=bias,
        thesis=thesis,
        cautions=tuple(cautions),
    )
