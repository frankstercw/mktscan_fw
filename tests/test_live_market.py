from datetime import datetime, timezone

import pandas as pd

from mktscan.live_charts import prepare_chart_bars, daily_relative_volume
from mktscan.providers.alpaca import AlpacaMarketDataClient


class FakeResponse:
    status_code = 200
    headers = {"X-Request-ID": "test-id"}
    def __init__(self, payload): self._payload = payload
    def json(self): return self._payload


class FakeSession:
    def __init__(self, payloads):
        self.payloads = iter(payloads)
        self.calls = []
    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append((url, params))
        return FakeResponse(next(self.payloads))


def test_alpaca_snapshot_normalizes_quote():
    sess = FakeSession([{
        "latestTrade": {"p": 101.25, "t": "2026-08-17T19:59:00Z"},
        "latestQuote": {"bp": 101.20, "ap": 101.30},
        "dailyBar": {"o": 99, "h": 102, "l": 98.5, "c": 101.25, "v": 123456},
        "prevDailyBar": {"c": 100.0},
    }])
    client = AlpacaMarketDataClient("k", "s", feed="iex", session=sess)
    q = client.get_quote("aapl")
    assert q.ticker == "AAPL"
    assert q.last == 101.25
    assert q.bid == 101.20
    assert q.ask == 101.30
    assert round(q.change_pct, 2) == 1.25
    assert q.feed == "iex"


def test_alpaca_bars_normalize_dataframe():
    sess = FakeSession([{
        "bars": [
            {"t": "2026-08-17T13:30:00Z", "o": 100, "h": 101, "l": 99, "c": 100.5, "v": 1000},
            {"t": "2026-08-17T13:31:00Z", "o": 100.5, "h": 102, "l": 100, "c": 101.5, "v": 1200},
        ],
        "next_page_token": None,
    }])
    client = AlpacaMarketDataClient("k", "s", session=sess)
    df = client.get_bars("AAPL", timeframe="1Min", start=datetime(2026, 8, 17, tzinfo=timezone.utc))
    assert list(df.columns[:6]) == ["timestamp", "open", "high", "low", "close", "volume"]
    assert len(df) == 2
    assert df.iloc[-1]["close"] == 101.5


def test_prepare_chart_bars_adds_ema_vwap_and_rvol():
    df = pd.DataFrame({
        "timestamp": pd.date_range("2026-08-17 13:30", periods=25, freq="min", tz="UTC"),
        "open": range(100, 125),
        "high": [x + 1 for x in range(100, 125)],
        "low": [x - 1 for x in range(100, 125)],
        "close": [x + 0.5 for x in range(100, 125)],
        "volume": [1000] * 25,
    })
    out = prepare_chart_bars(df, "1D")
    assert {"ema_9", "ema_20", "vwap", "bar_rvol"}.issubset(out.columns)
    assert abs(out.iloc[-1]["bar_rvol"] - 1.0) < 1e-9
    assert pd.notna(out.iloc[-1]["vwap"])


def test_daily_relative_volume_uses_prior_average():
    daily = pd.DataFrame({"volume": [100, 100, 100, 100, 100, 50]})
    assert daily_relative_volume(daily, 200) == 2.0
