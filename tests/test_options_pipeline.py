"""
tests/test_options_pipeline.py

Regression tests for the defects fixed in the options pipeline rewrite. Each test
names the bug it guards against, so a future change that reintroduces one fails
with an explanation rather than a bare assertion.

These cover the pure-logic layers (pricing, strategy selection, cross-sectional
ranking, signal construction, market clock). Database and network paths are
covered by tests/test_scrapers.py.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta

import pytest

from mktscan import clock
from mktscan.cross_section import (
    MIN_BASKET, blend, build_cross_sectional_scores, percentile_scores,
    summarise_dispersion,
)
from mktscan.pricing import (
    bs_greeks, bs_price, implied_vol, probability_of_profit_long, years_to_expiry,
)
from mktscan.strategy import (
    DIRECTIONAL_THRESHOLD, EARNINGS_BLACKOUT_DAYS, classify_direction,
    classify_iv, select_strategy,
)
from mktscan.tradeability import (
    MIN_CATEGORY_CONFIDENCE, calc_event_driven_signal, calc_options_iv_signal,
    calc_price_momentum_signal, calc_technical_signal, compute_tradeability,
    extract_features, wilder_rsi,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Pricing
# ═══════════════════════════════════════════════════════════════════════════════

class TestBlackScholes:
    def test_put_call_parity(self):
        """C - P == S - K*exp(-rt). The single strongest check on the pricer."""
        S, K, t, v, r = 100.0, 100.0, years_to_expiry(35), 0.30, 0.042
        lhs = bs_price(S, K, t, v, "C") - bs_price(S, K, t, v, "P")
        rhs = S - K * math.exp(-r * t)
        assert abs(lhs - rhs) < 1e-9

    def test_atm_call_delta_near_half(self):
        g = bs_greeks(100, 100, years_to_expiry(35), 0.30, "C")
        assert 0.50 < g.delta < 0.60

    def test_call_put_delta_differ_by_one(self):
        t = years_to_expiry(35)
        c = bs_greeks(100, 100, t, 0.30, "C").delta
        p = bs_greeks(100, 100, t, 0.30, "P").delta
        assert abs((c - p) - 1.0) < 1e-6

    def test_delta_monotonic_in_strike(self):
        """Needed for delta-based strike selection to be well defined."""
        t = years_to_expiry(35)
        deltas = [bs_greeks(100, k, t, 0.30, "C").delta for k in range(85, 120, 5)]
        assert all(a > b for a, b in zip(deltas, deltas[1:]))

    @pytest.mark.parametrize("vol", [0.12, 0.30, 0.85])
    def test_implied_vol_round_trip(self, vol):
        t = years_to_expiry(35)
        px = bs_price(100, 110, t, vol, "C")
        assert abs(implied_vol(px, 100, 110, t, "C") - vol) < 1e-4

    def test_long_option_theta_is_negative(self):
        assert bs_greeks(100, 100, years_to_expiry(35), 0.30, "C").theta < 0

    def test_price_at_expiry_is_intrinsic(self):
        assert bs_price(110, 100, 0, 0.30, "C") == pytest.approx(10.0)
        assert bs_price(90, 100, 0, 0.30, "C") == pytest.approx(0.0)


class TestStructureEconomics:
    """
    Guards the option-level P&L that replaced the old underlying-price R/R.

    The previous code reported `2 ATR / 1.5 ATR = 1.33` for positions whose real
    payoff was closer to +200% / -75%.
    """

    @staticmethod
    def _leg(action, right, strike, bid, ask, iv=0.30):
        from mktscan.options import Leg
        return Leg(action=action, right=right, strike=strike, expiry="2026-09-18",
                   bid=bid, ask=ask, mid=(bid + ask) / 2, open_interest=5000,
                   volume=500, iv=iv, delta=None, theta=None, vega=None,
                   spread_pct=(ask - bid) / ((bid + ask) / 2))

    def test_debit_spread_economics(self):
        from mktscan.options import _net_debit, _structure_economics
        legs = [self._leg("BUY", "C", 100, 3.90, 4.10),
                self._leg("SELL", "C", 105, 1.90, 2.10)]
        # Conservative fill: pay the ask (4.10), receive the bid (1.90).
        debit = _net_debit(legs)
        assert debit == pytest.approx(2.20)

        econ = _structure_economics("bull_call_spread", legs, debit)
        assert econ["max_loss"] == pytest.approx(2.20)
        assert econ["max_profit"] == pytest.approx(2.80)
        assert econ["breakeven"] == pytest.approx(102.20)
        assert econ["is_credit"] is False

    def test_credit_spread_economics(self):
        from mktscan.options import _net_debit, _structure_economics
        legs = [self._leg("SELL", "P", 95, 2.00, 2.20),
                self._leg("BUY", "P", 90, 0.90, 1.10)]
        net = _net_debit(legs)
        assert net == pytest.approx(-0.90)          # a credit

        econ = _structure_economics("bull_put_spread", legs, net)
        assert econ["is_credit"] is True
        assert econ["max_profit"] == pytest.approx(0.90)
        assert econ["max_loss"] == pytest.approx(4.10)
        assert econ["breakeven"] == pytest.approx(94.10)

    def test_pnl_sign_convention_holds_for_debit_and_credit(self):
        """One convention must work for both, or credit spreads report inverted P&L."""
        from mktscan.options import _net_debit, value_at

        debit_legs = [self._leg("BUY", "C", 100, 3.90, 4.10),
                      self._leg("SELL", "C", 105, 1.90, 2.10)]
        open_val = _net_debit(debit_legs)
        assert value_at(debit_legs, 112, 17, 35) - open_val > 0    # favourable
        assert value_at(debit_legs, 92, 17, 35) - open_val < 0     # adverse

        credit_legs = [self._leg("SELL", "P", 95, 2.00, 2.20),
                       self._leg("BUY", "P", 90, 0.90, 1.10)]
        open_val = _net_debit(credit_legs)
        assert value_at(credit_legs, 110, 17, 35) - open_val > 0
        assert value_at(credit_legs, 88, 17, 35) - open_val < 0

    def test_conservative_fill_costs_more_than_mid(self):
        from mktscan.options import _net_debit
        legs = [self._leg("BUY", "C", 100, 3.90, 4.10),
                self._leg("SELL", "C", 105, 1.90, 2.10)]
        assert _net_debit(legs, use_mid=False) > _net_debit(legs, use_mid=True)


# ═══════════════════════════════════════════════════════════════════════════════
# Strategy selection
# ═══════════════════════════════════════════════════════════════════════════════

class TestStrategySelection:
    def test_high_iv_bullish_sells_premium(self):
        """Bullish + expensive premium should sell it, not buy it."""
        spec = select_strategy(0.6, iv_rank=85, iv_basis="chain")
        assert spec.structure == "bull_put_spread"
        assert any(leg["action"] == "SELL" for leg in spec.legs)

    def test_low_iv_bullish_buys_premium(self):
        spec = select_strategy(0.6, iv_rank=15, iv_basis="chain")
        assert spec.structure == "bull_call_spread"
        assert spec.legs[0]["action"] == "BUY"

    def test_earnings_blackout_overrides_everything(self):
        for days in range(0, EARNINGS_BLACKOUT_DAYS + 1):
            spec = select_strategy(0.9, iv_rank=10, days_to_earn=days, iv_basis="chain")
            assert spec.tradeable is False
            assert spec.avoid_reason == "earnings_too_close"

    def test_neutral_with_no_iv_edge_is_no_trade(self):
        spec = select_strategy(0.0, iv_rank=50, iv_basis="chain")
        assert spec.tradeable is False

    def test_proxy_iv_treated_as_unknown(self):
        """
        A rank built from realised vol measures a different quantity from option
        IV, so it must not be used to choose between buying and selling premium.
        """
        assert classify_iv(85, iv_basis="proxy") == "unknown"
        spec = select_strategy(0.6, iv_rank=85, iv_basis="proxy")
        assert spec.structure == "bull_call_spread"     # the safe fallback
        assert spec.sizing in ("half", "quarter")

    def test_unknown_iv_reduces_size(self):
        assert select_strategy(0.6, iv_rank=None).sizing in ("half", "quarter")

    def test_expiry_pushed_past_earnings(self):
        spec = select_strategy(0.6, iv_rank=15, days_to_earn=20, iv_basis="chain")
        assert spec.min_dte > 20, "expiry must not land before a known earnings date"

    def test_default_expiry_is_not_weekly(self):
        """
        The old selector reached for 1-week ATM options on its strongest signals —
        maximum theta and the highest breakeven hurdle available.
        """
        spec = select_strategy(0.9, iv_rank=15, iv_basis="chain")
        assert spec.target_dte >= 21

    def test_direction_thresholds_symmetric(self):
        assert classify_direction(DIRECTIONAL_THRESHOLD) == "bullish"
        assert classify_direction(-DIRECTIONAL_THRESHOLD) == "bearish"
        assert classify_direction(0.0) == "neutral"


# ═══════════════════════════════════════════════════════════════════════════════
# Cross-sectional ranking
# ═══════════════════════════════════════════════════════════════════════════════

class TestCrossSection:
    def test_constant_feature_yields_no_signal(self):
        """
        The core problem: analyst ratings were ~+0.6 for every name, so the
        signal said the same thing about everything and could not rank.
        """
        scores = percentile_scores({f"T{i}": 0.6 for i in range(10)})
        assert all(v == 0.0 for v in scores.values())

    def test_ranking_spreads_across_full_range(self):
        scores = percentile_scores({f"T{i}": float(i) for i in range(10)})
        assert scores["T0"] == pytest.approx(-1.0)
        assert scores["T9"] == pytest.approx(1.0)
        assert -0.2 < scores["T4"] < 0.2

    def test_lower_is_better_inverts(self):
        """P/E: cheaper should rank more bullish."""
        scores = percentile_scores({f"T{i}": float(i) for i in range(10)},
                                   higher_is_bullish=False)
        assert scores["T0"] == pytest.approx(1.0)
        assert scores["T9"] == pytest.approx(-1.0)

    def test_small_basket_falls_back(self):
        assert percentile_scores({f"T{i}": float(i) for i in range(MIN_BASKET - 1)}) == {}

    def test_missing_values_are_not_imputed(self):
        """A missing value is not evidence of an average value."""
        column = {f"T{i}": float(i) for i in range(10)}
        column["T3"] = None
        scores = percentile_scores(column)
        assert "T3" not in scores

    def test_ties_share_average_rank(self):
        scores = percentile_scores({"A": 1.0, "B": 1.0, "C": 1.0,
                                    "D": 5.0, "E": 5.0, "F": 5.0, "G": 9.0})
        assert scores["A"] == scores["B"] == scores["C"]
        assert scores["D"] == scores["E"] == scores["F"]

    def test_regime_invariance(self):
        """
        The same relative ordering must produce the same scores whether the whole
        basket is at highs or in a drawdown. This is the property absolute
        thresholds lack.
        """
        bull = {f"T{i}": 0.90 + i * 0.01 for i in range(10)}
        bear = {f"T{i}": 0.20 + i * 0.01 for i in range(10)}
        assert percentile_scores(bull) == percentile_scores(bear)

    def test_blend_respects_weight(self):
        assert blend(1.0, -1.0, weight=0.5) == pytest.approx(0.0)
        assert blend(0.5, None) == pytest.approx(0.5)     # no XS data → absolute

    def test_dispersion_flags_degenerate_feature(self):
        features = {f"T{i}": {"52w_position": 0.8} for i in range(10)}
        report = summarise_dispersion(features)
        assert report["52w_position"]["degenerate"] is True


# ═══════════════════════════════════════════════════════════════════════════════
# Signal construction
# ═══════════════════════════════════════════════════════════════════════════════

class TestRSI:
    def test_all_gains_is_100(self):
        assert wilder_rsi([1.0] * 60) == pytest.approx(100.0)

    def test_all_losses_is_zero(self):
        assert wilder_rsi([-1.0] * 60) == pytest.approx(0.0)

    def test_alternating_is_mid_range(self):
        rsi = wilder_rsi([1.0, -1.0] * 40)
        assert 40 < rsi < 60

    def test_requires_enough_bars(self):
        """Seeding from a single observation over 14 bars gave arbitrary readings."""
        assert wilder_rsi([0.5] * 10, period=14) is None
        assert wilder_rsi([0.5] * 20, period=14) is not None

    def test_converges_with_long_history(self):
        """Wilder smoothing must shed its seed; a stable series should be stable."""
        import random
        random.seed(7)
        series = [random.gauss(0, 1) for _ in range(400)]
        assert abs(wilder_rsi(series[-200:]) - wilder_rsi(series)) < 6


class TestMomentumSignal:
    def test_monotonic_in_rsi(self):
        """
        The old mapping scored RSI<25 at +0.8 and RSI 60-70 at +0.4, making both
        tails bullish and leaving only 40-60 neutral — two incompatible theses
        through one variable.
        """
        from mktscan.tradeability import calc_price_momentum_signal as calc
        down = calc([-1.5] * 60)
        up   = calc([1.5] * 60)
        assert down["score"] < 0 < up["score"]

    def test_extremes_flagged_not_inverted(self):
        result = calc_price_momentum_signal([2.0] * 60)
        assert result["components"].get("mean_reversion_flag") is True
        assert result["score"] > 0, "an extreme reading must not flip the sign"

    def test_insufficient_history_has_zero_confidence(self):
        assert calc_price_momentum_signal([1.0, 2.0])["confidence"] == 0.0


class TestEarningsSignal:
    def test_imminent_earnings_is_negative(self):
        """
        calc_event_driven_signal scored earnings-within-7-days at +0.5 while the
        never-called calc_earnings_proximity_signal scored it at -0.5. For a long
        option, imminent earnings is risk: IV inflates into the print and
        collapses after it.
        """
        events = [{
            "ticker": "TEST", "period": "UPCOMING",
            "report_date": datetime.combine(clock.market_date() + timedelta(days=2),
                                            datetime.min.time()),
            "eps_actual": None, "surprise_pct": None,
        }]
        result = calc_event_driven_signal("TEST", events, None)
        assert result["components"]["earnings_proximity"] < 0
        assert result["components"]["earnings_days_away"] == 2

    def test_days_to_earnings_exposed(self):
        events = [{
            "ticker": "TEST", "period": "U",
            "report_date": datetime.combine(clock.market_date() + timedelta(days=12),
                                            datetime.min.time()),
            "eps_actual": None, "surprise_pct": None,
        }]
        assert calc_event_driven_signal("TEST", events, None)["days_to_earnings"] == 12

    def test_past_earnings_with_report_date_are_used(self):
        """Rows stored with report_date=None were filtered out entirely."""
        events = [{
            "ticker": "TEST", "period": "Q1",
            "report_date": datetime.combine(clock.market_date() - timedelta(days=20),
                                            datetime.min.time()),
            "eps_actual": 1.5, "eps_estimate": 1.2, "surprise_pct": 25.0,
        }]
        result = calc_event_driven_signal("TEST", events, None)
        assert "last_eps_surprise_pct" in result["components"]


class TestIVSignal:
    def test_no_iv_data_gives_zero_confidence(self):
        """
        This is the defect that silently disabled the joint-highest-weighted
        category: the columns it read did not exist, so it always returned empty —
        and under the old weighting it still voted at 30% weight with score 0.0.
        """
        result = calc_options_iv_signal({"price": 100}, None)
        assert result["confidence"] == 0.0
        assert result["score"] == 0.0

    def test_high_iv_rank_is_negative_for_buying(self):
        result = calc_options_iv_signal(
            {"price": 100, "change_pct": 0.0},
            {"iv_rank": 90, "iv_current": 0.55, "basis": "chain", "confidence": 0.9},
        )
        assert result["score"] < 0
        assert result["iv_rank"] == 90

    def test_low_iv_rank_is_positive_for_buying(self):
        result = calc_options_iv_signal(
            {"price": 100, "change_pct": 0.0},
            {"iv_rank": 10, "iv_current": 0.18, "basis": "chain", "confidence": 0.9},
        )
        assert result["score"] > 0

    def test_proxy_basis_is_reported(self):
        result = calc_options_iv_signal(
            {"price": 100},
            {"iv_rank": 60, "iv_current": 0.3, "basis": "proxy", "confidence": 0.4},
        )
        assert result["iv_basis"] == "proxy"


class TestComposite:
    @staticmethod
    def _args(**overrides):
        args = dict(
            ticker="TEST", sentiment_score=None, article_count=0, articles=[],
            sentiment_history=[], price_data=None, earnings_events=None,
            daily_returns=None,
        )
        args.update(overrides)
        return args

    def test_empty_categories_excluded_from_denominator(self):
        """
        The old formula kept 30% of a dead category's weight at score 0.0 — an
        assertion of neutrality that missing data does not support. With ~28% of
        weight routinely dead, it compressed every score toward NEUTRAL.
        """
        result = compute_tradeability(**self._args(daily_returns=[2.0] * 60))
        assert result["coverage"] < 0.5
        # Momentum alone is strongly positive, so the composite must reflect that
        # rather than being dragged to zero by categories with no data.
        assert result["score"] > 0.15

    def test_coverage_reported(self):
        result = compute_tradeability(**self._args())
        assert "coverage" in result and "skipped_categories" in result

    def test_strategy_spec_attached(self):
        result = compute_tradeability(**self._args(daily_returns=[1.0] * 60))
        assert "strategy_spec" in result
        assert result["strategy"]["structure"] is not None

    def test_score_bounded(self):
        for returns in ([5.0] * 60, [-5.0] * 60, []):
            score = compute_tradeability(**self._args(daily_returns=returns))["score"]
            assert -1.0 <= score <= 1.0


class TestFeatureExtraction:
    def test_breakout_proximity_is_relative_not_absolute(self):
        features = extract_features(
            {"price": 100, "week_52_high": 100, "week_52_low": 50}, None, None, None
        )
        assert features["breakout_proximity"] == pytest.approx(0.0)   # at the high
        assert features["52w_position"] == pytest.approx(1.0)

    def test_analyst_mean_inverted_so_higher_is_bullish(self):
        strong_buy = extract_features({"analyst_mean_score": 1.0}, None, None, None)
        sell       = extract_features({"analyst_mean_score": 5.0}, None, None, None)
        assert strong_buy["analyst_mean_inverted"] > sell["analyst_mean_inverted"]

    def test_missing_inputs_yield_none_not_zero(self):
        features = extract_features(None, None, None, None)
        assert features["52w_position"] is None
        assert features["pe_ratio"] is None


# ═══════════════════════════════════════════════════════════════════════════════
# Market clock
# ═══════════════════════════════════════════════════════════════════════════════

class TestClock:
    def test_market_date_uses_eastern_not_utc(self):
        """
        At 23:00 UTC it is still the previous day in New York. Comparing
        utcnow().date() against earnings dates shifted the 'days to earnings'
        count by one for several hours every day.
        """
        assert clock.market_date() == clock.market_now().date()

    def test_as_date_handles_all_shapes(self):
        assert clock.as_date(date(2026, 8, 9)) == date(2026, 8, 9)
        assert clock.as_date(datetime(2026, 8, 9, 12, 0)) is not None
        assert clock.as_date("2026-08-09") == date(2026, 8, 9)
        assert clock.as_date(None) is None
        assert clock.as_date("not a date") is None

    def test_trading_day_helpers_skip_weekends(self):
        friday = date(2026, 8, 7)
        assert friday.weekday() == 4
        assert clock.next_trading_day(friday) == date(2026, 8, 10)     # Monday
        assert clock.previous_trading_day(date(2026, 8, 10)) == friday

    def test_trading_days_between_excludes_weekends(self):
        assert clock.trading_days_between(date(2026, 8, 7), date(2026, 8, 14)) == 5


# ═══════════════════════════════════════════════════════════════════════════════
# Headline deduplication
# ═══════════════════════════════════════════════════════════════════════════════

class TestHeadlineDedup:
    def test_syndicated_copy_collapses(self):
        """
        URL-level dedup misses wire copy republished by several outlets, which
        counted the same story N times and inflated the source-diversity bonus.
        """
        pytest.importorskip("sqlalchemy")
        from mktscan.database import headline_key
        a = headline_key("Apple beats Q3 earnings estimates")
        b = headline_key("apple beats q3 earnings estimates!")
        c = headline_key("UPDATE 2-Apple beats Q3 earnings estimates")
        assert a == b == c

    def test_different_stories_differ(self):
        pytest.importorskip("sqlalchemy")
        from mktscan.database import headline_key
        assert headline_key("Apple beats estimates") != headline_key("Apple misses estimates")

    def test_empty_headline_is_empty_key(self):
        pytest.importorskip("sqlalchemy")
        from mktscan.database import headline_key
        assert headline_key("") == ""


# ═══════════════════════════════════════════════════════════════════════════════
# Sentiment thresholds
# ═══════════════════════════════════════════════════════════════════════════════

class TestSentimentThresholds:
    def test_bull_bear_bands_symmetric(self):
        """
        Thresholds were +0.3 / -0.1: a -0.15 reading was BEARISH while its mirror
        at +0.15 was NEUTRAL, with no stated rationale.
        """
        from mktscan.sentiment import BEAR_THRESHOLD, BULL_THRESHOLD, classify_score
        assert BULL_THRESHOLD == pytest.approx(-BEAR_THRESHOLD)
        assert classify_score(0.25) == "BULLISH"
        assert classify_score(-0.25) == "BEARISH"
        assert classify_score(0.15) == classify_score(-0.15) == "NEUTRAL"


# ═══════════════════════════════════════════════════════════════════════════════
# Earnings surprise units
# ═══════════════════════════════════════════════════════════════════════════════

class TestEarningsSurpriseUnits:
    def test_surprise_is_a_percentage_not_a_dollar_amount(self):
        """
        yfinance's epsDifference is an absolute dollar amount but was stored in
        surprise_pct and consumed as a percentage (`avg_surp / 10.0`), so a $0.05
        beat read as a 0.5% surprise.
        """
        pytest.importorskip("tenacity")   # pulled in by sibling scraper modules
        from mktscan.scrapers.yahoo import YahooScraper
        # $0.05 beat on a $1.00 estimate is a 5% surprise, not 0.05.
        assert YahooScraper._surprise_pct(1.05, 1.00, 0.05) == pytest.approx(5.0)
        assert YahooScraper._surprise_pct(2.20, 2.00, 0.20) == pytest.approx(10.0)
        assert YahooScraper._surprise_pct(0.90, 1.00, -0.10) == pytest.approx(-10.0)

    def test_near_zero_estimate_returns_none(self):
        pytest.importorskip("tenacity")   # pulled in by sibling scraper modules
        from mktscan.scrapers.yahoo import YahooScraper
        assert YahooScraper._surprise_pct(0.05, 0.001, 0.049) is None

    def test_clamped_to_sane_range(self):
        pytest.importorskip("tenacity")   # pulled in by sibling scraper modules
        from mktscan.scrapers.yahoo import YahooScraper
        assert abs(YahooScraper._surprise_pct(50.0, 0.02, 49.98)) <= 200.0
