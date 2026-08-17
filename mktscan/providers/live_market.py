"""Vendor-neutral live stock market data contracts.

The dashboard consumes these normalized objects instead of provider-specific
payloads.  That keeps Alpaca replaceable by Tradier/IBKR/etc. later without
rewriting chart or indicator code.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

import pandas as pd


@dataclass(frozen=True)
class LiveStockQuote:
    ticker: str
    timestamp: datetime | None
    last: float | None
    bid: float | None
    ask: float | None
    day_open: float | None
    day_high: float | None
    day_low: float | None
    day_close: float | None
    prev_close: float | None
    day_volume: int | None
    source: str
    feed: str | None = None

    @property
    def spread(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        return self.ask - self.bid

    @property
    def change(self) -> float | None:
        if self.last is None or self.prev_close in (None, 0):
            return None
        return self.last - self.prev_close

    @property
    def change_pct(self) -> float | None:
        if self.change is None or self.prev_close in (None, 0):
            return None
        return self.change / self.prev_close * 100.0


class LiveMarketProvider(Protocol):
    """Minimal interface needed by the Streamlit live-chart page."""

    def get_quote(self, ticker: str) -> LiveStockQuote: ...

    def get_bars(
        self,
        ticker: str,
        *,
        timeframe: str,
        start: datetime,
        end: datetime | None = None,
        limit: int = 10_000,
    ) -> pd.DataFrame: ...
