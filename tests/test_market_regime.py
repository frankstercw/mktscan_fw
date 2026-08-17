import numpy as np
import pandas as pd

from mktscan.regime import compute_regime_from_data, _rates_from_frame


def _market_frame(vix_start=24.0, vix_end=16.0):
    idx = pd.date_range("2025-08-01", periods=260, freq="B")
    series = {
        "SPY": np.linspace(500, 650, len(idx)),
        "QQQ": np.linspace(450, 620, len(idx)),
        "AAA": np.linspace(100, 145, len(idx)),
        "BBB": np.linspace(80, 115, len(idx)),
        "CCC": np.linspace(60, 90, len(idx)),
        "^VIX": np.linspace(vix_start, vix_end, len(idx)),
    }
    cols = pd.MultiIndex.from_product([["Close"], list(series)])
    arr = np.column_stack([series[t] for t in series])
    return pd.DataFrame(arr, index=idx, columns=cols)


def _rates():
    return pd.DataFrame({
        "DATE": pd.date_range("2026-06-01", periods=40, freq="B"),
        "DGS2": np.linspace(4.4, 4.1, 40),
        "DGS10": np.linspace(4.7, 4.3, 40),
    })


def test_risk_on_regime_from_trend_breadth_and_falling_vix():
    result = compute_regime_from_data(
        _market_frame(), ["AAA", "BBB", "CCC"], _rates(),
        {"risk_score": 0.0, "confidence": 1.0},
    )
    assert result["score"] > 0.35
    assert "RISK_ON" in result["label"]
    assert result["breadth"]["above_50d"] == 100.0
    assert result["volatility"]["score"] > 0
    assert result["confidence"] > 0.8


def test_macro_event_adds_caution_without_changing_directional_score():
    base = compute_regime_from_data(
        _market_frame(), ["AAA", "BBB", "CCC"], _rates(),
        {"risk_score": 0.0, "confidence": 1.0},
    )
    caution = compute_regime_from_data(
        _market_frame(), ["AAA", "BBB", "CCC"], _rates(),
        {"risk_score": 0.8, "confidence": 1.0},
    )
    assert caution["score"] == base["score"]
    assert caution["label"].endswith("CAUTION")


def test_rates_score_rewards_falling_long_yields_and_positive_curve():
    result = _rates_from_frame(_rates())
    assert result["two_year"] is not None
    assert result["ten_year"] is not None
    assert result["curve_10y_2y"] > 0
    assert result["ten_year_20d_change_bps"] < 0
    assert result["score"] > 0
