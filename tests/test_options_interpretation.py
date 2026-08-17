from types import SimpleNamespace
from mktscan.options_interpretation import interpret_options_market


def snap(**kw):
    base = dict(iv_rank_1y=30, iv_percentile_1y=25, iv_30d=.40, iv_60d=.36,
                iv_90d=.35, term_state="BACKWARDATION", put_skew=.05,
                call_skew=-.01, expected_move_pct=6.5, confidence=1.0)
    base.update(kw)
    return SimpleNamespace(**base)


def test_bullish_low_iv_prefers_debit():
    x = interpret_options_market(snap(), .55, "BULLISH")
    assert x.structure_bias == "Bullish debit structure"
    assert "bull call spread" in x.thesis.lower()
    assert x.cautions


def test_bullish_high_iv_prefers_credit():
    x = interpret_options_market(snap(iv_percentile_1y=85), .55, "BULLISH")
    assert x.structure_bias == "Bullish credit structure"
    assert "bull put spread" in x.thesis.lower()


def test_unknown_iv_is_explicit():
    x = interpret_options_market(snap(iv_rank_1y=None, iv_percentile_1y=None), 0, "NEUTRAL")
    assert x.iv_state == "UNKNOWN"
    assert any("unknown" in c.lower() for c in x.cautions)
