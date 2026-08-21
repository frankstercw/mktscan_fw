from mktscan.terminal import (
    TechnicalOpportunity,
    detect_divergences,
    directional_conviction,
    signal_agreement,
)


def tech(**overrides):
    base = dict(
        ticker="NVDA", price=100.0, trend_state="STRONG BULL",
        momentum_state="ACCELERATING BULLISH", relative_strength_state="STRONG",
        volume_state="CONFIRMED", rsi14=64.0, adx14=31.0, rvol20=1.8,
        return_5d=5.0, return_20d=12.0, rs_spy_20d=6.0, rs_qqq_20d=5.0,
        momentum_acceleration=2.0, ema20=95.0, ema50=90.0, sma200=80.0,
    )
    base.update(overrides)
    return TechnicalOpportunity(**base)


def test_high_bullish_agreement():
    result = directional_conviction(
        .62, .85, "STRONG_RISK_ON", tech(),
        analyst_state="POSITIVE", options_bias="Bullish debit structure",
    )
    agreement = signal_agreement(result)
    assert "BULL" in result["direction"]
    assert result["conviction"] >= 60
    assert agreement["label"] in {"HIGH", "MODERATE"}
    assert agreement["opposing"] == 0


def test_detects_bullish_relative_strength_and_analyst_divergence():
    t = tech(relative_strength_state="LAGGING", volume_state="WEAK")
    result = directional_conviction(
        .55, .8, "RISK_ON", t, analyst_state="NEGATIVE",
    )
    divs = detect_divergences(result, t, analyst_state="NEGATIVE", iv_percentile=90)
    titles = {d["title"] for d in divs}
    assert "Relative-strength divergence" in titles
    assert "Analyst divergence" in titles
    assert "Volatility pricing risk" in titles
