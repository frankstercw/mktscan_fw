"""External market-data provider adapters.

Provider-specific payloads are normalized before they enter the rest of MktScan.
"""
from .base import OptionQuote, HistoricalOptionsProvider
from .live_market import LiveStockQuote, LiveMarketProvider
from .alpaca import AlpacaMarketDataClient, AlpacaMarketDataError
from .orats import OratsClient, OratsError

__all__ = [
    "OptionQuote", "HistoricalOptionsProvider",
    "LiveStockQuote", "LiveMarketProvider",
    "OratsClient", "OratsError",
    "AlpacaMarketDataClient", "AlpacaMarketDataError",
]
