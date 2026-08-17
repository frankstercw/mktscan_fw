from datetime import date

from mktscan.providers.orats import OratsClient
from mktscan.options_market import build_orats_options_market
from mktscan.providers.base import OptionQuote


class FakeResponse:
    def __init__(self, payload): self.payload = payload
    def raise_for_status(self): return None
    def json(self): return self.payload


class FakeSession:
    def __init__(self, payload): self.payload = payload
    def get(self, *args, **kwargs): return FakeResponse(self.payload)


def test_orats_chain_normalizes_call_and_put_rows():
    payload = {"data": [{
        "ticker": "AAPL", "tradeDate": "2024-01-02", "expirDate": "2024-02-16",
        "dte": 45, "strike": 190, "spotPrice": 188.0,
        "callBidPrice": 7.0, "callAskPrice": 7.2, "callValue": 7.1,
        "putBidPrice": 8.8, "putAskPrice": 9.1, "putValue": 8.95,
        "callVolume": 100, "callOpenInterest": 1000,
        "putVolume": 200, "putOpenInterest": 1200,
        "callMidIv": .25, "putMidIv": .27, "smvVol": .26,
        "delta": .48, "gamma": .03, "theta": -.12, "vega": .18,
    }]}
    client = OratsClient(token="test", session=FakeSession(payload))
    quotes = client.get_chain("AAPL", date(2024, 1, 2))
    assert len(quotes) == 2
    call = next(q for q in quotes if q.right == "C")
    put = next(q for q in quotes if q.right == "P")
    assert call.delta == .48
    assert round(put.delta, 2) == -.52
    assert call.iv == .25
    assert put.iv == .27
    assert call.source == "ORATS_EOD"


class FakeOrats:
    def get_summary(self, ticker, trade_date=None):
        return {
            "tradeDate": "2024-01-02", "stockPrice": 100,
            "iv30d": .20, "iv60d": .24, "iv90d": .26,
            "dlt25Iv30d": .18, "dlt75Iv30d": .25,
            "impliedMove": .06, "confidence": .95, "contango": .5,
        }
    def get_iv_rank(self, ticker, trade_date=None):
        return {"ivRank1y": 25, "ivPct1y": 35, "iv": 20}


def test_options_market_builds_term_skew_and_expected_move():
    result = build_orats_options_market("AAPL", client=FakeOrats())
    assert result["term_state"] == "CONTANGO"
    assert round(result["put_skew"], 4) == .05
    assert round(result["call_skew"], 4) == -.02
    assert result["expected_move_pct"] == 6.0
    assert result["expected_move_dollars"] == 6.0
    assert result["iv_rank_1y"] == 25
