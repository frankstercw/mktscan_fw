from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol


@dataclass(frozen=True)
class OptionQuote:
    """Vendor-neutral end-of-day option quote used by research/backtesting."""
    ticker: str
    trade_date: date
    expiration: date
    strike: float
    right: str                  # C | P
    underlying_price: float | None
    bid: float | None
    ask: float | None
    model_value: float | None
    volume: int | None
    open_interest: int | None
    iv: float | None
    delta: float | None
    gamma: float | None
    theta: float | None
    vega: float | None
    source: str

    @property
    def mid(self) -> float | None:
        if self.bid is not None and self.ask is not None and self.bid >= 0 and self.ask >= 0:
            return (self.bid + self.ask) / 2.0
        return self.model_value

    @property
    def spread_pct(self) -> float | None:
        mid = self.mid
        if not mid or self.bid is None or self.ask is None:
            return None
        return max(0.0, self.ask - self.bid) / mid


class HistoricalOptionsProvider(Protocol):
    def get_chain(self, ticker: str, trade_date: date, min_dte: int = 21,
                  max_dte: int = 60) -> list[OptionQuote]: ...
