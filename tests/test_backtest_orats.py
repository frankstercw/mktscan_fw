from datetime import date

from mktscan.providers.base import OptionQuote
from mktscan.backtest_options import _choose_entry_structure


def q(strike, delta, right="C", bid=2.0, ask=2.1):
    return OptionQuote(
        ticker="XYZ", trade_date=date(2024,1,2), expiration=date(2024,2,9),
        strike=strike, right=right, underlying_price=100, bid=bid, ask=ask,
        model_value=(bid+ask)/2, volume=100, open_interest=1000, iv=.25,
        delta=delta, gamma=.02, theta=-.1, vega=.2, source="ORATS_EOD"
    )


def test_bull_spread_uses_actual_delta_targets_and_orientation():
    chain = [q(100,.46), q(105,.34), q(110,.24), q(95,.65)]
    long_leg, short_leg = _choose_entry_structure(chain, .6)
    assert long_leg.strike == 100
    assert short_leg.strike == 110


def test_bear_spread_uses_put_delta_targets_and_orientation():
    chain = [
        q(100,-.46,"P"), q(95,-.32,"P"), q(90,-.24,"P"), q(105,-.62,"P")
    ]
    long_leg, short_leg = _choose_entry_structure(chain, -.6)
    assert long_leg.strike == 100
    assert short_leg.strike == 90
